# wardrobe-assistant

A personal assistant that recommends which saree to wear tomorrow, based on your
calendar (occasion), the weather (fabric suitability), and your wear history
(avoiding repeats).

**Status**: in progress. See [ROADMAP.md](ROADMAP.md) for what's built vs. planned,
and [SPEC.md](SPEC.md) for the full design and the reasoning behind it.

## Setup

1. Clone the repo and create a virtualenv:
   ```
   python3 -m venv venv
   ```
2. If pip or Python HTTPS calls fail with `CERTIFICATE_VERIFY_FAILED` (common behind
   a corporate/network proxy that re-signs traffic), run:
   ```
   ./setup_ssl_trust.sh
   ```
3. Install dependencies:
   ```
   ./venv/bin/pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in:
   - `GEMINI_API_KEY` — free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey), used for photo tagging during ingestion.
   - `ANTHROPIC_API_KEY` — only needed if you have a funded Anthropic account; currently unused (see SPEC.md).
5. Google Calendar/Gmail/Photos access needs a `credentials.json` (OAuth Desktop
   client) from [console.cloud.google.com](https://console.cloud.google.com) — see
   SPEC.md's "Open decisions" section for the exact steps. The first run of any
   script that touches Google APIs will open a browser link for you to authorize;
   the resulting token is cached in `token.json`.
6. Local vision/reasoning models run via [Ollama](https://ollama.com) — install it,
   then `ollama pull llama3.2`.

## Usage

```
./venv/bin/python main.py
```

Confirms whether you wore the last recommendation (if one's due), figures out
tomorrow's occasion from your calendar, checks the weather, filters your catalog,
and prints a top pick + two alternates with reasoning. Run it once a day.

## Other scripts

- `./venv/bin/python db.py` — initializes `wardrobe.db` (SQLite) with the `sarees`
  and `wear_history` tables.
- `./venv/bin/python ingest.py` — walks you through selecting your saree photo
  album (Google Photos Picker), tags each photo, and stores results in `sarees`.
  Free-tier quota limits this to ~20 photos/day — re-run daily until your catalog
  is fully tagged; already-tagged photos are skipped automatically.
- `./venv/bin/python weather.py`, `context.py`, `wardrobe.py`, `ranking.py`,
  `confirm.py` — the individual pipeline steps `main.py` wires together; each is
  runnable on its own for testing.
