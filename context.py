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


def get_tomorrow_calendar_text() -> str:
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)
    tomorrow = datetime.datetime.now(TZ).date() + datetime.timedelta(days=1)
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
        return "No events found for tomorrow."
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


# Deciding whether to also check email for context was originally meant to be an
# agentic tool-use decision, but llama3.2 (the free local model we're using here,
# since there's no funded Anthropic account) isn't reliable enough at multi-step
# tool orchestration - it skipped the calendar and called email with a garbage query
# in testing. Simplified to a single, non-agentic classification call over calendar
# text only. See SPEC.md.
CLASSIFY_PROMPT = """Given tomorrow's calendar events below, classify the occasion for
someone deciding what saree to wear. Respond with ONLY a JSON object, no other text,
in exactly this shape:

{{"occasion": "...", "formality": 1-5, "time_of_day": "morning|afternoon|evening|night",
 "indoor_outdoor": "indoor|outdoor|mixed"}}

formality: 1 (very casual) to 5 (very formal/bridal).
If there are no events, use exactly: occasion "no plans, regular day at home",
formality 1, time_of_day "morning", indoor_outdoor "indoor".

Calendar:
{calendar_text}"""


def get_context() -> OccasionContext:
    calendar_text = get_tomorrow_calendar_text()
    model = ChatOllama(model="llama3.2", temperature=0)
    response = model.invoke(CLASSIFY_PROMPT.format(calendar_text=calendar_text))
    raw = response.content
    start, end = raw.find("{"), raw.rfind("}")
    data = json.loads(raw[start : end + 1])
    return OccasionContext(**data)


if __name__ == "__main__":
    print(get_context())
