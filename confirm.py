import datetime
import sqlite3

from db import get_connection


def get_pending_recommendation() -> dict | None:
    """Returns the most recently recommended saree if it hasn't been confirmed yet
    (wear_history.last_worn_date doesn't already match last_recommended_date) AND
    that date has actually arrived - a recommendation for tomorrow isn't confirmable
    yet, so it shouldn't be asked about until tomorrow itself. Returns None if
    there's nothing pending."""
    today = datetime.date.today().isoformat()
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT s.photo_id, s.fabric, s.color, w.last_recommended_date, w.last_worn_date
        FROM wear_history w
        JOIN sarees s ON s.photo_id = w.photo_id
        WHERE w.last_recommended_date IS NOT NULL
          AND w.last_recommended_date <= ?
          AND (w.last_worn_date IS NULL OR w.last_worn_date != w.last_recommended_date)
        ORDER BY w.last_recommended_date DESC
        LIMIT 1
        """,
        (today,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_worn(photo_id: str, worn_date: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE wear_history SET last_worn_date = ?, wear_count = wear_count + 1 WHERE photo_id = ?",
        (worn_date, photo_id),
    )
    conn.commit()
    conn.close()


def record_recommendation(photo_id: str, date: str) -> None:
    """Called by the output step once a saree is recommended - this is what
    confirm_today() resolves on the next run. Upserts since a saree could already
    have a wear_history row from a previous cycle."""
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO wear_history (photo_id, last_recommended_date, wear_count)
        VALUES (?, ?, 0)
        ON CONFLICT(photo_id) DO UPDATE SET last_recommended_date = excluded.last_recommended_date
        """,
        (photo_id, date),
    )
    conn.commit()
    conn.close()


def confirm_today() -> None:
    pending = get_pending_recommendation()
    if pending is None:
        return  # nothing outstanding, don't prompt

    answer = input(
        f"Did you wear the {pending['fabric']} ({pending['color']}) saree recommended "
        f"for {pending['last_recommended_date']}? (y/n) "
    ).strip().lower()

    if answer.startswith("y"):
        mark_worn(pending["photo_id"], pending["last_recommended_date"])
        print("Got it, marked as worn.")
    else:
        print("Okay, noted - not marking it as worn.")


if __name__ == "__main__":
    confirm_today()
