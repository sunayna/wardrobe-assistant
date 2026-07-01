# Roadmap

Each item below is one feature, built and committed on its own. Ordered so the
highest-uncertainty, most foundational work happens early, and nothing is built before
the data it needs exists. See SPEC.md for the full design behind each step.

- [x] **0. Project scaffold** — repo, `.gitignore`, `SPEC.md`, `db.py` with `sarees` +
      `wear_history` tables.

- [ ] **1. Google Cloud / API auth setup** — create/rename the Cloud project, enable
      Calendar API, Gmail API, Photos Picker API, configure the OAuth consent screen
      (External, Testing, self as test user), create a Desktop OAuth Client ID. Mostly
      manual console work plus a small auth helper for storing/reusing tokens.
      Prerequisite for features 2 and 3.

- [ ] **2. Photo ingestion** — Picker API flow (you select the album) → Claude vision
      call per photo → writes rows into `sarees`. The actual foundation of the
      catalog, and the highest-uncertainty piece (Picker API flow, how well
      vision-tagging works on real sarees) — worth de-risking early. Depends on
      feature 1.

- [ ] **3. Weather step** — call a weather API for tomorrow in Gurgaon, map forecast to
      `{recommended_fabrics, avoid_fabrics}`. No Google auth needed, low uncertainty —
      safe to slot in whenever, doesn't block anything on its own.

- [ ] **4. Context step** — LangChain agent with `calendar_tool` + `email_tool`;
      decides on its own whether to check email; outputs
      `{occasion, formality, time_of_day, indoor/outdoor}`. Depends on feature 1.

- [ ] **5. Wardrobe query step** — SQLite query filtered by occasion + fabric + season,
      excludes recently worn/recommended, retries with a relaxed window if empty.
      Depends on features 2 and 3 for real data to query against.

- [ ] **6. Ranking step** — single Claude call ranks candidates on occasion fit +
      weather fit + freshness, returns top pick + 2 alternates with reasoning.
      Depends on features 3, 4, 5 for real inputs.

- [ ] **7. Confirm step** — chat-driven update of `wear_history` (did you wear
      yesterday's pick?), runs first in every session per SPEC.md's ordering.

- [ ] **8. End-to-end wiring** — chat entry point that runs
      `confirm_today() → context() → weather() → query_wardrobe() → rank() → deliver()`
      as one flow. Ties features 1–7 together into the actual assistant.
