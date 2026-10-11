# EGMIS Digital Quiz System V24.4 — Turso Integration Preparation (Phase 1)

## Purpose

This is a safe preparation kit for the V24.4 Bulk Registration UI Refresh patch. It does **not** replace `app.py`, migrate any records, or switch the live app to Turso.

## Findings from the supplied V24.4 patch archive

The supplied ZIP is an overlay patch, not a complete application. It contains `app.py`, `start_lan.py`, and selected templates, but not the complete template/static tree or `quiz.db` / `analytics.duckdb`. The supplied `app.py` currently opens the primary database with `sqlite3.connect(DB)`, sets `sqlite3.Row`, runs SQLite PRAGMAs, uses `executescript()`, and performs schema upgrades with `PRAGMA table_info(...)`. Therefore, a Turso integration must account for the existing DB-API behavior and schema initialization; changing only a URL would not be sufficient.

## Recommended target architecture

- **Hosted/public mode:** the hosted Flask/Waitress application connects to one shared Turso database. All students and teachers use the same deployed application URL, and all application instances use the same Turso database URL/token.
- **LAN/offline mode:** retain the existing local SQLite database as a deliberately separate offline mode. Do not silently switch to local SQLite when Turso is unavailable, because online and offline writes would split into two divergent databases.
- **Analytics:** keep DuckDB local initially. Analytics failures must never block quiz submissions. A later phase can synchronize completed-attempt records into analytics after the main database migration has been validated.
- **Secrets:** load `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` from environment variables / hosting secret settings. Never commit a real token to source control or put it in a ZIP.

## Python driver note

Current Turso Python documentation describes `turso_serverless` as a remote HTTP DB-API driver for Turso databases. The smoke test in this kit uses it only to run `SELECT 1`. That verifies network/authentication/driver connectivity, not compatibility of the existing quiz app or migration correctness. Do not install an older `libsql-experimental` package based on old tutorials; Turso's newer documentation distinguishes current SDKs from deprecated packages.

## Phase 1 — connection smoke test (no app changes)

1. Back up the entire V24.4 project folder, including `quiz.db`, `analytics.duckdb`, `static`, `templates`, and launcher files.
2. Use Python 3.14 in a new test environment if that is the version on the deployment laptop. Do not replace the working environment.
3. Install the driver:

   ```powershell
   py -3.14 -m pip install turso_serverless
   ```

4. Set credentials in the current PowerShell session. Use your real URL and token locally; never paste the token into chat:

   ```powershell
   $env:TURSO_DATABASE_URL = 'libsql://YOUR-DATABASE-HOST'
   $env:TURSO_AUTH_TOKEN = 'PASTE-TOKEN-ONLY-IN-THIS-LOCAL-SESSION'
   ```

5. Run the smoke test:

   ```powershell
   py -3.14 .\turso_smoke_test.py
   ```

6. Expected result: `TURSO CONNECTION OK`. If it fails, the script reports a short error without printing the token.
7. Close the PowerShell window when done to clear these session-scoped variables.

## Phase 2 — required before switching the app

The full base project is needed to build and test the actual integration patch. The current overlay archive is not enough to guarantee a drop-in replacement because it lacks other application files and the live databases. Once the full project is available, the implementation must:

- add an explicit `QUIZ_DB_MODE=local|turso` setting;
- preserve the current SQLite connection path for LAN/offline mode;
- implement and test a compatibility layer for row mapping, errors, transactions, `lastrowid`, multi-statement schema creation, and schema introspection;
- move schema creation/upgrades to a one-time, explicit migration step rather than blindly running local-SQLite PRAGMAs on every hosted startup;
- migrate data from a copy of `quiz.db`, verify table counts and key relationships, and retain the original unchanged;
- test login, bulk registration, quiz creation, scheduled availability, attempts, submissions, result review, admin/teacher filters, and repeated submissions;
- simulate concurrent student submissions before schoolwide rollout;
- ensure a Turso outage fails safely with a clear message rather than silently writing to a different local database.

## Important

Do not point the live app at the new Turso database until schema/data migration and functional tests pass. The existing `quiz.db` remains the source of truth until the migration is explicitly verified. Never upload the database auth token or include it in the project ZIP.
