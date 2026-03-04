"""
Database abstraction layer — SQLite (local) / PostgreSQL (production).

Usage:
    from db import DBConnection, USE_PG, get_column_names

    # In Flask get_db():
    conn = DBConnection(db_path)  # SQLite path (ignored if USE_PG)
    conn.execute("SELECT * FROM investors WHERE id = ?", (1,))
    conn.commit()
    conn.close()

Features:
    - Auto-detects DATABASE_URL env var for PostgreSQL
    - Translates ? placeholders to %s for psycopg2
    - Provides get_column_names() helper for both backends
    - lastrowid() normalised across backends
"""

import os
import re

DATABASE_URL = os.environ.get('DATABASE_URL', '')

# Render gives postgres:// but psycopg2 needs postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

USE_PG = bool(DATABASE_URL)


class DBConnection:
    """Thin wrapper around sqlite3 / psycopg2 that exposes a uniform API."""

    def __init__(self, sqlite_path: str = 'ideas.db'):
        self.use_pg = USE_PG
        self._cursor = None
        self._lastrowid = None

        if USE_PG:
            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(DATABASE_URL)
            self._conn.autocommit = False
        else:
            import sqlite3
            self._conn = sqlite3.connect(sqlite_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA foreign_keys=ON')

    # ── Core API ──────────────────────────────────────────────────────────────

    def execute(self, sql: str, params=()):
        """Execute a single SQL statement.  Returns self for chaining."""
        if self.use_pg:
            sql = _to_pg(sql)
            cur = self._conn.cursor()
            cur.execute(sql, params)
            self._cursor = cur
            # For INSERT … RETURNING id we grab it here
            if cur.description:
                try:
                    row = cur.fetchone()
                    if row:
                        self._lastrowid = row[0]
                except Exception:
                    pass
        else:
            cur = self._conn.execute(sql, params)
            self._cursor = cur
            self._lastrowid = cur.lastrowid
        return self

    def fetchone(self):
        if self._cursor is None:
            return None
        row = self._cursor.fetchone()
        if self.use_pg and row is not None:
            return tuple(row)
        if not self.use_pg and row is not None:
            return tuple(row)  # sqlite3.Row → plain tuple
        return row

    def fetchall(self):
        if self._cursor is None:
            return []
        rows = self._cursor.fetchall()
        if self.use_pg:
            return [tuple(r) for r in rows]
        return [tuple(r) for r in rows]

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def lastrowid(self) -> int:
        """Return the last inserted row-id (works for both backends)."""
        if self.use_pg:
            if self._lastrowid is not None:
                return self._lastrowid
            # Fallback: ask the cursor
            if self._cursor:
                try:
                    row = self._cursor.fetchone()
                    if row:
                        return row[0]
                except Exception:
                    pass
        return self._lastrowid or 0

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_pg(sql: str) -> str:
    """Replace SQLite ? placeholders with PostgreSQL $1, $2, … and adapt syntax."""
    # Replace ? with $n
    counter = 0

    def replacer(m):
        nonlocal counter
        counter += 1
        return f'${counter}'

    sql = re.sub(r'\?', replacer, sql)

    # SQLite-specific pragmas → no-op in PG
    if sql.strip().upper().startswith('PRAGMA'):
        return 'SELECT 1'

    # INSERT OR IGNORE → INSERT … ON CONFLICT DO NOTHING
    sql = re.sub(r'(?i)INSERT OR IGNORE', 'INSERT', sql)
    sql = re.sub(
        r'(?i)(INSERT INTO \w+\s*\([^)]+\)\s*VALUES\s*\([^)]+\))(?!.*ON CONFLICT)',
        lambda m: m.group(0) + ' ON CONFLICT DO NOTHING',
        sql
    )

    # INSERT OR REPLACE → INSERT … ON CONFLICT DO UPDATE (simplistic)
    sql = re.sub(r'(?i)INSERT OR REPLACE', 'INSERT', sql)

    # AUTOINCREMENT → handled via SERIAL/BIGSERIAL in schema
    sql = sql.replace('AUTOINCREMENT', '')

    # strftime('%Y-%m', col) → to_char(col, 'YYYY-MM')
    sql = re.sub(
        r"strftime\('([^']+)',\s*([^)]+)\)",
        lambda m: f"to_char({m.group(2).strip()}, '{_strftime_to_pg(m.group(1))}')",
        sql
    )

    # date('now', ...) → NOW() (simplified)
    sql = re.sub(r"date\('now'[^)]*\)", 'NOW()', sql)

    # CURRENT_TIMESTAMP is valid in both, leave it
    return sql


def _strftime_to_pg(fmt: str) -> str:
    mapping = {'%Y': 'YYYY', '%m': 'MM', '%d': 'DD', '%H': 'HH24', '%M': 'MI', '%S': 'SS'}
    for k, v in mapping.items():
        fmt = fmt.replace(k, v)
    return fmt


def get_column_names(conn: DBConnection, table: str):
    """Return column names for a table (works for both backends)."""
    if conn.use_pg:
        rows = conn.execute(
            'SELECT column_name FROM information_schema.columns WHERE table_name = $1 ORDER BY ordinal_position',
            (table,)
        ).fetchall()
        return [r[0] for r in rows]
    else:
        rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
        return [r[1] for r in rows]
