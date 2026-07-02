# Wardrobe Assistant — Spec

## Goal
Given "tomorrow," recommend which saree to wear, using calendar/email context (occasion),
weather (fabric suitability), and wardrobe history (avoid repeating recently-worn sarees).

## Data source: saree catalog
Source of truth is a **Google Photos album** (photos only, no metadata today).

- **Phase 0 — Ingestion (one-time, then periodic, interactive):**
  Google removed silent/background read access to Photos libraries in March 2025 — the
  old Library API read scopes now hard-fail. The replacement, the **Picker API**, is
  interactive by design: you open a picker link and manually select the album/photos
  in a Google-hosted UI each time, then the script fetches just what you selected and
  runs it through Gemini's free tier (`gemini-2.5-flash-lite`) to extract structured
  tags into SQLite — free, no local compute. (Originally tried a local Ollama vision
  model instead of any API — free and fully local, but this machine only has 8GB RAM,
  and a 4.7GB model running 99 back-to-back inferences swap-thrashed badly enough to
  break DNS resolution and make the whole system hang. Moved tagging off-device to
  Gemini's free tier instead.) This fits fine with how ingestion already worked
  (occasional, not automatic) — it just means "re-run ingestion" is always a "you
  click through a picker" action, never a silent background scan.

  **Real quota discovered in practice**: despite docs advertising 1,000 req/day for
  this model, a freshly created Cloud project's actual free-tier quota was only
  **20 requests/day** (`generate_content_free_tier_requests`, likely because the
  project hadn't "warmed up"). For a ~99-photo catalog this means ingestion now
  naturally spreads across ~5 days — `ingest.py` tracks already-tagged photos and
  skips them, and stops cleanly (rather than burning retries) when it detects the
  daily-quota error, so you just re-run it once a day until the catalog is complete.

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
   recommendation outstanding.

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

5. **Output** — delivered as a chat reply (see Triggers). Writes
   `last_recommended_date` to `wear_history` for the chosen `photo_id` — this becomes
   tomorrow's pending confirmation, resolved by step 0 of the next run.

Straight-line script: `confirm_today() → context() → weather() → query_wardrobe() →
rank() → deliver()`, called in order, no pausing or resuming.

## Triggers

- **On-demand only**: you ask in chat ("what should I wear tomorrow?"), the script
  runs and replies. No unattended scheduled job.
- Why: Gmail/Calendar/Photos access all count as Google "sensitive" scopes, and an
  app in Google Cloud's default "Testing" mode gets its refresh tokens auto-expired
  after 7 days. A silent daily cron job would break weekly needing a browser
  re-auth nobody's watching for. Since you're interacting on-demand anyway, if a
  token has expired it just prompts a re-auth right there instead of failing silently
  in the background. Getting non-expiring tokens would require Google's app
  verification review (privacy policy, possibly a demo video, weeks of turnaround) —
  not worth it for a personal single-user script right now.

## Tech stack

- **LangChain** — Google Calendar / Gmail tool wrappers, `ChatAnthropic` model wrapper,
  and the agent construct for the one tool-calling step (context step). Photos access
  goes through the separate interactive **Picker API** flow (not a LangChain wrapper —
  it's a user-driven picker link, not something an agent calls as a tool).
- **Claude** (via Anthropic API) — text reasoning only (context/ranking steps). Needs a
  real `ANTHROPIC_API_KEY` in `.env` (still a placeholder) before those steps are built.
- **Gemini free tier (`gemini-2.5-flash-lite`)** — vision tagging for ingestion. Free,
  1,000 requests/day, needs a `GEMINI_API_KEY` in `.env` (a plain API key from Google
  AI Studio, not the OAuth client used for Calendar/Gmail/Photos). Kept separate from
  the Claude reasoning steps since ingestion is a bulk job (one call per photo) and the
  reasoning steps are low-volume (one or two calls a day) — no reason to put the bulk
  job on a paid API, and no reason to put it on this machine's limited local compute
  either (see above).
- **SQLite** — saree catalog + wear history.
- **Weather API** — TBD (e.g. OpenWeatherMap).
- No LangGraph for now — revisit only if the retry loop or confirmation step outgrow
  plain Python (see earlier discussion: LangGraph's main value here would've been the
  retry loop and pause/resume for confirmation, neither of which need a framework at
  this scale).

## Open decisions (not yet settled)

- Weather API choice (location settled: **Gurgaon**, fixed — not derived from calendar,
  since travel-day handling isn't in scope yet).
- Repeat-avoidance window length (N days) — starting guess, tune later.
- Since vision-tagging your sarees is a guess (fabric especially is hard to tell from a
  photo), do you want a review/correction step after Phase 0 ingestion, or trust the
  auto-tags as-is?
- Google API auth setup (OAuth credentials for Calendar/Gmail/Photos) — not yet
  created. Project currently sits as the default "My First Project" in Google Cloud,
  no APIs enabled, no OAuth client yet. Since we're staying in Testing mode (see
  Triggers), setup is: create/rename a project → enable Calendar API, Gmail API,
  Photos Picker API → configure OAuth consent screen (External, Testing, add your own
  account as a test user) → create an OAuth Client ID (Desktop app type).
