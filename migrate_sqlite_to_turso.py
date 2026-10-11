"""Copy EGMIS quiz.db data into Turso. Run once from the project folder.

This is a destructive target replacement only when --replace-existing is given.
Never pass or print the Turso auth token on the command line; use environment vars.
"""
import argparse
import os
import sqlite3
import sys

TABLES = ['users', 'quizzes', 'questions', 'attempts', 'audit_logs']
DELETE_ORDER = ['attempts', 'questions', 'audit_logs', 'quizzes', 'users']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='quiz.db', help='Path to the local SQLite quiz.db (default: ./quiz.db)')
    parser.add_argument('--replace-existing', action='store_true', help='Allow replacing all quiz data currently in the Turso target')
    args = parser.parse_args()
    os.environ['QUIZ_DB_MODE'] = 'turso'
    url = os.environ.get('TURSO_DATABASE_URL', '').strip()
    token = os.environ.get('TURSO_AUTH_TOKEN', '').strip()
    if not url or not token:
        print('ERROR: Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in your local terminal environment.', file=sys.stderr)
        return 2
    if not os.path.isfile(args.source):
        print(f'ERROR: Source SQLite file not found: {args.source}', file=sys.stderr)
        return 2

    from turso_compat import TursoConnection
    target = TursoConnection(url, token)
    # Determine whether the target already has real application data before schema init seeds defaults.
    target_has_data = False
    try:
        for table in TABLES:
            count = target.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            if count:
                target_has_data = True
                break
    except Exception:
        # A brand-new Turso database has no schema yet.
        target_has_data = False
    if target_has_data and not args.replace_existing:
        target.close()
        print('ERROR: Turso already contains data. No changes made. Re-run with --replace-existing only if you intend to replace it.', file=sys.stderr)
        return 3

    # Create/upgrade the remote schema with the same schema logic as the app.
    from app import init_db
    init_db()
    source = sqlite3.connect(args.source)
    source.row_factory = sqlite3.Row
    try:
        # Clear dependent tables first, then import in parent-before-child order.
        for table in DELETE_ORDER:
            target.execute(f'DELETE FROM {table}')
        target.commit()
        copied = {}
        for table in TABLES:
            src_cols = [r['name'] for r in source.execute(f'PRAGMA table_info({table})').fetchall()]
            dst_cols = [r['name'] for r in target.execute(f'PRAGMA table_info({table})').fetchall()]
            cols = [c for c in src_cols if c in dst_cols]
            if not cols:
                copied[table] = 0
                continue
            names = ','.join('"' + c.replace('"', '""') + '"' for c in cols)
            marks = ','.join('?' for _ in cols)
            rows = source.execute(f'SELECT {names} FROM {table}').fetchall()
            for row in rows:
                target.execute(f'INSERT INTO {table} ({names}) VALUES ({marks})', tuple(row[c] for c in cols))
            target.commit()
            copied[table] = len(rows)
        # Verify counts after writes, not just that inserts returned.
        verified = {}
        for table in TABLES:
            verified[table] = int(target.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
            if verified[table] != copied[table]:
                raise RuntimeError(f'Count mismatch for {table}: copied {copied[table]}, target has {verified[table]}')
        print('TURSO DATA COPY COUNTS VERIFIED')
        for table in TABLES:
            print(f'  {table}: {verified[table]} rows')
        print('Note: analytics.duckdb is intentionally not migrated; it is a secondary analytics cache.')
        print('Keep the original quiz.db unchanged until you have tested login, registration, quiz delivery, submissions, and review.')
        return 0
    except Exception as exc:
        try: target.rollback()
        except Exception: pass
        print(f'MIGRATION FAILED: {type(exc).__name__}: {str(exc)[:300]}', file=sys.stderr)
        print('Do not use this Turso target for the exam until the counts and app workflows are verified.', file=sys.stderr)
        return 4
    finally:
        source.close()
        target.close()

if __name__ == '__main__':
    raise SystemExit(main())
