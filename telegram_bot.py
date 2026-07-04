import os
import sqlite3
import threading
import time
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

from confirm import get_pending_recommendation, mark_worn, record_recommendation
from context import classify_context, default_calendar_text, get_tomorrow_calendar_text, get_tomorrow_date
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


def send_message(chat_id, text: str) -> None:
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)


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


def run_wardrobe_flow(chat_id, cursor: UpdateCursor) -> None:
    send_message(chat_id, "On it — checking your calendar, the weather, and your wardrobe...")

    pending = get_pending_recommendation()
    if pending is not None:
        send_message(
            chat_id,
            f"Did you wear the {pending['fabric']} ({pending['color']}) saree "
            f"recommended for {pending['last_recommended_date']}? (y/n)",
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
            f"({tomorrow.strftime('%A, %b %d')}). Anything planned? "
            "(reply 'no' if nothing special)",
        )
        answer = wait_for_reply(chat_id, cursor)
        calendar_text = answer if answer.lower() not in ("no", "n", "nothing") else default_calendar_text(tomorrow)
        send_message(chat_id, "Got it — thinking it through now, this can take a bit since it runs locally...")

    with keep_typing(chat_id):
        occasion_ctx = classify_context(calendar_text)
        weather_ctx = get_weather_constraints()
        candidates = query_wardrobe(occasion_ctx.formality, weather_ctx["avoid_fabrics"])

        if not candidates:
            send_message(
                chat_id,
                "No sarees match tomorrow's occasion/weather even after relaxing "
                "filters - you may need to tag more of your catalog.",
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
        f"Tomorrow ({weather_ctx['date']}): {occasion_ctx.occasion}, "
        f"feels like {weather_ctx['feels_like_max']}°C, "
        f"{weather_ctx['precip_probability']}% chance of rain.",
    )
    send_pick(chat_id, top)
    if result["alternates"]:
        send_message(chat_id, "Alternates:")
        for alt in result["alternates"]:
            send_pick(chat_id, alt)
    send_message(chat_id, "Want another option? Send /more. Spot a wrong tag? Send /correct.")

    tomorrow_iso = tomorrow.isoformat()
    record_recommendation(top["photo_id"], tomorrow_iso)


def run_more_flow(chat_id) -> None:
    state = chat_state.get(str(chat_id))
    if not state or not state["remaining"]:
        send_message(chat_id, "No more options left from today's filtered list - run /wardrobe again to start fresh.")
        return
    next_pick = state["remaining"].pop(0)
    state["shown"].append({**next_pick, "reasoning": "another option from today's matches"})
    send_pick(chat_id, state["shown"][-1])


def run_correct_flow(chat_id, cursor: UpdateCursor, arg: str) -> None:
    state = chat_state.get(str(chat_id))
    if not state or not state["shown"]:
        send_message(chat_id, "I haven't shown you anything yet this session - run /wardrobe first.")
        return

    index = int(arg) - 1 if arg.strip().isdigit() else 0
    if not (0 <= index < len(state["shown"])):
        send_message(chat_id, f"That's not one of the {len(state['shown'])} I've shown you - try /correct 1, /correct 2, etc.")
        return

    photo_id = state["shown"][index]["photo_id"]
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM sarees WHERE photo_id = ?", (photo_id,)).fetchone()
    saree = dict(row) if row else {}
    conn.close()

    send_message(
        chat_id,
        f"Current tags: fabric={saree.get('fabric')}, weight={saree.get('weight')}, "
        f"color={saree.get('color')}, occasion_tags={saree.get('occasion_tags')}, "
        f"formality={saree.get('formality')}, season={saree.get('season')}\n\n"
        "What should change? Reply like 'fabric: cotton' or 'formality: 3'.",
    )
    answer = wait_for_reply(chat_id, cursor)
    if ":" not in answer:
        send_message(chat_id, "Didn't understand that - use the format 'field: value', e.g. 'fabric: cotton'.")
        return

    field, value = (part.strip() for part in answer.split(":", 1))
    field = field.lower()
    if field not in CORRECTABLE_FIELDS:
        send_message(chat_id, f"'{field}' isn't a field I can correct. Valid ones: {', '.join(sorted(CORRECTABLE_FIELDS))}.")
        return
    if field == "formality":
        if not value.isdigit():
            send_message(chat_id, "formality needs to be a number from 1 to 5.")
            return
        value = int(value)

    conn = get_connection()
    conn.execute(f"UPDATE sarees SET {field} = ? WHERE photo_id = ?", (value, photo_id))
    conn.commit()
    conn.close()
    send_message(chat_id, f"Updated {field} to '{value}'.")


def main() -> None:
    cursor = UpdateCursor()
    print("Telegram bot running. Send /wardrobe to your bot to get a recommendation.")
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
                elif command == "/more":
                    run_more_flow(chat_id)
                elif command == "/correct":
                    run_correct_flow(chat_id, cursor, arg)
        except requests.exceptions.RequestException as e:
            print(f"Network error, retrying: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
