"""Render entry point: validate secrets, initialize Turso schema, then start Gunicorn."""
import os
import sys

os.environ.setdefault('QUIZ_DB_MODE', 'turso')
os.environ.setdefault('QUIZ_ACCESS_MODE', 'public')

if os.environ.get('QUIZ_DB_MODE', '').strip().lower() != 'turso':
    raise SystemExit('Render deployment requires QUIZ_DB_MODE=turso; refusing local SQLite fallback.')
for key in ('TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN'):
    if not os.environ.get(key, '').strip():
        raise SystemExit(f'Missing required Render environment variable: {key}')
secret = os.environ.get('QUIZ_SECRET', '').strip()
if len(secret) < 32 or secret == 'tle-css-quiz-v4-change-this':
    raise SystemExit('Set QUIZ_SECRET to a private random value of at least 32 characters in Render environment settings.')

from app import init_db
init_db()

port = os.environ.get('PORT', '10000')
argv = ['gunicorn', '--bind', f'0.0.0.0:{port}', '--workers', '1', '--threads', '8',
        '--timeout', '120', '--graceful-timeout', '30', '--access-logfile', '-',
        '--error-logfile', '-', 'app:app']
os.execvp(argv[0], argv)
