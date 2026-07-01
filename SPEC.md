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

- **Storage (SQLite), two tables:**
  ```
  sarees(                              -- catalog, written only by Phase 0 ingestion
    photo_id TEXT PRIMARY KEY,
    fabric TEXT, weight TEXT, color TEXT,
    occasion_tags TEXT, formality INTEGER, season TEXT
  )

  wear_history(                        -- written only by the confirm/output steps
    photo_id TEXT PRIMARY KEY REFERENCES sarees(photo_id),
    last_worn_date DATE, wear_count INTEGER,
    last_recommended_date DATE
  )
  ```
  Kept separate since they're written by different, unrelated steps (tagging vs.
  confirmation) and change at different rates. The daily pipeline reads both, joined on
  `photo_id`.

## Daily pipeline (plain Python + LangChain)

No LangGraph for now — this is a fixed sequence of plain Python function calls, run
top to bottom, no orchestration framework needed. Only one step needs LLM-driven
decision-making, and that's handled with a LangChain agent (tool-calling loop), not a
graph.

Every run does two things in order: **close out today, then plan tomorrow.** If it
generated tomorrow's pick first, that ranking would be working off yesterday's
unconfirmed guess instead of what you actually wore today — so confirmation always
runs first.

0. **Confirm step (plain function, first)** — resolves whatever was recommended in the
   previous run: did you wear it? Updates `wear_history.last_worn_date` / `wear_count`
   for that `photo_id`. Runs before anything else, so every later step sees accurate
   wear data, not just a pending recommendation. Only prompts if there's an unconfirmed
   recommendation outstanding — the on-demand trigger won't double-ask if the scheduled
   run already confirmed today.

1. **Context step (LangChain agent)** — a small LangChain agent with `calendar_tool`
   and `email_tool`. It always checks tomorrow's calendar; it *decides on its own*
   whether the event is ambiguous enough to also search email (e.g. "Dinner at
   Priya's" → check email; "Office all-hands, formal attire" → skip email). Output:
   structured `{occasion, formality, time_of_day, indoor/outdoor}`.

2. **Weather step (plain function)** — always runs, no LLM decision needed (forecast
   is always wanted). Calls weather API for tomorrow in **Gurgaon** → maps to
   `{recommended_fabrics, avoid_fabrics}`.

3. **Wardrobe query step (plain function, with retry)** — queries SQLite filtered by
   occasion + fabric fit + season, excluding sarees worn/recommended within the last
   N days. If the filtered pool is empty, loosen the window and re-query in a plain
   Python loop rather than fail outright.

4. **Stylist/ranking step (LLM call)** — a single `ChatAnthropic` call ranks remaining
   candidates on occasion fit + weather fit + freshness (days since last worn), returns
   top pick + 2 alternates with reasoning.

5. **Output** — delivered via scheduled notification or chat reply (see Triggers).
   Writes `last_recommended_date` to `wear_history` for the chosen `photo_id` — this
   becomes tomorrow's pending confirmation, resolved by step 0 of the next run.

Straight-line script: `confirm_today() → context() → weather() → query_wardrobe() →
rank() → deliver()`, called in order, no pausing or resuming — every step runs to
completion within a single run, once a day.

## Triggers

- **Scheduled**: daily evening job runs the full script, sends the recommendation as a
  notification (email, most likely — TBD).
- **On-demand**: same script, callable from chat ("what should I wear tomorrow?").
- Both call the identical sequence of functions; only the entry point and output
  delivery differ.

## Tech stack

- **LangChain** — Google Calendar / Gmail / Photos tool wrappers, `ChatAnthropic` model
  wrapper, and the agent construct for the one tool-calling step (context step).
- **Claude** (via Anthropic API) — text reasoning (context/ranking steps) + vision
  (ingestion tagging). Note: current `agent.py` in this repo uses `ChatOllama`/local
  llama3.2 as a toy example — will need to switch to `ChatAnthropic` with a real API key
  for this project (`.env` currently only has `OPENAI_API_KEY` + a commented-out
  `ANTHROPIC_API_KEY`).
- **SQLite** — saree catalog + wear history.
- **Weather API** — TBD (e.g. OpenWeatherMap).
- No LangGraph for now — revisit only if the retry loop or confirmation step outgrow
  plain Python (see earlier discussion: LangGraph's main value here would've been the
  retry loop and pause/resume for confirmation, neither of which need a framework at
  this scale).

## Open decisions (not yet settled)

- Weather API choice (location settled: **Gurgaon**, fixed — not derived from calendar,
  since travel-day handling isn't in scope yet).
- Notification channel for the scheduled run (email / push / other).
- Repeat-avoidance window length (N days) — starting guess, tune later.
- Since vision-tagging your sarees is a guess (fabric especially is hard to tell from a
  photo), do you want a review/correction step after Phase 0 ingestion, or trust the
  auto-tags as-is?
- Google API auth setup (OAuth credentials for Calendar/Gmail/Photos) — not yet created.
