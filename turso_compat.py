"""Small DB-API compatibility adapter for EGMIS V24.4 and turso_serverless.

This adapter deliberately supports only the SQLite behavior used by app.py.
It does not silently fall back to a local database when Turso is unavailable.
"""
from __future__ import annotations
import re
import sqlite3

class CompatRow(dict):
    """Mapping row with sqlite3.Row-style integer indexing and keys()."""
    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._names = list(names)
    def __getitem__(self, key):
        if isinstance(key, int):
            key = self._names[key]
        return super().__getitem__(key)
    def keys(self):
        return list(self._names)

class CompatCursor:
    def __init__(self, cursor=None, rows=None, names=None, lastrowid=None, rowcount=None):
        self._cursor = cursor
        self._rows = list(rows) if rows is not None else None
        self._names = list(names or [])
        self._index = 0
        self.lastrowid = lastrowid if lastrowid is not None else getattr(cursor, 'lastrowid', None)
        self.rowcount = rowcount if rowcount is not None else getattr(cursor, 'rowcount', -1)
        self.description = getattr(cursor, 'description', None)
        if self.description:
            self._names = [d[0] for d in self.description]
    def _convert(self, row):
        if row is None or isinstance(row, CompatRow): return row
        if isinstance(row, dict): return CompatRow(list(row.keys()), list(row.values()))
        try: values = list(row)
        except TypeError: return row
        return CompatRow(self._names, values)
    def fetchone(self):
        if self._rows is not None:
            if self._index >= len(self._rows): return None
            row = self._rows[self._index]; self._index += 1
            return self._convert(row)
        return self._convert(self._cursor.fetchone())
    def fetchall(self):
        if self._rows is not None:
            rows = self._rows[self._index:]; self._index = len(self._rows)
            return [self._convert(r) for r in rows]
        return [self._convert(r) for r in self._cursor.fetchall()]
    def fetchmany(self, size=None):
        if self._rows is not None:
            size = size or 1
            rows = self._rows[self._index:self._index+size]; self._index += len(rows)
            return [self._convert(r) for r in rows]
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._convert(r) for r in rows]

class TursoConnection:
    def __init__(self, url, token):
        try:
            import turso_serverless
        except ImportError as exc:
            raise RuntimeError("Turso mode requires the turso_serverless package. Install requirements.txt.") from exc
        self._conn = turso_serverless.connect(url, auth_token=token)
        self._closed = False
    @staticmethod
    def _translate(exc):
        msg = str(exc)
        low = msg.lower()
        if any(s in low for s in ('unique constraint', 'foreign key constraint', 'not null constraint', 'constraint failed', 'primary key')):
            return sqlite3.IntegrityError(msg)
        if any(s in low for s in ('busy', 'locked', 'timeout', 'temporarily unavailable', 'rate limit', 'connection', 'network', 'http')):
            return sqlite3.OperationalError(msg)
        if any(s in low for s in ('syntax error', 'no such table', 'no such column', 'no such function', 'datatype mismatch')):
            return sqlite3.OperationalError(msg)
        return sqlite3.DatabaseError(msg)
    def execute(self, sql, parameters=()):
        statement = sql.strip()
        # Local SQLite PRAGMAs do not apply to a remote Turso database.
        if re.match(r'(?is)^PRAGMA\s+(foreign_keys|busy_timeout|wal_autocheckpoint|temp_store|journal_mode|synchronous)\b', statement):
            return CompatCursor(rows=[], names=[])
        # The app only needs column names from PRAGMA table_info(...).
        match = re.match(r'(?is)^PRAGMA\s+table_info\s*\(\s*["`\[]?([\w]+)', statement)
        if match:
            table = match.group(1)
            try:
                cur = self._conn.execute(f'SELECT * FROM "{table}" LIMIT 0')
                desc = getattr(cur, 'description', None) or []
                names = [d[0] for d in desc]
                rows = [(i, name, '', 0, None, 0) for i, name in enumerate(names)]
                return CompatCursor(rows=rows, names=['cid','name','type','notnull','dflt_value','pk'])
            except Exception as exc:
                raise self._translate(exc) from exc
        # Use RETURNING to provide reliable lastrowid across the remote DB-API.
        add_returning = bool(re.match(r'(?is)^INSERT\s+INTO\s+', statement)) and ' returning ' not in (' '+statement.lower()+' ')
        exec_sql = statement + ' RETURNING id' if add_returning else statement
        try:
            cur = self._conn.execute(exec_sql, parameters)
            lastrowid = getattr(cur, 'lastrowid', None)
            if add_returning:
                row = cur.fetchone()
                if row is not None:
                    try: lastrowid = row[0] if not isinstance(row, dict) else row.get('id')
                    except Exception: pass
                # The returned row is only for lastrowid; callers of these INSERTs don't fetch it.
                return CompatCursor(cursor=None, rows=[], names=[], lastrowid=lastrowid, rowcount=getattr(cur, 'rowcount', 1))
            return CompatCursor(cur, lastrowid=lastrowid)
        except Exception as exc:
            if isinstance(exc, (sqlite3.Error, RuntimeError)) and isinstance(exc, sqlite3.Error): raise
            raise self._translate(exc) from exc
    def executemany(self, sql, seq_of_parameters):
        # Keep the existing app's batched-registration path compatible with drivers
        # whose remote cursor doesn't implement executemany consistently.
        count = 0
        for params in seq_of_parameters:
            cur = self.execute(sql, params)
            count += max(0, cur.rowcount if isinstance(cur.rowcount, int) else 0)
        return CompatCursor(rows=[], names=[], rowcount=count)
    def executescript(self, script):
        # App schema script contains ordinary CREATE TABLE statements, not triggers.
        result = CompatCursor(rows=[], names=[])
        for statement in script.split(';'):
            if statement.strip(): result = self.execute(statement)
        return result
    def commit(self):
        fn = getattr(self._conn, 'commit', None)
        if callable(fn):
            try: fn()
            except Exception as exc: raise self._translate(exc) from exc
    def rollback(self):
        fn = getattr(self._conn, 'rollback', None)
        if callable(fn):
            try: fn()
            except Exception as exc: raise self._translate(exc) from exc
    def close(self):
        if self._closed: return
        self._closed = True
        fn = getattr(self._conn, 'close', None)
        if callable(fn): fn()
