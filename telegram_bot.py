import os
import sqlite3
import threading
import time
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

from confirm import get_pending_recommendation, mark_worn, record_recommendation
from context import classify_context, default_calendar_text, get_tomorrow_calendar_text, get_tomorrow_date
from dateparse import parse_target_date
from db import get_connection
from ingest import has_local_photo
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

# Message IDs the bot itself has sent, per chat - so /clrscr can delete them. Telegram
# only lets a bot delete its own messages, never the other party's, so this can only
# ever clear my replies, not what you typed.
sent_message_ids: dict[str, list[int]] = {}


def _track_sent(chat_id, resp: requests.Response) -> None:
    data = resp.json()
    if data.get("ok"):
        sent_message_ids.setdefault(str(chat_id), []).append(data["result"]["message_id"])


# Always-visible menu of the main commands, so you don't have to remember them -
# tapping a button just sends that text, same as typing it.
MAIN_MENU = [["/wardrobe", "/plan"], ["/more", "/correct"], ["/help"]]


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
    resp = requests.post(f"{API}/sendMessage", json=payload, timeout=15)
    _track_sent(chat_id, resp)


def send_typing(chat_id) -> None:
    requests.post(f"{API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=15)


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
        resp = requests.post(
            f"{API}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": f},
            timeout=30,
        )
    _track_sent(chat_id, resp)


def run_clrscr_flow(chat_id) -> None:
    message_ids = sent_message_ids.pop(str(chat_id), [])
    for message_id in message_ids:
        try:
            requests.post(f"{API}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id}, timeout=15)
        except requests.exceptions.RequestException:
            pass  # message too old to delete, or already gone - not worth failing over
    send_message(
        chat_id,
        "Cleared my messages. (Telegram doesn't let a bot delete what you typed - only its own.)",
        keyboard=MAIN_MENU,
        one_time=False,
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


def run_wardrobe_flow(chat_id, cursor: UpdateCursor) -> None:
    send_message(chat_id, "On it — checking your calendar, the weather, and your wardrobe...")

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
            send_message(chat_id, "Got it, marked as worn. Now figuring out tomorrow...")
        else:
            send_message(chat_id, "Okay, noted - not marking it as worn. Now figuring out tomorrow...")

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
        send_message(chat_id, "Got it — thinking it through now, this can take a bit since it runs locally...")

    with keep_typing(chat_id):
        occasion_ctx = classify_context(calendar_text)
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

    send_message(chat_id, f"Got it — checking the weather and your wardrobe for {target_date.strftime('%A, %b %d')}...")
    with keep_typing(chat_id):
        occasion_ctx = classify_context(occasion_answer)
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


BOT_COMMANDS = [
    {"command": "wardrobe", "description": "What to wear tomorrow"},
    {"command": "plan", "description": "What to wear for a specific date/occasion"},
    {"command": "more", "description": "Show another option from the last result"},
    {"command": "correct", "description": "Fix a wrong tag (e.g. /correct 2)"},
    {"command": "clrscr", "description": "Clear my messages from this chat"},
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
            for update in get_updates(cursor.next_id):
                cursor.advance_past(update)
                message = update.get("message")
                if not message or "text" not in message:
                    continue
                chat_id = message["chat"]["id"]
                text = message["text"].strip()
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
                elif command == "/clrscr":
                    run_clrscr_flow(chat_id)
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
