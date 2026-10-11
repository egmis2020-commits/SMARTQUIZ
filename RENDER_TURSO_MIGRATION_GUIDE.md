# EGMIS TLE-CSS Quiz V24.4 — Render + Turso migration guide

## Important status

This package adds an explicit `QUIZ_DB_MODE=local|turso` selector, a Turso compatibility adapter, a SQLite-to-Turso copy utility, and a Render startup entry point. Local/LAN mode remains the default. **This package has not been verified against your live Turso account in this workspace**, because your credentials are private and are not available here. Do not schedule the ADL exam until the live-driver and end-to-end tests below pass.

## Keep your LAN version

Do not replace or delete the working classroom folder. Use this extracted folder as a separate branch/copy for Render. `QUIZ_DB_MODE=local` continues to use `quiz.db`; `QUIZ_DB_MODE=turso` requires both Turso environment variables and fails instead of silently falling back to local SQLite.

## Local prerequisites

Use Python 3.11+ in a new virtual environment. Install `requirements.txt`. The file includes Gunicorn and `turso_serverless` for the hosted database adapter. Do not commit your Turso token, `QUIZ_SECRET`, `quiz.db`, or `analytics.duckdb`. The migration ZIP omits both databases on purpose.

## First migration — be careful

1. Make a backup copy of the whole project, including `quiz.db`, `analytics.duckdb`, `static/uploads`, and templates.
2. Set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` as environment variables in your local terminal; do not put the token in source files or chat.
3. Run a dry connectivity check: `python turso_smoke_test.py`.
4. The migration ZIP intentionally does **not** contain `quiz.db` or `analytics.duckdb`, to avoid exposing student records if the code is pushed to GitHub. Run the migration script using the path to your private original database, for example `python migrate_sqlite_to_turso.py --source "C:\EGMIS_PRIVATE\quiz.db"`. The script creates/upgrades the remote schema, replaces only the starter rows created by schema initialization, copies the application tables, and verifies row counts. If the target already has application data, it stops unless `--replace-existing` is supplied. That flag clears existing target quiz data; use it only when you explicitly intend that.
5. Compare the printed counts with the source SQLite table counts. Row counts alone do not prove correctness; test the records and workflows below.

## Render settings

- **Root Directory:** the directory containing `app.py`, `render_start.py`, and `requirements.txt`.
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python render_start.py`
- **Environment variables:**
  - `QUIZ_DB_MODE` = `turso`
  - `QUIZ_ACCESS_MODE` = `public`
  - `TURSO_DATABASE_URL` = your Turso URL (secret/private)
  - `TURSO_AUTH_TOKEN` = your Turso token (secret/private)
  - `QUIZ_SECRET` = a private random value at least 32 characters long
  - `QUIZ_PUBLIC_URL` = your actual Render service URL, such as `https://your-service.onrender.com`

The startup command fails if Turso credentials or a strong `QUIZ_SECRET` are missing. It initializes the schema and then starts one Gunicorn worker with eight threads. This conservative configuration avoids some process-level race conditions; it is not a promise that 600 simultaneous students will be handled by a Free instance.

## Mandatory acceptance tests before ADL

- Confirm Render logs show successful startup without database errors.
- Login as admin and a student; verify passwords and roles.
- Bulk-register a small test batch and verify no duplicate accounts.
- Create/edit a quiz and confirm scheduled availability.
- Start and submit test attempts from multiple devices; check duplicate-submit behavior, score, answer review, and attempt limits.
- Check teacher/admin filters, exports, audit logs, and images.
- Test with realistic concurrent users in a separate test quiz/database and inspect errors, latency, and Turso usage limits.
- Verify that if Turso credentials are wrong or Turso is unavailable, the app fails clearly instead of writing to local SQLite.

## Production capacity

Render Free can sleep or be resource-limited and should not be treated as a guaranteed always-on exam host. A 600-student simultaneous start requires a staged load test and a production deployment sized for the observed workload. Turso is the shared database, but it does not remove application CPU, request-queue, rate-limit, or transaction-contention limits.
