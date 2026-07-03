# Roadmap

Each item below is one feature, built and committed on its own. Ordered so the
highest-uncertainty, most foundational work happens early, and nothing is built before
the data it needs exists. See SPEC.md for the full design behind each step.

- [x] **0. Project scaffold** — repo, `.gitignore`, `SPEC.md`, `db.py` with `sarees` +
      `wear_history` tables.

- [x] **1. Google Cloud / API auth setup** — project created, Calendar API, Gmail API,
      and Photos Picker API enabled, OAuth consent screen configured (External,
      Testing, self as test user), Desktop OAuth Client ID created. `google_auth.py`
      handles the auth flow and token caching — verified working against both the
      Calendar and Gmail APIs. (Photos Picker uses its own session-based flow, reusing
      this same OAuth client — exercised when feature 2 is built.) Also hit and fixed
      a local machine issue: a network proxy re-signs HTTPS with a self-signed cert,
      breaking Python's default CA trust — `setup_ssl_trust.sh` patches the venv to
      trust it (re-run after any venv rebuild).

- [~] **2. Photo ingestion** — Picker API flow (you select the album) → Gemini
      free-tier vision call per photo → writes rows into `sarees`. Code complete and
      working; data population is ongoing — Gemini's real free-tier quota turned out
      to be 20 req/day (not the 1,000 advertised), so tagging all ~99 photos spreads
      across several days. Already-tagged photos are skipped automatically on re-run.
      Quota resets at midnight Pacific = **1:30 PM IST**, not midnight India time -
      running before that reconnects to the still-exhausted previous window. 16/99
      tagged so far.

- [x] **3. Weather step** — Open-Meteo (free, no API key) for tomorrow's forecast in
      Gurgaon, mapped to `{recommended_fabrics, avoid_fabrics}` via
      temperature/humidity/rain thresholds. No Google auth needed, no LLM needed.

- [x] **4. Context step** — originally planned as a LangChain agent deciding on its
      own whether to check email for ambiguous events. Simplified: no funded
      Anthropic account, so this runs on local `llama3.2`, which wasn't reliable
      enough at multi-step tool-use (skipped the calendar, called email with a
      garbage query in testing). Now a plain deterministic calendar fetch + one
      non-agentic classification call → `{occasion, formality, time_of_day,
      indoor_outdoor}`. Known cost: ambiguous events (e.g. "Dinner at Priya's") get a
      guessed formality instead of a disambiguated one. See SPEC.md.

- [x] **5. Wardrobe query step** — SQLite query filtered by formality closeness (±1,
      widened if needed) and avoid-fabrics (hard filter, substring match against
      weather's list), excludes sarees worn/recommended within a 14-day window.
      Relaxes the window first (14 → 7 → 0 days), then formality tolerance, if the
      pool comes up empty. Season filtering skipped for now - weather's fabric
      recommendations already capture most of that signal. Tested end-to-end against
      real context/weather output and the 16 tagged sarees so far - correctly
      filtered to 3 cotton candidates for a hot/humid/rainy casual day.

- [ ] **6. Ranking step** — single LLM call ranks candidates on occasion fit +
      weather fit + freshness, returns top pick + 2 alternates with reasoning. Model
      choice still open (see SPEC.md) — no funded Anthropic account.
      Depends on features 3, 4, 5 for real inputs.

- [ ] **7. Confirm step** — chat-driven update of `wear_history` (did you wear
      yesterday's pick?), runs first in every session per SPEC.md's ordering.

- [ ] **8. End-to-end wiring** — chat entry point that runs
      `confirm_today() → context() → weather() → query_wardrobe() → rank() → deliver()`
      as one flow. Ties features 1–7 together into the actual assistant.
