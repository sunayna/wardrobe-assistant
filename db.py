import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "wardrobe.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sarees (
    photo_id TEXT PRIMARY KEY,
    fabric TEXT,
    weight TEXT,
    color TEXT,
    occasion_tags TEXT,
    formality INTEGER,
    season TEXT
);

CREATE TABLE IF NOT EXISTS wear_history (
    photo_id TEXT PRIMARY KEY REFERENCES sarees(photo_id),
    last_worn_date DATE,
    wear_count INTEGER DEFAULT 0,
    last_recommended_date DATE
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
