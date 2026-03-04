import os
import io
import csv
import json
import base64
import tempfile
import threading
import time
import functools
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, session, send_from_directory, redirect, url_for, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import requests

from db import DBConnection, get_column_names

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = not bool(os.environ.get('FLASK_DEBUG'))
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

DATABASE_URL = os.environ.get('DATABASE_URL', '')
DB_PATH = 'ideas.db'

# Async upload jobs: job_id -> {'status': 'processing'|'done'|'error', ...}
_upload_jobs = {}

GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO = os.environ.get('GITHUB_REPO', 'dajanarodriguez/ridea')
BACKUP_BRANCH = 'data-backups'
_branch_ready = False
_backup_lock = threading.Lock()

ALLOWED_AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.m4a', '.mp4', '.flac', '.webm', '.mpeg', '.opus'}

DEPARTMENTS = ['development', 'marketing', 'production', 'management', 'other']
ROLES = ['c-level', 'manager', 'employee', 'majo-markech']

# ─── Failed login tracking ─────────────────────────────────────────────────────
_failed_logins = {}
_failed_logins_lock = threading.Lock()


def get_db():
    return DBConnection(DB_PATH)


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if session.get('role') not in roles:
                return jsonify({'error': 'Forbidden'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# ─── DB init ───────────────────────────────────────────────────────────────────

def init_db():
    """Initialize the database schema on startup."""
    db = get_db()
    if db.use_pg:
        # PostgreSQL – run schema_pg.sql
        schema_path = os.path.join(os.path.dirname(__file__), 'schema_pg.sql')
        if os.path.exists(schema_path):
            with open(schema_path, 'r') as fh:
                sql = fh.read()
            # Split on semicolons and run each statement
            for stmt in sql.split(';'):
                stmt = stmt.strip()
                if stmt:
                    try:
                        db.execute(stmt)
                    except Exception as e:
                        print(f'[init_db] Warning: {e}')
            db.commit()
    else:
        # SQLite – inline schema
        db.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            department TEXT,
            role TEXT DEFAULT 'employee',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            department TEXT,
            author_id INTEGER,
            status TEXT DEFAULT 'submitted',
            anonymous INTEGER DEFAULT 0,
            audio_path TEXT,
            transcript TEXT,
            ai_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (author_id) REFERENCES users(id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            vote_type TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(idea_id, user_id),
            FOREIGN KEY (idea_id) REFERENCES ideas(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (idea_id) REFERENCES ideas(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS idea_tags (
            idea_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (idea_id, tag_id),
            FOREIGN KEY (idea_id) REFERENCES ideas(id),
            FOREIGN KEY (tag_id) REFERENCES tags(id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS idea_categories (
            idea_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY (idea_id, category_id),
            FOREIGN KEY (idea_id) REFERENCES ideas(id),
            FOREIGN KEY (category_id) REFERENCES categories(id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'EUR',
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (idea_id) REFERENCES ideas(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )''')
        db.commit()
    db.close()


# ─── GitHub backup helpers ──────────────────────────────────────────────────────

def _ensure_backup_branch():
    global _branch_ready
    if _branch_ready:
        return True
    if not GITHUB_TOKEN:
        return False
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
    }
    # Check if branch exists
    r = requests.get(
        f'https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/{BACKUP_BRANCH}',
        headers=headers, timeout=10)
    if r.status_code == 200:
        _branch_ready = True
        return True
    # Create branch from main/master
    for base in ('main', 'master'):
        r2 = requests.get(
            f'https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/{base}',
            headers=headers, timeout=10)
        if r2.status_code == 200:
            sha = r2.json()['object']['sha']
            payload = {'ref': f'refs/heads/{BACKUP_BRANCH}', 'sha': sha}
            r3 = requests.post(
                f'https://api.github.com/repos/{GITHUB_REPO}/git/refs',
                headers=headers, json=payload, timeout=10)
            if r3.status_code in (200, 201):
                _branch_ready = True
                return True
    return False


def _push_backup_to_github(content_bytes: bytes, filename: str):
    """Push a file to the backup branch (fire-and-forget thread)."""
    if not _ensure_backup_branch():
        return
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
    }
    b64 = base64.b64encode(content_bytes).decode()
    # Check existing SHA
    path_in_repo = f'backups/{filename}'
    r = requests.get(
        f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path_in_repo}?ref={BACKUP_BRANCH}',
        headers=headers, timeout=10)
    sha = r.json().get('sha') if r.status_code == 200 else None
    payload = {
        'message': f'backup: {filename}',
        'content': b64,
        'branch': BACKUP_BRANCH,
    }
    if sha:
        payload['sha'] = sha
    requests.put(
        f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path_in_repo}',
        headers=headers, json=payload, timeout=30)


# ─── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    department = data.get('department', 'other')
    role = data.get('role', 'employee')

    if not name or not email or not password:
        return jsonify({'error': 'Name, email and password are required'}), 400
    if department not in DEPARTMENTS:
        department = 'other'
    if role not in ROLES:
        role = 'employee'

    pw_hash = generate_password_hash(password)
    db = get_db()
    try:
        db.execute(
            'INSERT INTO users (name, email, password_hash, department, role) VALUES (?, ?, ?, ?, ?)',
            (name, email, pw_hash, department, role))
        db.commit()
        user_id = db.lastrowid()
    except Exception as e:
        db.close()
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': 'Email already registered'}), 409
        return jsonify({'error': str(e)}), 500
    db.close()
    session.permanent = True
    session['user_id'] = user_id
    session['role'] = role
    session['name'] = name
    return jsonify({'message': 'Registered', 'user': {'id': user_id, 'name': name, 'email': email, 'role': role, 'department': department}}), 201


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    # Rate-limit per IP
    ip = request.remote_addr
    with _failed_logins_lock:
        attempts, locked_until = _failed_logins.get(ip, (0, None))
        if locked_until and datetime.utcnow() < locked_until:
            return jsonify({'error': 'Too many failed attempts. Try again later.'}), 429

    db = get_db()
    rows = db.execute('SELECT id, name, password_hash, role, department FROM users WHERE email = ?', (email,)).fetchall()
    cols = get_column_names(db, 'users')
    db.close()

    if not rows:
        _record_failed(ip)
        return jsonify({'error': 'Invalid credentials'}), 401

    user = dict(zip(get_column_names_static(['id', 'name', 'password_hash', 'role', 'department']), rows[0]))
    if not check_password_hash(user['password_hash'], password):
        _record_failed(ip)
        return jsonify({'error': 'Invalid credentials'}), 401

    with _failed_logins_lock:
        _failed_logins.pop(ip, None)

    session.permanent = True
    session['user_id'] = user['id']
    session['role'] = user['role']
    session['name'] = user['name']
    return jsonify({'message': 'Logged in', 'user': {'id': user['id'], 'name': user['name'], 'role': user['role'], 'department': user['department']}})


def get_column_names_static(cols):
    return cols


def _record_failed(ip):
    with _failed_logins_lock:
        attempts, _ = _failed_logins.get(ip, (0, None))
        attempts += 1
        locked_until = datetime.utcnow() + timedelta(minutes=15) if attempts >= 5 else None
        _failed_logins[ip] = (attempts, locked_until)


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': 'Logged out'})


@app.route('/api/me', methods=['GET'])
@login_required
def me():
    db = get_db()
    rows = db.execute('SELECT id, name, email, department, role, created_at FROM users WHERE id = ?',
                      (session['user_id'],)).fetchall()
    db.close()
    if not rows:
        return jsonify({'error': 'User not found'}), 404
    keys = ['id', 'name', 'email', 'department', 'role', 'created_at']
    return jsonify(dict(zip(keys, rows[0])))


# ─── Ideas CRUD ────────────────────────────────────────────────────────────────

@app.route('/api/ideas', methods=['GET'])
@login_required
def list_ideas():
    db = get_db()
    status_filter = request.args.get('status')
    dept_filter = request.args.get('department')
    search = request.args.get('search', '').strip()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    offset = (page - 1) * per_page

    conditions = []
    params = []
    if status_filter:
        conditions.append('i.status = ?')
        params.append(status_filter)
    if dept_filter:
        conditions.append('i.department = ?')
        params.append(dept_filter)
    if search:
        conditions.append('(i.title LIKE ? OR i.description LIKE ?)')
        params.extend([f'%{search}%', f'%{search}%'])

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

    count_row = db.execute(f'SELECT COUNT(*) FROM ideas i {where}', params).fetchone()
    total = count_row[0] if count_row else 0

    rows = db.execute(f'''
        SELECT i.id, i.title, i.description, i.department, i.author_id,
               CASE WHEN i.anonymous = 1 THEN NULL ELSE u.name END as author_name,
               i.status, i.anonymous, i.created_at, i.updated_at,
               i.ai_summary, i.transcript,
               COALESCE(SUM(CASE WHEN v.vote_type='up' THEN 1 ELSE 0 END), 0) as upvotes,
               COALESCE(SUM(CASE WHEN v.vote_type='down' THEN 1 ELSE 0 END), 0) as downvotes
        FROM ideas i
        LEFT JOIN users u ON i.author_id = u.id
        LEFT JOIN votes v ON i.id = v.idea_id
        {where}
        GROUP BY i.id
        ORDER BY i.created_at DESC
        LIMIT ? OFFSET ?
    ''', params + [per_page, offset]).fetchall()

    keys = ['id', 'title', 'description', 'department', 'author_id', 'author_name',
            'status', 'anonymous', 'created_at', 'updated_at', 'ai_summary', 'transcript',
            'upvotes', 'downvotes']
    ideas = [dict(zip(keys, r)) for r in rows]
    db.close()
    return jsonify({'ideas': ideas, 'total': total, 'page': page, 'per_page': per_page})


@app.route('/api/ideas', methods=['POST'])
@login_required
def create_idea():
    data = request.get_json() or {}
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()
    department = data.get('department', session.get('department', 'other'))
    anonymous = int(bool(data.get('anonymous', False)))
    tags = data.get('tags', [])

    if not title:
        return jsonify({'error': 'Title is required'}), 400
    if department not in DEPARTMENTS:
        department = 'other'

    db = get_db()
    db.execute(
        'INSERT INTO ideas (title, description, department, author_id, anonymous) VALUES (?, ?, ?, ?, ?)',
        (title, description, department, session['user_id'], anonymous))
    db.commit()
    idea_id = db.lastrowid()

    # Handle tags
    for tag_name in tags:
        tag_name = tag_name.strip().lower()
        if not tag_name:
            continue
        db.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (tag_name,))
        db.commit()
        tag_row = db.execute('SELECT id FROM tags WHERE name = ?', (tag_name,)).fetchone()
        if tag_row:
            db.execute('INSERT OR IGNORE INTO idea_tags (idea_id, tag_id) VALUES (?, ?)', (idea_id, tag_row[0]))
    db.commit()

    # Notify managers/c-level
    notif_rows = db.execute(
        "SELECT id FROM users WHERE role IN ('manager', 'c-level')").fetchall()
    for row in notif_rows:
        db.execute('INSERT INTO notifications (user_id, message) VALUES (?, ?)',
                   (row[0], f'New idea submitted: {title}'))
    db.commit()
    db.close()
    return jsonify({'message': 'Idea created', 'id': idea_id}), 201


@app.route('/api/ideas/<int:idea_id>', methods=['GET'])
@login_required
def get_idea(idea_id):
    db = get_db()
    row = db.execute('''
        SELECT i.id, i.title, i.description, i.department, i.author_id,
               CASE WHEN i.anonymous = 1 THEN NULL ELSE u.name END as author_name,
               i.status, i.anonymous, i.audio_path, i.transcript, i.ai_summary,
               i.created_at, i.updated_at,
               COALESCE(SUM(CASE WHEN v.vote_type='up' THEN 1 ELSE 0 END), 0) as upvotes,
               COALESCE(SUM(CASE WHEN v.vote_type='down' THEN 1 ELSE 0 END), 0) as downvotes
        FROM ideas i
        LEFT JOIN users u ON i.author_id = u.id
        LEFT JOIN votes v ON i.id = v.idea_id
        WHERE i.id = ?
        GROUP BY i.id
    ''', (idea_id,)).fetchone()

    if not row:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    keys = ['id', 'title', 'description', 'department', 'author_id', 'author_name',
            'status', 'anonymous', 'audio_path', 'transcript', 'ai_summary',
            'created_at', 'updated_at', 'upvotes', 'downvotes']
    idea = dict(zip(keys, row))

    # Tags
    tag_rows = db.execute('''
        SELECT t.name FROM tags t
        JOIN idea_tags it ON t.id = it.tag_id
        WHERE it.idea_id = ?
    ''', (idea_id,)).fetchall()
    idea['tags'] = [r[0] for r in tag_rows]

    # Comments
    comment_rows = db.execute('''
        SELECT c.id, c.content, c.created_at,
               CASE WHEN i.anonymous=1 AND c.user_id=i.author_id THEN NULL ELSE u.name END
        FROM comments c
        JOIN users u ON c.user_id = u.id
        JOIN ideas i ON i.id = c.idea_id
        WHERE c.idea_id = ?
        ORDER BY c.created_at
    ''', (idea_id,)).fetchall()
    idea['comments'] = [{'id': r[0], 'content': r[1], 'created_at': r[2], 'author_name': r[3]}
                        for r in comment_rows]

    # User's own vote
    vote_row = db.execute(
        'SELECT vote_type FROM votes WHERE idea_id = ? AND user_id = ?',
        (idea_id, session['user_id'])).fetchone()
    idea['user_vote'] = vote_row[0] if vote_row else None

    db.close()
    return jsonify(idea)


@app.route('/api/ideas/<int:idea_id>', methods=['PUT'])
@login_required
def update_idea(idea_id):
    db = get_db()
    row = db.execute('SELECT author_id, status FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    author_id, current_status = row
    role = session.get('role')
    user_id = session['user_id']

    data = request.get_json() or {}

    # Status change: managers/c-level only
    new_status = data.get('status')
    if new_status and new_status != current_status:
        if role not in ('manager', 'c-level'):
            db.close()
            return jsonify({'error': 'Forbidden'}), 403
        db.execute('UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?',
                   (new_status, datetime.utcnow().isoformat(), idea_id))
        # Notify author
        db.execute('INSERT INTO notifications (user_id, message) VALUES (?, ?)',
                   (author_id, f'Your idea status changed to {new_status}'))
        db.commit()
        db.close()
        return jsonify({'message': 'Status updated'})

    # Edit content: author only (if not in final state)
    if author_id != user_id:
        db.close()
        return jsonify({'error': 'Forbidden'}), 403
    if current_status in ('approved', 'rejected'):
        db.close()
        return jsonify({'error': 'Cannot edit idea in current status'}), 400

    title = data.get('title')
    description = data.get('description')
    updates = []
    params = []
    if title:
        updates.append('title = ?')
        params.append(title.strip())
    if description is not None:
        updates.append('description = ?')
        params.append(description.strip())
    updates.append('updated_at = ?')
    params.append(datetime.utcnow().isoformat())
    params.append(idea_id)
    db.execute(f'UPDATE ideas SET {", ".join(updates)} WHERE id = ?', params)
    db.commit()
    db.close()
    return jsonify({'message': 'Idea updated'})


@app.route('/api/ideas/<int:idea_id>', methods=['DELETE'])
@login_required
def delete_idea(idea_id):
    db = get_db()
    row = db.execute('SELECT author_id FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    author_id = row[0]
    role = session.get('role')
    if author_id != session['user_id'] and role not in ('manager', 'c-level'):
        db.close()
        return jsonify({'error': 'Forbidden'}), 403
    db.execute('DELETE FROM idea_tags WHERE idea_id = ?', (idea_id,))
    db.execute('DELETE FROM votes WHERE idea_id = ?', (idea_id,))
    db.execute('DELETE FROM comments WHERE idea_id = ?', (idea_id,))
    db.execute('DELETE FROM investments WHERE idea_id = ?', (idea_id,))
    db.execute('DELETE FROM ideas WHERE id = ?', (idea_id,))
    db.commit()
    db.close()
    return jsonify({'message': 'Idea deleted'})


# ─── Voting ─────────────────────────────────────────────────────────────────────

@app.route('/api/ideas/<int:idea_id>/vote', methods=['POST'])
@login_required
def vote(idea_id):
    data = request.get_json() or {}
    vote_type = data.get('vote_type')
    if vote_type not in ('up', 'down'):
        return jsonify({'error': 'vote_type must be up or down'}), 400
    db = get_db()
    existing = db.execute(
        'SELECT vote_type FROM votes WHERE idea_id = ? AND user_id = ?',
        (idea_id, session['user_id'])).fetchone()
    if existing:
        if existing[0] == vote_type:
            # Toggle off
            db.execute('DELETE FROM votes WHERE idea_id = ? AND user_id = ?',
                       (idea_id, session['user_id']))
        else:
            db.execute('UPDATE votes SET vote_type = ? WHERE idea_id = ? AND user_id = ?',
                       (vote_type, idea_id, session['user_id']))
    else:
        db.execute('INSERT INTO votes (idea_id, user_id, vote_type) VALUES (?, ?, ?)',
                   (idea_id, session['user_id'], vote_type))
    db.commit()
    counts = db.execute(
        'SELECT COALESCE(SUM(CASE WHEN vote_type="up" THEN 1 ELSE 0 END),0), COALESCE(SUM(CASE WHEN vote_type="down" THEN 1 ELSE 0 END),0) FROM votes WHERE idea_id = ?',
        (idea_id,)).fetchone()
    db.close()
    return jsonify({'upvotes': counts[0], 'downvotes': counts[1]})


# ─── Comments ──────────────────────────────────────────────────────────────────

@app.route('/api/ideas/<int:idea_id>/comments', methods=['POST'])
@login_required
def add_comment(idea_id):
    data = request.get_json() or {}
    content = data.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Content required'}), 400
    db = get_db()
    db.execute('INSERT INTO comments (idea_id, user_id, content) VALUES (?, ?, ?)',
               (idea_id, session['user_id'], content))
    db.commit()
    comment_id = db.lastrowid()

    # Notify idea author
    row = db.execute('SELECT author_id, title FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if row and row[0] != session['user_id']:
        db.execute('INSERT INTO notifications (user_id, message) VALUES (?, ?)',
                   (row[0], f'New comment on your idea "{row[1]}"'))
        db.commit()
    db.close()
    return jsonify({'message': 'Comment added', 'id': comment_id}), 201


@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
@login_required
def delete_comment(comment_id):
    db = get_db()
    row = db.execute('SELECT user_id FROM comments WHERE id = ?', (comment_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    if row[0] != session['user_id'] and session.get('role') not in ('manager', 'c-level'):
        db.close()
        return jsonify({'error': 'Forbidden'}), 403
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    db.close()
    return jsonify({'message': 'Comment deleted'})


# ─── Audio upload & transcription ──────────────────────────────────────────────

@app.route('/api/ideas/<int:idea_id>/audio', methods=['POST'])
@login_required
def upload_audio(idea_id):
    """Upload audio and start async transcription job."""
    db = get_db()
    row = db.execute('SELECT author_id FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    if row[0] != session['user_id']:
        db.close()
        return jsonify({'error': 'Forbidden'}), 403
    db.close()

    if 'audio' not in request.files:
        return jsonify({'error': 'No audio file'}), 400
    f = request.files['audio']
    ext = os.path.splitext(secure_filename(f.filename or ''))[1].lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        return jsonify({'error': f'Unsupported format: {ext}'}), 400

    # Save to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    job_id = str(uuid.uuid4())
    _upload_jobs[job_id] = {'status': 'processing', 'idea_id': idea_id}

    def _do_transcribe():
        try:
            transcript = _transcribe_audio(tmp_path)
            ai_summary = _ai_summarize(transcript) if transcript else None
            db2 = get_db()
            db2.execute('UPDATE ideas SET transcript = ?, ai_summary = ?, updated_at = ? WHERE id = ?',
                        (transcript, ai_summary, datetime.utcnow().isoformat(), idea_id))
            db2.commit()
            db2.close()
            _upload_jobs[job_id] = {'status': 'done', 'transcript': transcript, 'ai_summary': ai_summary}
        except Exception as e:
            _upload_jobs[job_id] = {'status': 'error', 'error': str(e)}
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    threading.Thread(target=_do_transcribe, daemon=True).start()
    return jsonify({'job_id': job_id, 'status': 'processing'}), 202


@app.route('/api/jobs/<job_id>', methods=['GET'])
@login_required
def get_job(job_id):
    job = _upload_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


def _transcribe_audio(file_path: str) -> str:
    """Transcribe audio using OpenAI Whisper API."""
    import openai
    client = openai.OpenAI(api_key=os.environ.get('OPENAI_API_KEY', ''))
    with open(file_path, 'rb') as f:
        transcript = client.audio.transcriptions.create(
            model='whisper-1',
            file=f,
            response_format='text'
        )
    return transcript


def _ai_summarize(text: str) -> str:
    """Summarize text using Claude or OpenAI."""
    anthropic_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if anthropic_key:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model='claude-3-haiku-20240307',
            max_tokens=300,
            messages=[{
                'role': 'user',
                'content': f'Summarize this idea in 2-3 sentences:\n\n{text}'
            }]
        )
        return msg.content[0].text
    openai_key = os.environ.get('OPENAI_API_KEY', '')
    if openai_key:
        import openai
        client = openai.OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[{
                'role': 'user',
                'content': f'Summarize this idea in 2-3 sentences:\n\n{text}'
            }],
            max_tokens=300
        )
        return resp.choices[0].message.content
    return None


# ─── AI Summarize existing idea ─────────────────────────────────────────────────

@app.route('/api/ideas/<int:idea_id>/summarize', methods=['POST'])
@login_required
def summarize_idea(idea_id):
    db = get_db()
    row = db.execute('SELECT title, description FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    text = f'{row[0]}\n\n{row[1] or ""}'
    db.close()
    summary = _ai_summarize(text)
    if summary:
        db = get_db()
        db.execute('UPDATE ideas SET ai_summary = ? WHERE id = ?', (summary, idea_id))
        db.commit()
        db.close()
    return jsonify({'ai_summary': summary})


# ─── Tags ───────────────────────────────────────────────────────────────────────

@app.route('/api/tags', methods=['GET'])
@login_required
def list_tags():
    db = get_db()
    rows = db.execute('SELECT name, COUNT(it.idea_id) as count FROM tags t LEFT JOIN idea_tags it ON t.id = it.tag_id GROUP BY t.id ORDER BY count DESC').fetchall()
    db.close()
    return jsonify([{'name': r[0], 'count': r[1]} for r in rows])


# ─── Users ─────────────────────────────────────────────────────────────────────

@app.route('/api/users', methods=['GET'])
@login_required
@role_required('manager', 'c-level')
def list_users():
    db = get_db()
    rows = db.execute('SELECT id, name, email, department, role, created_at FROM users ORDER BY created_at DESC').fetchall()
    db.close()
    keys = ['id', 'name', 'email', 'department', 'role', 'created_at']
    return jsonify([dict(zip(keys, r)) for r in rows])


@app.route('/api/users/<int:user_id>/role', methods=['PUT'])
@login_required
@role_required('c-level')
def update_user_role(user_id):
    data = request.get_json() or {}
    new_role = data.get('role')
    if new_role not in ROLES:
        return jsonify({'error': 'Invalid role'}), 400
    db = get_db()
    db.execute('UPDATE users SET role = ? WHERE id = ?', (new_role, user_id))
    db.commit()
    db.close()
    return jsonify({'message': 'Role updated'})


# ─── Notifications ──────────────────────────────────────────────────────────────

@app.route('/api/notifications', methods=['GET'])
@login_required
def get_notifications():
    db = get_db()
    rows = db.execute(
        'SELECT id, message, read, created_at FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 50',
        (session['user_id'],)).fetchall()
    db.close()
    keys = ['id', 'message', 'read', 'created_at']
    return jsonify([dict(zip(keys, r)) for r in rows])


@app.route('/api/notifications/<int:notif_id>/read', methods=['PUT'])
@login_required
def mark_notification_read(notif_id):
    db = get_db()
    db.execute('UPDATE notifications SET read = 1 WHERE id = ? AND user_id = ?',
               (notif_id, session['user_id']))
    db.commit()
    db.close()
    return jsonify({'message': 'Marked read'})


@app.route('/api/notifications/read-all', methods=['PUT'])
@login_required
def mark_all_notifications_read():
    db = get_db()
    db.execute('UPDATE notifications SET read = 1 WHERE user_id = ?', (session['user_id'],))
    db.commit()
    db.close()
    return jsonify({'message': 'All marked read'})


# ─── Analytics ─────────────────────────────────────────────────────────────────

@app.route('/api/analytics', methods=['GET'])
@login_required
@role_required('manager', 'c-level')
def analytics():
    db = get_db()

    total_ideas = db.execute('SELECT COUNT(*) FROM ideas').fetchone()[0]
    total_users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]

    # Ideas by status
    status_rows = db.execute('SELECT status, COUNT(*) FROM ideas GROUP BY status').fetchall()
    ideas_by_status = {r[0]: r[1] for r in status_rows}

    # Ideas by department
    dept_rows = db.execute('SELECT department, COUNT(*) FROM ideas GROUP BY department').fetchall()
    ideas_by_department = {r[0]: r[1] for r in dept_rows}

    # Top voted ideas
    top_rows = db.execute('''
        SELECT i.id, i.title,
               COALESCE(SUM(CASE WHEN v.vote_type='up' THEN 1 ELSE 0 END), 0) as upvotes
        FROM ideas i
        LEFT JOIN votes v ON i.id = v.idea_id
        GROUP BY i.id
        ORDER BY upvotes DESC
        LIMIT 5
    ''').fetchall()
    top_ideas = [{'id': r[0], 'title': r[1], 'upvotes': r[2]} for r in top_rows]

    # Monthly submissions (last 6 months)
    monthly_rows = db.execute('''
        SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as count
        FROM ideas
        WHERE created_at >= date('now', '-6 months')
        GROUP BY month
        ORDER BY month
    ''').fetchall()
    monthly_submissions = [{'month': r[0], 'count': r[1]} for r in monthly_rows]

    # Total investments
    inv_row = db.execute('SELECT COALESCE(SUM(amount), 0) FROM investments').fetchone()
    total_investment = inv_row[0] if inv_row else 0

    db.close()
    return jsonify({
        'total_ideas': total_ideas,
        'total_users': total_users,
        'ideas_by_status': ideas_by_status,
        'ideas_by_department': ideas_by_department,
        'top_ideas': top_ideas,
        'monthly_submissions': monthly_submissions,
        'total_investment': total_investment,
    })


# ─── Export ─────────────────────────────────────────────────────────────────────

@app.route('/api/export/csv', methods=['GET'])
@login_required
@role_required('manager', 'c-level')
def export_csv():
    db = get_db()
    rows = db.execute('''
        SELECT i.id, i.title, i.description, i.department,
               CASE WHEN i.anonymous=1 THEN 'Anonymous' ELSE u.name END as author,
               i.status, i.created_at,
               COALESCE(SUM(CASE WHEN v.vote_type='up' THEN 1 ELSE 0 END), 0) as upvotes
        FROM ideas i
        LEFT JOIN users u ON i.author_id = u.id
        LEFT JOIN votes v ON i.id = v.idea_id
        GROUP BY i.id
        ORDER BY i.created_at DESC
    ''').fetchall()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Title', 'Description', 'Department', 'Author', 'Status', 'Created At', 'Upvotes'])
    writer.writerows(rows)
    output.seek(0)

    # Optional GitHub backup
    if GITHUB_TOKEN:
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        threading.Thread(
            target=_push_backup_to_github,
            args=(output.getvalue().encode(), f'ideas_{ts}.csv'),
            daemon=True).start()

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=ideas.csv'})


@app.route('/api/export/json', methods=['GET'])
@login_required
@role_required('manager', 'c-level')
def export_json():
    db = get_db()
    rows = db.execute('''
        SELECT i.id, i.title, i.description, i.department,
               CASE WHEN i.anonymous=1 THEN NULL ELSE u.name END as author_name,
               i.status, i.created_at, i.ai_summary,
               COALESCE(SUM(CASE WHEN v.vote_type='up' THEN 1 ELSE 0 END), 0) as upvotes,
               COALESCE(SUM(CASE WHEN v.vote_type='down' THEN 1 ELSE 0 END), 0) as downvotes
        FROM ideas i
        LEFT JOIN users u ON i.author_id = u.id
        LEFT JOIN votes v ON i.id = v.idea_id
        GROUP BY i.id
        ORDER BY i.created_at DESC
    ''').fetchall()
    db.close()
    keys = ['id', 'title', 'description', 'department', 'author_name', 'status',
            'created_at', 'ai_summary', 'upvotes', 'downvotes']
    data = [dict(zip(keys, r)) for r in rows]

    if GITHUB_TOKEN:
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        threading.Thread(
            target=_push_backup_to_github,
            args=(json.dumps(data).encode(), f'ideas_{ts}.json'),
            daemon=True).start()

    return jsonify(data)


# ─── Investments (Fáza 3) ──────────────────────────────────────────────────────

@app.route('/api/ideas/<int:idea_id>/invest', methods=['POST'])
@login_required
@role_required('c-level', 'majo-markech')
def invest(idea_id):
    data = request.get_json() or {}
    try:
        amount = float(data.get('amount', 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400
    if amount <= 0:
        return jsonify({'error': 'Amount must be positive'}), 400
    currency = data.get('currency', 'EUR')
    note = data.get('note', '').strip()

    db = get_db()
    if not db.execute('SELECT id FROM ideas WHERE id = ?', (idea_id,)).fetchone():
        db.close()
        return jsonify({'error': 'Idea not found'}), 404
    db.execute(
        'INSERT INTO investments (idea_id, user_id, amount, currency, note) VALUES (?, ?, ?, ?, ?)',
        (idea_id, session['user_id'], amount, currency, note))
    db.commit()
    inv_id = db.lastrowid()
    db.close()
    return jsonify({'message': 'Investment recorded', 'id': inv_id}), 201


@app.route('/api/ideas/<int:idea_id>/investments', methods=['GET'])
@login_required
def list_investments(idea_id):
    db = get_db()
    rows = db.execute('''
        SELECT inv.id, inv.amount, inv.currency, inv.note, inv.created_at, u.name
        FROM investments inv
        JOIN users u ON inv.user_id = u.id
        WHERE inv.idea_id = ?
        ORDER BY inv.created_at DESC
    ''', (idea_id,)).fetchall()
    db.close()
    keys = ['id', 'amount', 'currency', 'note', 'created_at', 'investor_name']
    return jsonify([dict(zip(keys, r)) for r in rows])


# ─── Categories ─────────────────────────────────────────────────────────────────

@app.route('/api/categories', methods=['GET'])
@login_required
def list_categories():
    db = get_db()
    rows = db.execute('SELECT id, name, description FROM categories ORDER BY name').fetchall()
    db.close()
    return jsonify([{'id': r[0], 'name': r[1], 'description': r[2]} for r in rows])


@app.route('/api/categories', methods=['POST'])
@login_required
@role_required('manager', 'c-level')
def create_category():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    description = data.get('description', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    db = get_db()
    try:
        db.execute('INSERT INTO categories (name, description) VALUES (?, ?)', (name, description))
        db.commit()
        cat_id = db.lastrowid()
    except Exception as e:
        db.close()
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': 'Category already exists'}), 409
        return jsonify({'error': str(e)}), 500
    db.close()
    return jsonify({'message': 'Category created', 'id': cat_id}), 201


@app.route('/api/ideas/<int:idea_id>/categories', methods=['POST'])
@login_required
def assign_categories(idea_id):
    data = request.get_json() or {}
    category_ids = data.get('category_ids', [])
    db = get_db()
    db.execute('DELETE FROM idea_categories WHERE idea_id = ?', (idea_id,))
    for cid in category_ids:
        try:
            db.execute('INSERT INTO idea_categories (idea_id, category_id) VALUES (?, ?)', (idea_id, cid))
        except Exception:
            pass
    db.commit()
    db.close()
    return jsonify({'message': 'Categories assigned'})


# ─── Serve static files (SPA) ──────────────────────────────────────────────────

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_static(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')


# ─── Health check ──────────────────────────────────────────────────────────────

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})


# ─── Startup ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=bool(os.environ.get('FLASK_DEBUG')))
else:
    # When run by gunicorn
    init_db()
