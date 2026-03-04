-- PostgreSQL schema for idea-capture
-- Run on startup when DATABASE_URL is set

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    department TEXT,
    role TEXT DEFAULT 'employee',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ideas (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    department TEXT,
    author_id BIGINT REFERENCES users(id),
    status TEXT DEFAULT 'submitted',
    anonymous BOOLEAN DEFAULT FALSE,
    audio_path TEXT,
    transcript TEXT,
    ai_summary TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS votes (
    id BIGSERIAL PRIMARY KEY,
    idea_id BIGINT NOT NULL REFERENCES ideas(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    vote_type TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(idea_id, user_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id BIGSERIAL PRIMARY KEY,
    idea_id BIGINT NOT NULL REFERENCES ideas(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS idea_tags (
    idea_id BIGINT NOT NULL REFERENCES ideas(id),
    tag_id BIGINT NOT NULL REFERENCES tags(id),
    PRIMARY KEY (idea_id, tag_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    message TEXT NOT NULL,
    read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS idea_categories (
    idea_id BIGINT NOT NULL REFERENCES ideas(id),
    category_id BIGINT NOT NULL REFERENCES categories(id),
    PRIMARY KEY (idea_id, category_id)
);

CREATE TABLE IF NOT EXISTS investments (
    id BIGSERIAL PRIMARY KEY,
    idea_id BIGINT NOT NULL REFERENCES ideas(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    amount NUMERIC(12,2) NOT NULL,
    currency TEXT DEFAULT 'EUR',
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
