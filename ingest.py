import base64
import json
import os
import time

import requests
from dotenv import load_dotenv

from db import get_connection, init_db
from google_auth import get_credentials

load_dotenv()

PICKER_BASE = "https://photospicker.googleapis.com/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"

TAGGING_PROMPT = """This is a photo of a saree (an Indian garment). Look at it and
respond with ONLY a JSON object, no other text, in exactly this shape:

{"fabric": "...", "weight": "light|medium|heavy", "color": "...",
 "occasion_tags": "comma,separated,tags", "formality": 1-5,
 "season": "summer|winter|monsoon|all"}

fabric: your best guess (e.g. silk, cotton, georgette, chiffon, linen).
weight: how heavy/warm the fabric looks.
color: the dominant color(s).
occasion_tags: likely occasions (e.g. casual, office, festive, wedding, puja).
formality: 1 (very casual) to 5 (very formal/bridal).
season: when it'd be comfortable to wear."""


def create_session(creds) -> dict:
    resp = requests.post(
        f"{PICKER_BASE}/sessions",
        headers={"Authorization": f"Bearer {creds.token}"},
    )
    resp.raise_for_status()
    return resp.json()


def wait_for_selection(creds, session: dict) -> dict:
    session_id = session["id"]
    poll_interval = float(session.get("pollingConfig", {}).get("pollInterval", "2s").rstrip("s"))
    while True:
        resp = requests.get(
            f"{PICKER_BASE}/sessions/{session_id}",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        resp.raise_for_status()
        session = resp.json()
        if session.get("mediaItemsSet"):
            return session
        time.sleep(poll_interval)


def list_media_items(creds, session_id: str) -> list[dict]:
    items = []
    page_token = None
    while True:
        params = {"sessionId": session_id, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(
            f"{PICKER_BASE}/mediaItems",
            headers={"Authorization": f"Bearer {creds.token}"},
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("mediaItems", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return items


class DailyQuotaExhausted(Exception):
    pass


def with_retries(fn, *args, attempts=5, base_delay=5, **kwargs):
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429 and "PerDay" in (e.response.text or ""):
                # Daily quota, not a per-minute rate limit - retrying today is
                # pointless, stop the whole run instead of burning attempts on
                # every remaining photo.
                raise DailyQuotaExhausted from e
            if attempt == attempts:
                raise
            wait = base_delay * (2 ** (attempt - 1))
            if status == 429:
                # Per-minute rate limit - a short backoff just hits it again.
                wait = max(wait, 20)
            time.sleep(wait)
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(base_delay * attempt)


def download_image_bytes(creds, base_url: str) -> bytes:
    resp = requests.get(
        f"{base_url}=d",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


# Gemini free tier for gemini-2.5-flash-lite allows 15 requests/minute. Space calls
# out so we stay under that instead of bursting and hitting 429s.
GEMINI_MIN_INTERVAL = 4.5
_last_gemini_call = 0.0


def tag_image(image_bytes: bytes, mime_type: str) -> dict:
    global _last_gemini_call
    elapsed = time.monotonic() - _last_gemini_call
    if elapsed < GEMINI_MIN_INTERVAL:
        time.sleep(GEMINI_MIN_INTERVAL - elapsed)
    _last_gemini_call = time.monotonic()

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    resp = requests.post(
        GEMINI_URL,
        headers={
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
            "Content-Type": "application/json",
        },
        json={
            "contents": [{
                "parts": [
                    {"text": TAGGING_PROMPT},
                    {"inline_data": {"mime_type": mime_type, "data": b64}},
                ]
            }],
            "generationConfig": {"response_mime_type": "application/json"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)


def upsert_saree(photo_id: str, tags: dict) -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO sarees (photo_id, fabric, weight, color, occasion_tags, formality, season)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(photo_id) DO UPDATE SET
            fabric=excluded.fabric, weight=excluded.weight, color=excluded.color,
            occasion_tags=excluded.occasion_tags, formality=excluded.formality,
            season=excluded.season
        """,
        (
            photo_id,
            tags.get("fabric"),
            tags.get("weight"),
            tags.get("color"),
            tags.get("occasion_tags"),
            tags.get("formality"),
            tags.get("season"),
        ),
    )
    conn.commit()
    conn.close()


def already_tagged(photo_id: str) -> bool:
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM sarees WHERE photo_id = ?", (photo_id,)).fetchone()
    conn.close()
    return row is not None


def run_ingestion() -> None:
    init_db()
    creds = get_credentials()

    session = create_session(creds)
    print(f"Open this link and select your saree photos/album:\n{session['pickerUri']}")
    session = wait_for_selection(creds, session)

    media_items = list_media_items(creds, session["id"])
    print(f"Selected {len(media_items)} photo(s). Tagging...")

    for i, item in enumerate(media_items, start=1):
        photo_id = item["id"]
        base_url = item["mediaFile"]["baseUrl"]
        mime_type = item["mediaFile"].get("mimeType", "image/jpeg")
        filename = item["mediaFile"].get("filename", photo_id)
        print(f"[{i}/{len(media_items)}] {filename} ...", end=" ", flush=True)
        if already_tagged(photo_id):
            print("already tagged, skipping")
            continue
        try:
            image_bytes = with_retries(download_image_bytes, creds, base_url)
            tags = with_retries(tag_image, image_bytes, mime_type)
            upsert_saree(photo_id, tags)
            print(f"tagged: {tags}")
        except DailyQuotaExhausted:
            tagged_count = sum(1 for it in media_items if already_tagged(it["id"]))
            print(
                f"\nDaily Gemini quota exhausted. {tagged_count}/{len(media_items)} "
                "tagged so far. Re-run tomorrow to continue - already-tagged photos "
                "are skipped automatically."
            )
            return
        except Exception as e:
            print(f"FAILED after retries: {e}")

    print("Ingestion complete.")


if __name__ == "__main__":
    run_ingestion()
