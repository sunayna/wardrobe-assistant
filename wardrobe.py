import datetime
import sqlite3

from db import get_connection

DEFAULT_WINDOW_DAYS = 14


def _fabric_ok(fabric: str | None, avoid_fabrics: list[str]) -> bool:
    if not fabric:
        return True
    fabric_lower = fabric.lower()
    return not any(avoid.lower() in fabric_lower for avoid in avoid_fabrics)


def _query(occasion_formality: int, avoid_fabrics: list[str], window_days: int, formality_tolerance: int) -> list[dict]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()

    rows = conn.execute(
        """
        SELECT s.*, w.last_worn_date, w.last_recommended_date, w.wear_count
        FROM sarees s
        LEFT JOIN wear_history w ON s.photo_id = w.photo_id
        WHERE (w.last_worn_date IS NULL OR w.last_worn_date < ?)
          AND (w.last_recommended_date IS NULL OR w.last_recommended_date < ?)
          AND ABS(s.formality - ?) <= ?
        """,
        (cutoff, cutoff, occasion_formality, formality_tolerance),
    ).fetchall()
    conn.close()

    return [dict(r) for r in rows if _fabric_ok(r["fabric"], avoid_fabrics)]


def query_wardrobe(occasion_formality: int, avoid_fabrics: list[str]) -> list[dict]:
    """Filters by formality closeness and avoid-fabrics (hard filter), excluding
    sarees worn/recommended within the repeat-avoidance window. If the pool comes up
    empty, relaxes the window first (14 -> 7 -> 0 days), then - if still empty even
    with no window at all - widens formality tolerance as a last resort, so this
    never just fails outright."""
    for window_days in (DEFAULT_WINDOW_DAYS, 7, 0):
        candidates = _query(occasion_formality, avoid_fabrics, window_days, formality_tolerance=1)
        if candidates:
            return candidates

    for tolerance in (2, 3, 5):
        candidates = _query(occasion_formality, avoid_fabrics, window_days=0, formality_tolerance=tolerance)
        if candidates:
            return candidates

    return []


if __name__ == "__main__":
    from context import get_context
    from weather import get_weather_constraints

    occasion_ctx = get_context()
    weather_ctx = get_weather_constraints()
    print("Context:", occasion_ctx)
    print("Weather:", weather_ctx)

    candidates = query_wardrobe(occasion_ctx.formality, weather_ctx["avoid_fabrics"])
    print(f"\n{len(candidates)} candidate(s):")
    for c in candidates:
        print(f"  {c['fabric']} ({c['color']}) formality={c['formality']} tags={c['occasion_tags']}")
