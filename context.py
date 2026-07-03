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


def resolve_empty_calendar_text(tomorrow: datetime.date) -> str:
    """Calendar being empty doesn't mean nothing's happening - always ask rather than
    silently assume. Falls back to a weekday-aware default (school/work vs. day off)
    only if you don't answer."""
    prompt = (
        f"No events found on your calendar for tomorrow "
        f"({tomorrow.strftime('%A, %b %d')}). Is there anything planned? "
        "(press Enter if nothing special) "
    )
    answer = input(prompt).strip()
    if answer:
        return answer
    is_weekday = tomorrow.weekday() < 5  # Monday=0 .. Sunday=6
    if is_weekday:
        return "Regular school/work day, nothing special planned."
    return "Regular day off, nothing special planned, staying at home."


# Deciding whether to also check email for context was originally meant to be an
# agentic tool-use decision, but llama3.2 (the free local model we're using here,
# since there's no funded Anthropic account) isn't reliable enough at multi-step
# tool orchestration - it skipped the calendar and called email with a garbage query
# in testing. Simplified to a single, non-agentic classification call over calendar
# text only. See SPEC.md.
CLASSIFY_PROMPT = """Given tomorrow's plans below, classify the occasion for someone
deciding what saree to wear. Respond with ONLY a JSON object, no other text, with
exactly these four fields:

- "occasion": a short string describing the occasion
- "formality": an integer from 1 (very casual) to 5 (very formal/bridal). A regular
  school/work day is around 2. A plain day off at home is 1.
- "time_of_day": pick exactly ONE of these four words: morning, afternoon, evening, night
- "indoor_outdoor": pick exactly ONE of these three words: indoor, outdoor, mixed

Plans:
{calendar_text}"""

VALID_TIMES_OF_DAY = {"morning", "afternoon", "evening", "night"}
VALID_INDOOR_OUTDOOR = {"indoor", "outdoor", "mixed"}


def get_context() -> OccasionContext:
    tomorrow = get_tomorrow_date()
    calendar_text = get_tomorrow_calendar_text(tomorrow)
    if calendar_text is None:
        calendar_text = resolve_empty_calendar_text(tomorrow)

    model = ChatOllama(model="llama3.2", temperature=0)
    response = model.invoke(CLASSIFY_PROMPT.format(calendar_text=calendar_text))
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


if __name__ == "__main__":
    print(get_context())
