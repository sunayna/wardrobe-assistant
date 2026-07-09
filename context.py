import datetime
import json
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from google_auth import get_credentials

TZ = ZoneInfo("Asia/Kolkata")


class OccasionContext(BaseModel):
    occasion: str
    formality: int  # 1 (very casual) to 5 (very formal/bridal)
    time_of_day: str  # morning, afternoon, evening, or night
    indoor_outdoor: str  # indoor, outdoor, or mixed


def get_tomorrow_date() -> datetime.date:
    return datetime.datetime.now(TZ).date() + datetime.timedelta(days=1)


def get_tomorrow_calendar_text(tomorrow: datetime.date) -> str | None:
    """Returns None if there are no calendar events (distinct from an empty string)."""
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)
    time_min = datetime.datetime.combine(tomorrow, datetime.time.min, tzinfo=TZ).isoformat()
    time_max = datetime.datetime.combine(tomorrow, datetime.time.max, tzinfo=TZ).isoformat()
    events_result = (
        service.events()
        .list(calendarId="primary", timeMin=time_min, timeMax=time_max,
              singleEvents=True, orderBy="startTime")
        .execute()
    )
    events = events_result.get("items", [])
    if not events:
        return None
    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        summary = e.get("summary", "(no title)")
        location = e.get("location", "")
        line = f"{start} - {summary}"
        if location:
            line += f" @ {location}"
        lines.append(line)
    return "\n".join(lines)


def default_calendar_text(tomorrow: datetime.date) -> str:
    """Weekday-aware fallback for when the calendar is empty AND nobody answered
    when asked - never used silently, only after giving the user a chance to say
    what's actually happening (see get_context / telegram_bot.py)."""
    is_weekday = tomorrow.weekday() < 5  # Monday=0 .. Sunday=6
    if is_weekday:
        return "Regular school/work day, nothing special planned."
    return "Regular day off, nothing special planned, staying at home."


AMBIGUITY_PROMPT = """Given this description of tomorrow's plans, decide if it's too
vague to confidently determine the occasion, formality, and setting for choosing an
outfit. Short, generic, or jargon-y phrases (like "parent meet", "team sync", "the
thing at 5") are often ambiguous - full sentences usually are not.

Respond with ONLY a JSON object with exactly these two fields:
- "ambiguous": true or false
- "question": if ambiguous, a short question to ask to find out what's actually
  happening. Empty string if not ambiguous.

Plans:
{calendar_text}"""


def check_ambiguity(calendar_text: str) -> str | None:
    """Returns a clarifying question if the plans are too vague to classify
    confidently, else None. Fails open (returns None) on any parsing hiccup - this
    check is a nice-to-have, not worth blocking the whole flow over. Only call this
    when profile.find_known_correction() has already come back empty - past
    corrections are matched deterministically, not left to the LLM to honor a
    prompt instruction (it doesn't reliably)."""
    prompt = AMBIGUITY_PROMPT.format(calendar_text=calendar_text)
    model = ChatOllama(model="llama3.2", temperature=0)
    response = model.invoke(prompt)
    raw = response.content
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if data.get("ambiguous") and data.get("question"):
        return data["question"]
    return None


CLASSIFY_PROMPT = """Given tomorrow's plans below, classify the occasion for someone
deciding what saree to wear. Respond with ONLY a JSON object, no other text, with
exactly these four fields:

- "occasion": a short string describing the occasion
- "formality": an integer from 1 (very casual) to 5 (very formal/bridal). A regular
  school/work day is around 2. A plain day off at home is 1.
- "time_of_day": pick exactly ONE of these four words: morning, afternoon, evening, night
- "indoor_outdoor": pick exactly ONE of these three words: indoor, outdoor, mixed
{profile_section}
Plans:
{calendar_text}"""

VALID_TIMES_OF_DAY = {"morning", "afternoon", "evening", "night"}
VALID_INDOOR_OUTDOOR = {"indoor", "outdoor", "mixed"}


def classify_context(calendar_text: str, profile_context: str = "") -> OccasionContext:
    profile_section = f"\n{profile_context}\n" if profile_context else ""
    prompt = CLASSIFY_PROMPT.format(calendar_text=calendar_text, profile_section=profile_section)
    model = ChatOllama(model="llama3.2", temperature=0)
    response = model.invoke(prompt)
    raw = response.content
    start, end = raw.find("{"), raw.rfind("}")
    data = json.loads(raw[start : end + 1])

    # llama3.2 sometimes echoes the allowed-values hint verbatim instead of picking
    # one - fall back to a safe default rather than fail the whole pipeline over it.
    if data.get("time_of_day") not in VALID_TIMES_OF_DAY:
        data["time_of_day"] = "morning"
    if data.get("indoor_outdoor") not in VALID_INDOOR_OUTDOOR:
        data["indoor_outdoor"] = "indoor"

    return OccasionContext(**data)


def get_context() -> OccasionContext:
    """CLI entry point - prompts via input() directly. For other front-ends (e.g. the
    Telegram bot), use get_tomorrow_date/get_tomorrow_calendar_text/
    default_calendar_text/classify_context directly with your own way of asking."""
    tomorrow = get_tomorrow_date()
    calendar_text = get_tomorrow_calendar_text(tomorrow)
    if calendar_text is None:
        prompt = (
            f"No events found on your calendar for tomorrow "
            f"({tomorrow.strftime('%A, %b %d')}). Is there anything planned? "
            "(press Enter if nothing special) "
        )
        answer = input(prompt).strip()
        calendar_text = answer or default_calendar_text(tomorrow)

    from profile import get_profile_context  # local import - keeps context.py DB-free unless actually used

    return classify_context(calendar_text, get_profile_context())


if __name__ == "__main__":
    print(get_context())
