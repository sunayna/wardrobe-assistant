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
      running before that reconnects to the still-exhausted previous window. Now also
      saves each photo locally (`photos/`) at ingestion time — the Picker API's
      baseUrl only works within its original session, so this is the only point the
      image bytes are ever reachable; needed for feature 9's photo-in-chat replies.
      30/99 tagged so far.

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

- [x] **6. Ranking step** — single local `llama3.2` call ranks candidates on
      occasion fit + weather fit + freshness, returns top pick + 2 alternates with
      reasoning. Had to drop JSON output entirely — llama3.2 invented its own broken
      nested structure when asked for it. Switched to a flat, labeled plain-text
      format (`TOP: <n>`, `TOP_REASON: ...`, etc.), parsed with simple line matching
      instead. Tested end-to-end against real data — sensible top pick + alternates
      with reasonable justifications.

- [x] **7. Confirm step** — finds the most recent unresolved recommendation (a
      `wear_history` row whose `last_recommended_date` doesn't yet match
      `last_worn_date`), asks whether you wore it, updates `last_worn_date` /
      `wear_count` if yes. No-op if nothing's pending. Also adds
      `record_recommendation()`, the write side of the same lifecycle — used by
      feature 8's output step. Tested both the yes and no paths against seeded data.

- [x] **8. End-to-end wiring** — `main.py` runs
      `confirm_today() → context() → weather() → query_wardrobe() → rank() → deliver()`
      as one flow, then `record_recommendation()`s the pick for tomorrow. Ties
      features 1–7 together into the actual assistant. Found and fixed a real bug
      while testing: `confirm_today()` was willing to ask about a recommendation for
      a date that hadn't arrived yet (e.g. asking "did you wear it" about tomorrow's
      pick, today) — added a check that the recommended date has actually passed
      before it's confirmable. Ran the full pipeline for real and got a sensible
      end-to-end recommendation.

- [x] **9. Telegram bot interface** — `telegram_bot.py`, a plain `requests`-based
      long-polling bot (no bot framework — consistent with this project's style of
      direct API calls). Chose Telegram over a hosted web app (e.g. Vercel)
      specifically to avoid undoing this project's "free and local" architecture —
      a cloud deployment would need to move the local Ollama models and SQLite DB
      off-device. `/wardrobe` runs the full pipeline conversationally (asks for
      calendar/confirm answers as chat messages instead of terminal `input()`);
      `/more` shows another candidate from today's already-filtered pool without
      recomputing anything; `/correct [n]` lets you fix a wrong tag on the nth
      saree shown this session. Sends the actual saree photo (not just text) using
      the local files saved during ingestion. Auto-starts via a macOS `launchd`
      agent (`~/Library/LaunchAgents/com.wardrobeassistant.telegrambot.plist`), so
      it runs continuously without a manually-kept-open terminal.

      Required refactoring `context.py` (`get_tomorrow_calendar_text` /
      `default_calendar_text` / `classify_context` split into reusable pieces, since
      the CLI's blocking `input()` doesn't map onto a bot's "ask now, get the reply
      as a separate incoming message later" flow).

      Two real bugs found while building this: (1) this network's proxy cuts
      long-held HTTPS connections, causing repeated read timeouts on Telegram's
      default 30s long-poll — fixed by shortening it to 8s; (2) Telegram's typing
      indicator only lasts ~5s, so a single ping went silent during longer LLM
      calls — fixed with a background thread that refreshes it every 4s for the
      duration of the actual work, plus explicit "thinking" text messages so it's
      never silently unclear that a response is coming.

- [x] **10. Multi-day planning + bot UX polish**
      - `/plan` — like `/wardrobe` but for an arbitrary future date + a directly
        stated occasion, instead of always tomorrow + whatever's on the calendar
        (e.g. "what should I wear for the wedding next Saturday"). Added
        `dateparse.py` (pure stdlib, no new dependency) handling weekday names
        ("next Saturday", "this Friday" — treated the same, both meaning the next
        upcoming occurrence), "today"/"tomorrow", and common explicit date formats.
        `weather.py`'s `get_weather_constraints()` now takes an optional
        `target_date` (Open-Meteo forecasts up to 16 days out, plenty for this).
        Refactored the query→rank→deliver→record sequence out of `run_wardrobe_flow`
        into a shared `recommend_and_deliver()` so `/wardrobe` and `/plan` don't
        duplicate that logic.
      - Reply-keyboard buttons (Yes/No for confirm, a persistent menu of
        `/wardrobe /plan /more /correct /help` after every flow ends) instead of
        expecting typed commands - addresses "how do I keep track of all these
        commands."
      - Registered commands with Telegram's `setMyCommands` so they also show in
        the client's own `/` autocomplete, plus a `/help` command as a backup.
      - Redesigned `/correct`: was crude (typed `field: value` text, and the menu
        button always defaulted to correcting option 1 since it sends bare
        `/correct` with no index). Now a proper step-by-step button flow — pick
        which saree (buttons show fabric/color for each), pick which field
        (buttons), then just type the new value.
      - `/clrscr` — clears the bot's own messages from the chat. Real platform
        limit: Telegram never lets a bot delete messages the *user* sent in a
        private chat, only its own — the command is upfront about that.
      - Generic/unrecognized messages now get a friendly button prompt ("here's
        what I can help with") instead of being silently dropped.
