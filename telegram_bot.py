import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

from confirm import get_pending_recommendation, mark_worn, record_recommendation
from context import check_ambiguity, classify_context, default_calendar_text, get_tomorrow_calendar_text, get_tomorrow_date
from dateparse import parse_target_date
from db import get_connection
from ingest import has_local_photo
from profile import add_profile_note, find_known_correction, get_profile_context
from ranking import rank_candidates
from wardrobe import query_wardrobe
from weather import get_weather_constraints

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
# Shorter than Telegram's typical 30-50s long-poll default - this network's proxy
# seems to cut long-held HTTPS connections, causing repeated read timeouts at 30s.
POLL_TIMEOUT = 8

CORRECTABLE_FIELDS = {"fabric", "weight", "color", "occasion_tags", "formality", "season"}

# In-memory per-chat state: which sarees were just shown (for /correct) and which
# filtered candidates haven't been shown yet (for /more). Single-user bot, so this
# doesn't need to survive a restart - it's just "what did we just talk about."
chat_state: dict[str, dict] = {}


# Friendly button labels mapping to the actual commands underneath - tapping shows
# plain language, not slash syntax, but routes the same as typing the command.
# (Telegram's own "/" autocomplete still lists the real command names - that's a
# platform convention we don't control, separate from these custom buttons.)
LABEL_TO_COMMAND = {
    "What to wear tomorrow": "/wardrobe",
    "Plan ahead": "/plan",
    "Show another option": "/more",
    "Fix a tag": "/correct",
    "Wear history": "/history",
    "Help": "/help",
}

# Always-visible menu, so you don't have to remember any commands - tapping a
# button sends its label, which gets translated to the real command before dispatch.
MAIN_MENU = [
    ["What to wear tomorrow", "Plan ahead"],
    ["Show another option", "Fix a tag"],
    ["Wear history", "Help"],
]


def build_reply_markup(buttons: list[list[str]], one_time: bool) -> dict:
    return {
        "keyboard": [[{"text": label} for label in row] for row in buttons],
        "resize_keyboard": True,
        "one_time_keyboard": one_time,
    }


def send_message(chat_id, text: str, keyboard: list[list[str]] | None = None, one_time: bool = True) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if keyboard is not None:
        payload["reply_markup"] = build_reply_markup(keyboard, one_time)
    requests.post(f"{API}/sendMessage", json=payload, timeout=15)


def send_typing(chat_id) -> None:
    requests.post(f"{API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=15)


def send_lines(chat_id, lines: list[str], keyboard: list[list[str]] | None = None, one_time: bool = True) -> None:
    """Sends a list of lines as one or more messages, splitting under Telegram's
    ~4096 char limit without breaking mid-line. The keyboard (if any) is attached
    only to the last chunk."""
    chunk: list[str] = []
    length = 0
    chunks: list[list[str]] = []
    for line in lines:
        if chunk and length + len(line) + 1 > 3500:
            chunks.append(chunk)
            chunk, length = [], 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        chunks.append(chunk)

    for i, c in enumerate(chunks):
        is_last = i == len(chunks) - 1
        send_message(chat_id, "\n".join(c), keyboard=keyboard if is_last else None, one_time=one_time)


# Rotates so it's not the same line every time - replaces the old dry "this can
# take a bit since it runs locally" style messaging.
STATUS_MESSAGES = [
    "Draping through the possibilities...",
    "Untangling the pallu...",
    "Consulting the silk council...",
    "Matching blouses and moods...",
    "Doing a wardrobe recce...",
    "Getting my drape on...",
    "Six yards of thinking...",
    "Reticulating pleats...",
    "Convincing the weather to cooperate...",
    "Asking the closet nicely...",
    "Buffering... but make it fashion...",
]


def send_status(chat_id) -> None:
    send_message(chat_id, random.choice(STATUS_MESSAGES))


@contextmanager
def keep_typing(chat_id):
    """Telegram's typing indicator only lasts ~5s - refresh it in the background for
    however long the wrapped block (an LLM call, usually) actually takes."""
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            send_typing(chat_id)
            stop.wait(4)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def send_photo(chat_id, photo_path, caption: str) -> None:
    with open(photo_path, "rb") as f:
        requests.post(
            f"{API}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": f},
            timeout=30,
        )


def send_pick(chat_id, pick: dict) -> None:
    caption = f"{pick['fabric']} ({pick['color']}) — {pick['reasoning']}"
    photo = has_local_photo(pick["photo_id"])
    if photo:
        send_photo(chat_id, photo, caption)
    else:
        send_message(chat_id, caption)


def get_updates(offset: int | None) -> list[dict]:
    params = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(f"{API}/getUpdates", params=params, timeout=POLL_TIMEOUT + 10)
    resp.raise_for_status()
    return resp.json()["result"]


class UpdateCursor:
    """Tracks the next update_id to fetch, shared between the main poll loop and
    wait_for_reply (which needs to keep advancing the same cursor when it consumes
    messages while blocking for a specific reply)."""

    def __init__(self):
        self.next_id: int | None = None

    def advance_past(self, update: dict) -> None:
        self.next_id = update["update_id"] + 1


def wait_for_reply(chat_id, cursor: UpdateCursor) -> str:
    """Blocks (long-polling) until a new text message arrives from chat_id."""
    while True:
        for update in get_updates(cursor.next_id):
            cursor.advance_past(update)
            message = update.get("message")
            if message and str(message["chat"]["id"]) == str(chat_id) and "text" in message:
                return message["text"].strip()


def resolve_occasion_text(chat_id, cursor: UpdateCursor, raw_text: str) -> str:
    """Applies an already-learned correction deterministically (matched by simple
    substring, not left to the LLM to honor an "already clarified" instruction - it
    doesn't reliably). If nothing's known yet and the text is genuinely ambiguous,
    asks once and saves the answer so it's known next time."""
    known = find_known_correction(raw_text)
    if known:
        return f"{raw_text}\n(Note: {known})"

    question = check_ambiguity(raw_text)
    if question is None:
        return raw_text

    send_message(chat_id, question)
    clarification = wait_for_reply(chat_id, cursor)
    add_profile_note(raw_text, clarification)
    return f"{raw_text}\n(Note: {clarification})"


def run_wardrobe_flow(chat_id, cursor: UpdateCursor) -> None:
    send_status(chat_id)

    pending = get_pending_recommendation()
    if pending is not None:
        send_message(
            chat_id,
            f"Did you wear the {pending['fabric']} ({pending['color']}) saree "
            f"recommended for {pending['last_recommended_date']}?",
            keyboard=[["Yes", "No"]],
        )
        answer = wait_for_reply(chat_id, cursor)
        if answer.lower().startswith("y"):
            mark_worn(pending["photo_id"], pending["last_recommended_date"])
            send_message(chat_id, "Got it, marked as worn.")
        else:
            send_message(chat_id, "Okay, noted - not marking it as worn.")
        send_status(chat_id)

    tomorrow = get_tomorrow_date()
    calendar_text = get_tomorrow_calendar_text(tomorrow)
    if calendar_text is None:
        send_message(
            chat_id,
            f"No events found on your calendar for tomorrow "
            f"({tomorrow.strftime('%A, %b %d')}). Anything planned?",
            keyboard=[["No, nothing special"]],
        )
        answer = wait_for_reply(chat_id, cursor)
        calendar_text = default_calendar_text(tomorrow) if answer.lower().startswith("no") else answer

    calendar_text = resolve_occasion_text(chat_id, cursor, calendar_text)
    send_status(chat_id)

    with keep_typing(chat_id):
        occasion_ctx = classify_context(calendar_text, get_profile_context())
        weather_ctx = get_weather_constraints()
        recommend_and_deliver(chat_id, occasion_ctx, weather_ctx, tomorrow, when_label="Tomorrow")


def recommend_and_deliver(chat_id, occasion_ctx, weather_ctx: dict, target_date, when_label: str) -> None:
    """Shared by /wardrobe (always tomorrow) and /plan (any date): filters the
    wardrobe, ranks, sends the result, and records the recommendation for that date.
    Assumes it's already running inside a keep_typing(chat_id) block."""
    candidates = query_wardrobe(occasion_ctx.formality, weather_ctx["avoid_fabrics"])

    if not candidates:
        send_message(
            chat_id,
            "No sarees match that occasion/weather even after relaxing filters - "
            "you may need to tag more of your catalog.",
        )
        return

    result = rank_candidates(occasion_ctx, weather_ctx, candidates)
    top = result["top_pick"]
    shown = [top] + result["alternates"]
    shown_ids = {c["photo_id"] for c in shown}
    remaining = [c for c in candidates if c["photo_id"] not in shown_ids]
    chat_state[str(chat_id)] = {"shown": shown, "remaining": remaining}

    send_message(
        chat_id,
        f"{when_label} ({weather_ctx['date']}): {occasion_ctx.occasion}, "
        f"feels like {weather_ctx['feels_like_max']}°C, "
        f"{weather_ctx['precip_probability']}% chance of rain.",
    )
    send_pick(chat_id, top)
    if result["alternates"]:
        send_message(chat_id, "Alternates:")
        for alt in result["alternates"]:
            send_pick(chat_id, alt)
    send_message(chat_id, "Want another option? Spot a wrong tag?", keyboard=MAIN_MENU, one_time=False)

    record_recommendation(top["photo_id"], target_date.isoformat())


def run_plan_flow(chat_id, cursor: UpdateCursor) -> None:
    """Like /wardrobe but for an arbitrary future date + a directly-stated occasion,
    instead of always tomorrow + whatever's on the calendar. E.g. 'what should I
    wear for the wedding next Saturday'."""
    send_message(chat_id, "Which date? (e.g. 'next Saturday', 'July 12', or a specific date)")
    date_answer = wait_for_reply(chat_id, cursor)
    target_date = parse_target_date(date_answer)
    if target_date is None:
        send_message(chat_id, f"Couldn't understand '{date_answer}' as a date - try /plan again with something like 'next Saturday' or 'July 12'.")
        return

    send_message(chat_id, "And what's the occasion?")
    occasion_answer = wait_for_reply(chat_id, cursor)
    occasion_answer = resolve_occasion_text(chat_id, cursor, occasion_answer)

    send_status(chat_id)
    with keep_typing(chat_id):
        occasion_ctx = classify_context(occasion_answer, get_profile_context())
        try:
            weather_ctx = get_weather_constraints(target_date)
        except ValueError as e:
            send_message(chat_id, f"{e} Try a closer date.")
            return
        recommend_and_deliver(chat_id, occasion_ctx, weather_ctx, target_date, when_label=target_date.strftime("%A, %b %d"))


def run_more_flow(chat_id) -> None:
    state = chat_state.get(str(chat_id))
    if not state or not state["remaining"]:
        send_message(chat_id, "No more options left from today's filtered list - run /wardrobe again to start fresh.", keyboard=MAIN_MENU, one_time=False)
        return
    next_pick = state["remaining"].pop(0)
    state["shown"].append({**next_pick, "reasoning": "another option from today's matches"})
    send_pick(chat_id, state["shown"][-1])


FIELD_BUTTONS = [["fabric", "weight", "color"], ["occasion_tags", "formality", "season"]]


def get_saree(photo_id: str) -> dict:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM sarees WHERE photo_id = ?", (photo_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def run_correct_flow(chat_id, cursor: UpdateCursor, arg: str) -> None:
    state = chat_state.get(str(chat_id))
    if not state or not state["shown"]:
        send_message(chat_id, "I haven't shown you anything yet this session - run /wardrobe first.", keyboard=MAIN_MENU, one_time=False)
        return
    shown = state["shown"]

    if arg.strip().isdigit():
        index = int(arg) - 1
    elif len(shown) == 1:
        index = 0
    else:
        option_labels = [f"{i + 1}: {c['fabric']} ({c['color']})" for i, c in enumerate(shown)]
        send_message(chat_id, "Which one do you want to fix?", keyboard=[[label] for label in option_labels])
        pick = wait_for_reply(chat_id, cursor)
        index = int(pick.split(":")[0]) - 1 if pick[:1].isdigit() else -1

    if not (0 <= index < len(shown)):
        send_message(chat_id, f"That's not one of the {len(shown)} I've shown you.", keyboard=MAIN_MENU, one_time=False)
        return

    photo_id = shown[index]["photo_id"]
    saree = get_saree(photo_id)

    send_message(
        chat_id,
        f"{shown[index]['fabric']} ({shown[index]['color']}) is currently tagged:\n"
        f"fabric={saree.get('fabric')}, weight={saree.get('weight')}, "
        f"color={saree.get('color')}, occasion_tags={saree.get('occasion_tags')}, "
        f"formality={saree.get('formality')}, season={saree.get('season')}\n\n"
        "Which field is wrong?",
        keyboard=FIELD_BUTTONS,
    )
    field = wait_for_reply(chat_id, cursor).strip().lower()
    if field not in CORRECTABLE_FIELDS:
        send_message(chat_id, f"'{field}' isn't a field I can correct. Valid ones: {', '.join(sorted(CORRECTABLE_FIELDS))}.", keyboard=MAIN_MENU, one_time=False)
        return

    send_message(chat_id, f"What should {field} be instead? (currently: {saree.get(field)})")
    value = wait_for_reply(chat_id, cursor).strip()
    if field == "formality":
        if not value.isdigit():
            send_message(chat_id, "formality needs to be a number from 1 to 5.", keyboard=MAIN_MENU, one_time=False)
            return
        value = int(value)

    conn = get_connection()
    conn.execute(f"UPDATE sarees SET {field} = ? WHERE photo_id = ?", (value, photo_id))
    conn.commit()
    conn.close()
    send_message(chat_id, f"Updated {field} to '{value}'.", keyboard=MAIN_MENU, one_time=False)


def get_wear_history_sorted() -> list[dict]:
    """Oldest-worn first; never-worn sarees sort first of all (most "overdue")."""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.fabric, s.color, w.last_worn_date, w.wear_count
        FROM sarees s
        LEFT JOIN wear_history w ON s.photo_id = w.photo_id
        ORDER BY w.last_worn_date IS NOT NULL, w.last_worn_date ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run_history_flow(chat_id) -> None:
    rows = get_wear_history_sorted()
    if not rows:
        send_message(chat_id, "No sarees tagged yet.", keyboard=MAIN_MENU, one_time=False)
        return

    lines = ["Oldest to newest worn:"]
    for i, r in enumerate(rows, start=1):
        worn = r["last_worn_date"] or "never worn"
        count = r["wear_count"] or 0
        suffix = f", worn {count}x" if count else ""
        lines.append(f"{i}. {r['fabric']} ({r['color']}) — {worn}{suffix}")

    send_lines(chat_id, lines, keyboard=MAIN_MENU, one_time=False)


BOT_COMMANDS = [
    {"command": "wardrobe", "description": "What to wear tomorrow"},
    {"command": "plan", "description": "What to wear for a specific date/occasion"},
    {"command": "more", "description": "Show another option from the last result"},
    {"command": "correct", "description": "Fix a wrong tag (e.g. /correct 2)"},
    {"command": "history", "description": "Wear history, oldest to newest worn"},
    {"command": "help", "description": "List what this bot can do"},
]


def register_commands() -> None:
    """Makes Telegram show these in the client's own '/' autocomplete menu, so you
    don't have to remember them."""
    requests.post(f"{API}/setMyCommands", json={"commands": BOT_COMMANDS}, timeout=15)


def run_help_flow(chat_id) -> None:
    lines = [f"/{c['command']} — {c['description']}" for c in BOT_COMMANDS]
    send_message(chat_id, "\n".join(lines), keyboard=MAIN_MENU, one_time=False)


def main() -> None:
    register_commands()
    cursor = UpdateCursor()
    print("Telegram bot running. Commands: /wardrobe, /plan, /more, /correct, /help.")
    while True:
        try:
            updates = get_updates(cursor.next_id)
            # Process one update per outer-loop pass, then re-fetch fresh - a flow
            # triggered below (e.g. /wardrobe) calls wait_for_reply(), which does
            # its own polling and advances this same cursor. If we kept iterating
            # this batch afterward, any later message already consumed by that
            # nested wait would get reprocessed here too (this is what caused a
            # confirm question to fire twice).
            for update in updates[:1]:
                cursor.advance_past(update)
                message = update.get("message")
                if not message or "text" not in message:
                    continue
                chat_id = message["chat"]["id"]
                text = LABEL_TO_COMMAND.get(message["text"].strip(), message["text"].strip())
                command, _, arg = text.partition(" ")
                command = command.lower()
                if command == "/wardrobe":
                    run_wardrobe_flow(chat_id, cursor)
                elif command == "/plan":
                    run_plan_flow(chat_id, cursor)
                elif command == "/more":
                    run_more_flow(chat_id)
                elif command == "/correct":
                    run_correct_flow(chat_id, cursor, arg)
                elif command == "/history":
                    run_history_flow(chat_id)
                elif command in ("/help", "/start"):
                    run_help_flow(chat_id)
                else:
                    # Any message that reaches here isn't part of an active
                    # flow's wait_for_reply (those consume messages before this
                    # loop sees them) - so it's a genuinely unrecognized message,
                    # not something silently dropped mid-conversation.
                    send_message(chat_id, "Here's what I can help with — tap one:", keyboard=MAIN_MENU, one_time=False)
        except requests.exceptions.RequestException as e:
            print(f"Network error, retrying: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
