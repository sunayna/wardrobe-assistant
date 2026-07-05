import datetime

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Tried in order - %Y-%m-%d first so an explicit year takes priority over
# accidentally matching a shorter format.
DATE_FORMATS = [
    "%Y-%m-%d", "%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y",
    "%d %B", "%B %d", "%d %b", "%b %d", "%d/%m/%Y", "%d/%m",
]


def parse_target_date(text: str, today: datetime.date | None = None) -> datetime.date | None:
    """Handles 'today', 'tomorrow', weekday names ('next Saturday', 'this Friday',
    plain 'Saturday' - all treated the same, meaning the next upcoming occurrence),
    and a handful of common explicit date formats. Returns None if nothing matched."""
    today = today or datetime.date.today()
    raw = text.strip()
    lower = raw.lower()

    if lower == "today":
        return today
    if lower == "tomorrow":
        return today + datetime.timedelta(days=1)

    for weekday_name, weekday_num in WEEKDAYS.items():
        if weekday_name in lower:
            days_ahead = (weekday_num - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # "Saturday" when today IS Saturday means next week
            return today + datetime.timedelta(days=days_ahead)

    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
        return parsed

    return None
