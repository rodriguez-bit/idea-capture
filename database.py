"""
Database initialization, migrations, seed data, and backup restore.
"""

import os
import json
from datetime import datetime
from werkzeug.security import generate_password_hash

from db import DBConnection, get_column_names
from config import (
    DATABASE_URL, DB_PATH, logger
)
from services.backup import _github_fetch_file


def get_db():
    return DBConnection(DB_PATH)


def init_db():
    db = get_db()

    if DATABASE_URL:
        # PostgreSQL: run schema file
        try:
            with open('schema_pg.sql', 'r', encoding='utf-8') as f:
                schema = f.read()
            db.executescript(schema)
            db.commit()
        except Exception as e:
            logger.error('PG schema error: %s', e)
    else:
        # SQLite: inline schema
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'submitter',
                department TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                author_name TEXT DEFAULT '',
                department TEXT DEFAULT '',
                role TEXT DEFAULT '',
                audio_filename TEXT DEFAULT '',
                audio_data TEXT DEFAULT '',
                duration_seconds INTEGER DEFAULT 0,
                transcript TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                ai_score INTEGER DEFAULT 0,
                ai_analysis TEXT DEFAULT '',
                reviewer_note TEXT DEFAULT '',
                reviewed_by TEXT DEFAULT '',
                reviewed_at TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                visibility TEXT NOT NULL DEFAULT 'personal',
                tags TEXT DEFAULT '[]',
                assigned_to TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                campaign_id INTEGER DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas (status);
            CREATE INDEX IF NOT EXISTS idx_ideas_created ON ideas (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ideas_dept ON ideas (department);

            CREATE TABLE IF NOT EXISTS company_context (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT DEFAULT '',
                text TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_comments_idea ON comments (idea_id);

            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE (idea_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_votes_idea ON votes (idea_id);

            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                meeting_date TEXT DEFAULT '',
                created_by INTEGER NOT NULL,
                created_by_name TEXT DEFAULT '',
                status TEXT DEFAULT 'planned',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS meeting_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                idea_id INTEGER NOT NULL,
                UNIQUE (meeting_id, idea_id)
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                start_date TEXT DEFAULT '',
                end_date TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_by INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
        ''')
        db.commit()

    # ─── Idempotent migrations ────────────────────────────────────────────────

    # Visibility column
    existing_cols = [row[1] for row in db.execute('PRAGMA table_info(ideas)').fetchall()] if not DATABASE_URL else []
    if not DATABASE_URL and 'visibility' not in existing_cols:
        db.execute("ALTER TABLE ideas ADD COLUMN visibility TEXT NOT NULL DEFAULT 'personal'")
        db.commit()
    elif DATABASE_URL:
        try:
            db.execute("""
                DO $$ BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='ideas' AND column_name='visibility'
                  ) THEN
                    ALTER TABLE ideas ADD COLUMN visibility TEXT NOT NULL DEFAULT 'personal';
                  END IF;
                END $$;
            """)
            db.commit()
        except Exception:
            logger.warning('Migration visibility column failed (may already exist)')

    # Tags column
    existing_cols2 = [row[1] for row in db.execute('PRAGMA table_info(ideas)').fetchall()] if not DATABASE_URL else []
    if not DATABASE_URL and 'tags' not in existing_cols2:
        db.execute("ALTER TABLE ideas ADD COLUMN tags TEXT DEFAULT '[]'")
        db.commit()
    elif DATABASE_URL:
        try:
            db.execute("""
                DO $$ BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='ideas' AND column_name='tags'
                  ) THEN
                    ALTER TABLE ideas ADD COLUMN tags TEXT DEFAULT '[]';
                  END IF;
                END $$;
            """)
            db.commit()
        except Exception:
            logger.warning('Migration tags column failed (may already exist)')

    # Additional columns: assigned_to, deadline, campaign_id, audio_data, transcribed_at, stt_engine, idea_type
    existing_cols3 = [row[1] for row in db.execute('PRAGMA table_info(ideas)').fetchall()] if not DATABASE_URL else []
    if not DATABASE_URL:
        if 'assigned_to' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN assigned_to TEXT DEFAULT ''")
        if 'deadline' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN deadline TEXT DEFAULT ''")
        if 'campaign_id' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN campaign_id INTEGER DEFAULT NULL")
        if 'audio_data' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN audio_data TEXT DEFAULT ''")
        if 'transcribed_at' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN transcribed_at TEXT DEFAULT ''")
        if 'stt_engine' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN stt_engine TEXT DEFAULT ''")
        if 'idea_type' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN idea_type TEXT DEFAULT 'napad'")
        db.commit()
    elif DATABASE_URL:
        try:
            db.execute("""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ideas' AND column_name='assigned_to') THEN
                    ALTER TABLE ideas ADD COLUMN assigned_to TEXT DEFAULT '';
                  END IF;
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ideas' AND column_name='deadline') THEN
                    ALTER TABLE ideas ADD COLUMN deadline TEXT DEFAULT '';
                  END IF;
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ideas' AND column_name='campaign_id') THEN
                    ALTER TABLE ideas ADD COLUMN campaign_id INTEGER DEFAULT NULL;
                  END IF;
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ideas' AND column_name='audio_data') THEN
                    ALTER TABLE ideas ADD COLUMN audio_data TEXT DEFAULT '';
                  END IF;
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ideas' AND column_name='transcribed_at') THEN
                    ALTER TABLE ideas ADD COLUMN transcribed_at TEXT DEFAULT '';
                  END IF;
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ideas' AND column_name='stt_engine') THEN
                    ALTER TABLE ideas ADD COLUMN stt_engine TEXT DEFAULT '';
                  END IF;
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ideas' AND column_name='idea_type') THEN
                    ALTER TABLE ideas ADD COLUMN idea_type TEXT DEFAULT 'napad';
                  END IF;
                END $$;
            """)
            db.commit()
        except Exception:
            logger.warning('Migration additional columns failed (may already exist)')

    # ─── Seed default users ───────────────────────────────────────────────────
    count = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    default_password = os.environ.get('DEFAULT_USER_PASSWORD', '')
    if count == 0:
        if not default_password:
            default_password = os.urandom(16).hex()
        default_hash = generate_password_hash(default_password)
        seed_users = [
            ('admin@dajanarodriguez.com', 'Admin', default_hash, 'admin', ''),
            ('raul@dajanarodriguez.com', 'Raul', default_hash, 'reviewer', 'management'),
            ('dajana@dajanarodriguez.com', 'Dajana', default_hash, 'reviewer', 'management'),
        ]
        for email, name, pw_hash, role, dept in seed_users:
            db.execute(
                'INSERT OR REPLACE INTO users (email, display_name, password_hash, role, department) VALUES (?, ?, ?, ?, ?)',
                (email, name, pw_hash, role, dept)
            )
        db.commit()
        logger.info('Seeded default users. Default password env: DEFAULT_USER_PASSWORD')
    elif default_password:
        # ALWAYS sync password hash for seed users when DEFAULT_USER_PASSWORD is set
        new_hash = generate_password_hash(default_password)
        seed_emails = ['admin@dajanarodriguez.com', 'raul@dajanarodriguez.com', 'dajana@dajanarodriguez.com']
        for email in seed_emails:
            row = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
            if row:
                uid = row['id'] if isinstance(row, dict) else row[0]
                db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (new_hash, uid))
                logger.info('Password hash force-synced for %s', email)
        db.commit()
        logger.info('Password sync done for %d seed users (pw_len=%d)', len(seed_emails), len(default_password))

    # Restore from backup
    _restore_from_backup(db)
    _restore_users_from_backup(db)
    db.close()


def _restore_from_backup(db):
    count = db.execute('SELECT COUNT(*) FROM ideas').fetchone()[0]
    if count > 0:
        return
    # Try GitHub backup first
    content = _github_fetch_file('ideas_backup.json')
    if not content:
        try:
            with open('ideas_backup.json', 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            return
    try:
        ideas = json.loads(content)
        for idea in ideas:
            db.execute('''
                INSERT INTO ideas
                (author_id, author_name, department, role, audio_filename, duration_seconds,
                 transcript, status, ai_score, ai_analysis, reviewer_note, reviewed_by, reviewed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                idea.get('author_id', 0),
                idea.get('author_name', ''),
                idea.get('department', ''),
                idea.get('role', ''),
                idea.get('audio_filename', ''),
                idea.get('duration_seconds', 0),
                idea.get('transcript', ''),
                idea.get('status', 'new'),
                idea.get('ai_score', 0),
                idea.get('ai_analysis', ''),
                idea.get('reviewer_note', ''),
                idea.get('reviewed_by', ''),
                idea.get('reviewed_at', ''),
                idea.get('created_at', datetime.now().isoformat())
            ))
        db.commit()
        logger.info('Restored %d ideas from backup', len(ideas))
    except Exception as e:
        logger.error('Restore error: %s', e)


def _restore_users_from_backup(db):
    content = _github_fetch_file('users_backup.json')
    if not content:
        try:
            with open('users_backup.json', 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            return
    try:
        users = json.loads(content)
        for u in users:
            db.execute(
                'INSERT OR IGNORE INTO users (email, display_name, password_hash, role, department, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (u.get('email', ''), u.get('display_name', ''), u.get('password_hash', ''),
                 u.get('role', 'submitter'), u.get('department', ''), u.get('active', 1), u.get('created_at', ''))
            )
        db.commit()
        logger.info('Restored %d users from backup', len(users))
    except Exception as e:
        logger.error('Users restore error: %s', e)
