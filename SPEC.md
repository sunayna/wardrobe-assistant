# Wardrobe Assistant — Spec

## Goal
Given "tomorrow," recommend which saree to wear, using calendar/email context (occasion),
weather (fabric suitability), and wardrobe history (avoid repeating recently-worn sarees).

## Data source: saree catalog
Source of truth is a **Google Photos album** (photos only, no metadata today).

- **Phase 0 — Ingestion (one-time, then periodic):**
  Pull images from the Photos album via the Photos API → run each through a vision-capable
  Claude call → extract structured tags → write to SQLite. Re-run only against new photos
  (diff by `photo_id`) when the album changes, not on every daily run.

- **Storage (SQLite):**
  ```
  sarees(
    photo_id TEXT PRIMARY KEY,
    fabric TEXT, weight TEXT, color TEXT,
    occasion_tags TEXT, formality INTEGER, season TEXT,
    last_worn_date DATE, wear_count INTEGER,
    last_recommended_date DATE
  )
  ```
  This table (not the raw photos) is what every daily run queries.

## Daily pipeline (LangGraph)

Mix of fixed steps and one LLM-decided step — decided per-node by asking "does the right
action depend on data only visible at runtime?"

1. **Context node (agent-style)** — LLM has access to `calendar_tool` and `email_tool`.
   It always checks tomorrow's calendar; it *decides on its own* whether the event is
   ambiguous enough to also search email (e.g. "Dinner at Priya's" → check email;
   "Office all-hands, formal attire" → skip email). Output: structured
   `{occasion, formality, time_of_day, indoor/outdoor}`.

2. **Weather node (fixed)** — always runs, no LLM decision needed (forecast is always
   wanted). Calls weather API for tomorrow in **Gurgaon** → maps to
   `{recommended_fabrics, avoid_fabrics}`.

3. **Wardrobe query node (fixed, with retry loop)** — queries SQLite filtered by
   occasion + fabric fit + season, excluding sarees worn/recommended within the last
   N days. If the filtered pool is empty, loosen the window and re-query rather than
   fail outright.

4. **Stylist/ranking node** — LLM ranks remaining candidates on occasion fit + weather
   fit + freshness (days since last worn), returns top pick + 2 alternates with reasoning.

5. **Output** — delivered via scheduled notification or chat reply (see Triggers).

## Wear confirmation (Phase 2)

Photos/calendar have no signal for what you actually wore, so this needs a manual
confirmation loop:
- After recommending, the graph pauses (LangGraph interrupt/checkpoint).
- Next day, you confirm what you actually wore (or say you didn't go with the suggestion).
- On confirmation, `last_worn_date` / `wear_count` updates in SQLite for that `photo_id`.
- If unconfirmed, only `last_recommended_date` is set — repeat-avoidance still uses that
  as a weaker signal so the same pick isn't suggested again immediately.

## Triggers

- **Scheduled**: daily evening job runs the full pipeline, sends the recommendation as a
  notification (email, most likely — TBD).
- **On-demand**: same graph, callable from chat ("what should I wear tomorrow?").
- Both call the identical graph; only the entry point and output delivery differ.

## Tech stack

- **LangGraph** — orchestrates the pipeline (state + nodes + the one agent-style node).
- **LangChain** — Google Calendar / Gmail / Photos tool wrappers, `ChatAnthropic` model
  wrapper.
- **Claude** (via Anthropic API) — text reasoning (context/ranking nodes) + vision
  (ingestion tagging). Note: current `agent.py` in this repo uses `ChatOllama`/local
  llama3.2 as a toy example — will need to switch to `ChatAnthropic` with a real API key
  for this project (`.env` currently only has `OPENAI_API_KEY` + a commented-out
  `ANTHROPIC_API_KEY`).
- **SQLite** — saree catalog + wear history.
- **Weather API** — TBD (e.g. OpenWeatherMap).

## Open decisions (not yet settled)

- Weather API choice (location settled: **Gurgaon**, fixed — not derived from calendar,
  since travel-day handling isn't in scope yet).
- Notification channel for the scheduled run (email / push / other).
- Repeat-avoidance window length (N days) — starting guess, tune later.
- Since vision-tagging your sarees is a guess (fabric especially is hard to tell from a
  photo), do you want a review/correction step after Phase 0 ingestion, or trust the
  auto-tags as-is?
- Google API auth setup (OAuth credentials for Calendar/Gmail/Photos) — not yet created.
