import datetime

from db import get_connection


def add_profile_note(trigger_text: str, correction: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO profile_notes (trigger_text, correction, created_at) VALUES (?, ?, ?)",
        (trigger_text, correction, datetime.date.today().isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_profile_notes() -> list[tuple[str, str]]:
    conn = get_connection()
    rows = conn.execute("SELECT trigger_text, correction FROM profile_notes ORDER BY id").fetchall()
    conn.close()
    return rows


def get_profile_context() -> str:
    """Formatted for direct inclusion in the classification prompt. Empty string if
    nothing's been taught yet, so callers can skip the section entirely."""
    rows = get_all_profile_notes()
    if not rows:
        return ""
    lines = [f'- When plans mentioned "{trigger}", that actually meant: {correction}' for trigger, correction in rows]
    return "Known context about you, from past corrections:\n" + "\n".join(lines)


def find_known_correction(calendar_text: str) -> str | None:
    """Deterministic substring match against past corrections - used to skip asking
    again when we already have a direct answer, rather than trusting the LLM to
    honor an "already clarified" instruction in a prompt (it doesn't reliably)."""
    lower = calendar_text.lower()
    for trigger_text, correction in get_all_profile_notes():
        if trigger_text.lower() in lower:
            return correction
    return None
