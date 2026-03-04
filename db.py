"""
Database abstraction layer — SQLite (local) / PostgreSQL (production).

Usage:
    from db import DBConnection, USE_PG, get_column_names

    # In Flask get_db():
    conn = DBConnection(db_path)          # returns sqlite3.Connection or psycopg2 connection
    with conn as c:                        # context-manager: commits on exit
        cur = c.cursor()
        cur.execute(...)

The module auto-detects DATABASE_URL in the environment.
"""

import os
import sqlite3

DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras


# ---------------------------------------------------------------------------
# Thin wrapper so callers can do: with DBConnection(...) as conn: ...
# ---------------------------------------------------------------------------
class _SQLiteConn:
    """Wraps sqlite3.Connection with context-manager commit semantics."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row  # optional nice-to-have

    # Delegate attribute access to the real connection
    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        # Do NOT close — callers may reuse outside of 'with' block
        return False  # don't suppress exceptions

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def executescript(self, script: str):
        """SQLite-specific; not available on psycopg2."""
        return self._conn.executescript(script)


class _PGConn:
    """Wraps a psycopg2 connection with the same interface."""

    def __init__(self):
        self._conn = psycopg2.connect(DATABASE_URL)
        self._conn.autocommit = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def DBConnection(db_path: str = ''):
    """
    Factory function — returns either a _PGConn or _SQLiteConn depending on
    whether DATABASE_URL is set in the environment.
    """
    if USE_PG:
        return _PGConn()
    return _SQLiteConn(db_path)


def get_column_names(cursor) -> list:
    """
    Return column names from a cursor after a SELECT.
    Works for both sqlite3 and psycopg2 cursors.
    """
    if cursor.description is None:
        return []
    return [d[0] for d in cursor.description]
