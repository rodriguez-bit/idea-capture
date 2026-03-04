-- PostgreSQL schema for idea-capture
-- Run on startup when DATABASE_URL is set

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'submitter',
    department TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS ideas (
    id SERIAL PRIMARY KEY,
    author_id INTEGER NOT NULL,
    author_name TEXT DEFAULT '',
    department TEXT DEFAULT '',
    role TEXT DEFAULT '',
    audio_filename TEXT DEFAULT '',
    duration_seconds INTEGER DEFAULT 0,
    transcript TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    ai_score INTEGER DEFAULT 0,
    ai_analysis TEXT DEFAULT '',
    reviewer_note TEXT DEFAULT '',
    reviewed_by TEXT DEFAULT '',
    reviewed_at TEXT DEFAULT '',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    visibility TEXT NOT NULL DEFAULT 'personal',
    tags TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas (status);
CREATE INDEX IF NOT EXISTS idx_ideas_created ON ideas (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ideas_dept ON ideas (department);
