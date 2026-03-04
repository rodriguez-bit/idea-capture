-- PostgreSQL schema for idea-capture
-- Run on startup when DATABASE_URL is set

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'employee',
    department    TEXT NOT NULL DEFAULT 'other'
);

CREATE TABLE IF NOT EXISTS ideas (
    id               SERIAL PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT,
    department       TEXT NOT NULL DEFAULT 'other',
    status           TEXT NOT NULL DEFAULT 'pending',
    priority         TEXT,
    estimated_effort TEXT,
    tags             TEXT,
    votes            INTEGER NOT NULL DEFAULT 0,
    user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username         TEXT,
    assigned_to      TEXT,
    deadline         TEXT,
    ai_analysis      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id         SERIAL PRIMARY KEY,
    idea_id    INTEGER REFERENCES ideas(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username   TEXT,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS votes (
    id      SERIAL PRIMARY KEY,
    idea_id INTEGER REFERENCES ideas(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    value   INTEGER NOT NULL DEFAULT 1,
    UNIQUE (idea_id, user_id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_ideas_status     ON ideas(status);
CREATE INDEX IF NOT EXISTS idx_ideas_department ON ideas(department);
CREATE INDEX IF NOT EXISTS idx_ideas_user_id    ON ideas(user_id);
CREATE INDEX IF NOT EXISTS idx_comments_idea_id ON comments(idea_id);
CREATE INDEX IF NOT EXISTS idx_votes_idea_id    ON votes(idea_id);
