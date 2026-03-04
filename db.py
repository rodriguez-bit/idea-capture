"""
Database abstraction layer — SQLite (local) / PostgreSQL (production).

Usage:
    from db import DBConnection, USE_PG, get_column_names

    # In Flask get_db():
    conn = DBConnection(db_path)  # SQLite path (ignored if USE_PG)
    conn.execute("SELECT * FROM investors WHERE id = ?", (1,))
    conn.commit()
    conn.close()

When DATABASE_URL is set, all queries are automatically translated:
    ? -> %s, datetime('now') -> CURRENT_TIMESTAMP, LIKE -> ILIKE, etc.
"""

import os
import re
import sqlite3

DATABASE_URL = os.environ.get('DATABASE_URL', '')
# Render/Heroku provide postgres:// but psycopg2 requires postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]
USE_PG = bool(DATABASE_URL)

# PostgreSQL connection pool (lazy init)
_pool = None

if USE_PG:
    try:
        import psycopg2
        import psycopg2.pool
        import psycopg2.extras
    except ImportError:
        print("WARNING: DATABASE_URL is set but psycopg2 not installed. Falling back to SQLite.")
        USE_PG = False


def _get_pool():
    """Lazy-initialize the PostgreSQL connection pool."""
    global _pool
    if _pool is None and USE_PG:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


def _translate_query(sql):
    """Translate SQLite-style SQL to PostgreSQL-compatible SQL."""
    if not USE_PG:
        return sql

    # ? -> %s (parameter placeholders)
    sql = sql.replace('?', '%s')

    # datetime('now') -> CURRENT_TIMESTAMP, date('now') -> CURRENT_DATE
    sql = sql.replace("datetime('now')", 'CURRENT_TIMESTAMP')
    sql = sql.replace("date('now')", 'CURRENT_DATE')

    # date(column) -> column::date (SQLite date() cast -> PG cast)
    sql = re.sub(r'\bdate\((\w+)\)', r'\1::date', sql)

    # TEXT >= CURRENT_DATE comparisons
    sql = re.sub(r'(\w+_datum)\s*>=\s*CURRENT_DATE', r"NULLIF(\1, '')::date >= CURRENT_DATE", sql)
    sql = re.sub(r'(\w+_datum)\s*<=\s*CURRENT_DATE', r"NULLIF(\1, '')::date <= CURRENT_DATE", sql)

    # INSERT OR REPLACE -> ON CONFLICT DO UPDATE
    if 'INSERT OR REPLACE INTO app_meta' in sql:
        sql = sql.replace('INSERT OR REPLACE INTO', 'INSERT INTO')
        if 'ON CONFLICT' not in sql:
            sql = sql.rstrip().rstrip(';')
            sql += ' ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value'
    elif 'INSERT OR REPLACE INTO users' in sql:
        sql = sql.replace('INSERT OR REPLACE INTO', 'INSERT INTO')
        if 'ON CONFLICT' not in sql:
            sql = sql.rstrip().rstrip(';')
            sql += ' ON CONFLICT (email) DO UPDATE SET display_name=EXCLUDED.display_name, password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, department=EXCLUDED.department'
    elif 'INSERT OR REPLACE INTO company_context' in sql:
        sql = sql.replace('INSERT OR REPLACE INTO', 'INSERT INTO')
        if 'ON CONFLICT' not in sql:
            sql = sql.rstrip().rstrip(';')
            sql += ' ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value'
    elif 'INSERT OR REPLACE INTO' in sql:
        sql = sql.replace('INSERT OR REPLACE INTO', 'INSERT INTO')
        if 'ON CONFLICT' not in sql:
            sql = sql.rstrip().rstrip(';')
            sql += ' ON CONFLICT DO NOTHING'

    # INSERT OR IGNORE -> ON CONFLICT DO NOTHING
    if 'INSERT OR IGNORE INTO' in sql:
        sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO')
        if 'ON CONFLICT' not in sql:
            sql = sql.rstrip().rstrip(';')
            sql += ' ON CONFLICT DO NOTHING'

    # LIKE -> ILIKE (case-insensitive for Slovak names)
    sql = sql.replace(' LIKE ', ' ILIKE ')

    # last_insert_rowid() -> lastval()
    sql = sql.replace('last_insert_rowid()', 'lastval()')

    # AUTOINCREMENT -> (remove, handled in schema_pg.sql)
    sql = sql.replace('AUTOINCREMENT', '')

    return sql


class DualAccessRow(dict):
    """Row that supports both row['column'] and row[0] index access.
    Compatible with sqlite3.Row interface used throughout the app."""

    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._columns = columns
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def keys(self):
        return self._columns

    def get(self, key, default=None):
        return super().get(key, default)


class CursorWrapper:
    """Wraps psycopg2 cursor to return DualAccessRow objects."""

    def __init__(self, cursor):
        self._cur = cursor
        self._description = None

    def _wrap_row(self, row):
        if row is None:
            return None
        cols = [desc[0] for desc in self._cur.description]
        return DualAccessRow(cols, row)

    def fetchone(self):
        row = self._cur.fetchone()
        return self._wrap_row(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [desc[0] for desc in self._cur.description]
        return [DualAccessRow(cols, r) for r in rows]

    @property
    def lastrowid(self):
        """Get last inserted row ID via PostgreSQL lastval()."""
        try:
            self._cur.execute("SELECT lastval()")
            return self._cur.fetchone()[0]
        except Exception:
            return None

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description


class DBConnection:
    """Unified database connection that works with both SQLite and PostgreSQL."""

    def __init__(self, db_path=None):
        if USE_PG:
            pool = _get_pool()
            self._conn = pool.getconn()
            self._conn.autocommit = False
            self._is_pg = True
        else:
            self._conn = sqlite3.connect(db_path or ':memory:')
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._is_pg = False

    def execute(self, sql, params=None):
        sql = _translate_query(sql)
        if self._is_pg:
            cur = self._conn.cursor()
            try:
                cur.execute(sql, params or ())
            except Exception as e:
                # Auto-rollback on error to keep connection usable
                self._conn.rollback()
                raise e
            return CursorWrapper(cur)
        else:
            return self._conn.execute(sql, params or ())

    def executescript(self, sql_script):
        """Execute a multi-statement SQL script."""
        if self._is_pg:
            cur = self._conn.cursor()
            for stmt in sql_script.split(';'):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            return CursorWrapper(cur)
        else:
            self._conn.executescript(sql_script)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._is_pg:
            pool = _get_pool()
            if pool:
                pool.putconn(self._conn)
        else:
            self._conn.close()


def get_column_names(db, table_name):
    """Get column names for a table. Works with both SQLite and PostgreSQL."""
    if USE_PG:
        rows = db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            (table_name,)
        ).fetchall()
        return [r[0] for r in rows]
    else:
        rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [r[1] for r in rows]
