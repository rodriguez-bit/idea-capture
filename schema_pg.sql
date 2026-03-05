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
    audio_data TEXT DEFAULT '',
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
    id SERIAL PRIMARY KEY,
    idea_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT DEFAULT '',
    text TEXT NOT NULL,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE INDEX IF NOT EXISTS idx_comments_idea ON comments (idea_id);

CREATE TABLE IF NOT EXISTS votes (
    id SERIAL PRIMARY KEY,
    idea_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    UNIQUE (idea_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_votes_idea ON votes (idea_id);

CREATE TABLE IF NOT EXISTS meetings (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    meeting_date TEXT DEFAULT '',
    created_by INTEGER NOT NULL,
    created_by_name TEXT DEFAULT '',
    status TEXT DEFAULT 'planned',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS meeting_ideas (
    id SERIAL PRIMARY KEY,
    meeting_id INTEGER NOT NULL,
    idea_id INTEGER NOT NULL,
    UNIQUE (meeting_id, idea_id)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    start_date TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_by INTEGER NOT NULL,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);
