import os
import io
import csv
import json
import base64
import re as re_module
import subprocess
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
ALLOWED_DOCUMENT_EXTENSIONS = {'.pdf', '.docx', '.doc', '.txt', '.md', '.rtf', '.png', '.jpg', '.jpeg', '.gif', '.webp'}

DEPARTMENTS = ['development', 'marketing', 'production', 'management', 'other']
ROLES = ['c-level', 'manager', 'employee']

# ─── Failed login tracking ───────────────────────────────────────────────────
_failed_logins = {}
_failed_logins_lock = threading.Lock()


def get_db():
    return DBConnection(DB_PATH)


# ─── CORS + Security headers ──────────────────────────────────────────────────
_ALLOWED_ORIGINS = {
    'null',  # Electron file:// origin
    'http://localhost:5000', 'http://localhost:5001',
    'https://ridea.onrender.com',
}

@app.after_request
def set_security_headers(response):
    origin = request.headers.get('Origin', '')
    if origin in _ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if not app.debug:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

@app.route('/api/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    return '', 204


# ─── Auth decorator ───────────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def reviewer_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('user_role') not in ('reviewer', 'admin'):
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('user_role') != 'admin':
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


# ─── GitHub backup ────────────────────────────────────────────────────────────
def _github_ensure_branch():
    global _branch_ready
    if _branch_ready or not GITHUB_TOKEN:
        return
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    url = f'https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/{BACKUP_BRANCH}'
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 200:
        _branch_ready = True
        return
    main_url = f'https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/main'
    r2 = requests.get(main_url, headers=headers, timeout=10)
    if r2.status_code == 200:
        sha = r2.json()['object']['sha']
        requests.post(f'https://api.github.com/repos/{GITHUB_REPO}/git/refs',
                      headers=headers,
                      json={'ref': f'refs/heads/{BACKUP_BRANCH}', 'sha': sha},
                      timeout=10)
    _branch_ready = True


def _github_fetch_file(file_path):
    if not GITHUB_TOKEN:
        return None
    try:
        headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
        url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}?ref={BACKUP_BRANCH}'
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return base64.b64decode(r.json()['content']).decode('utf-8')
    except Exception as e:
        print(f'GitHub fetch error: {e}')
    return None


def _github_push_file(file_path, content_bytes, commit_message):
    if not GITHUB_TOKEN:
        return
    try:
        _github_ensure_branch()
        headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
        url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}'
        sha = None
        r = requests.get(f'{url}?ref={BACKUP_BRANCH}', headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get('sha')
        payload = {
            'message': commit_message,
            'content': base64.b64encode(content_bytes).decode('utf-8'),
            'branch': BACKUP_BRANCH
        }
        if sha:
            payload['sha'] = sha
        requests.put(url, headers=headers, json=payload, timeout=30)
    except Exception as e:
        print(f'GitHub push error: {e}')


def save_ideas_backup():
    try:
        db = get_db()
        rows = db.execute('SELECT * FROM ideas ORDER BY id').fetchall()
        data = []
        for r in rows:
            d = dict(r)
            d.pop('id', None)
            data.append(d)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        with open('ideas_backup.json', 'w', encoding='utf-8') as f:
            f.write(content)
        threading.Thread(target=_github_push_file,
                         args=('ideas_backup.json', content.encode('utf-8'), 'Auto-backup ideas'),
                         daemon=True).start()
        db.close()
    except Exception as e:
        print(f'Backup error: {e}')


def save_users_backup():
    try:
        db = get_db()
        rows = db.execute(
            'SELECT email, display_name, password_hash, role, department, active, created_at FROM users ORDER BY id'
        ).fetchall()
        data = [dict(r) for r in rows]
        content = json.dumps(data, ensure_ascii=False, indent=2)
        with open('users_backup.json', 'w', encoding='utf-8') as f:
            f.write(content)
        threading.Thread(target=_github_push_file,
                         args=('users_backup.json', content.encode('utf-8'), 'Auto-backup users'),
                         daemon=True).start()
        db.close()
    except Exception as e:
        print(f'Users backup error: {e}')


# ─── DB init ──────────────────────────────────────────────────────────────────
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
            print(f'PG schema error: {e}')
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

    # Idempotent migration: add visibility column if missing
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
            pass

    # Idempotent migration: add tags column if missing
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
            pass

    # Idempotent migration: add assigned_to, deadline, campaign_id columns
    existing_cols3 = [row[1] for row in db.execute('PRAGMA table_info(ideas)').fetchall()] if not DATABASE_URL else []
    if not DATABASE_URL:
        if 'assigned_to' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN assigned_to TEXT DEFAULT ''")
        if 'deadline' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN deadline TEXT DEFAULT ''")
        if 'campaign_id' not in existing_cols3:
            db.execute("ALTER TABLE ideas ADD COLUMN campaign_id INTEGER DEFAULT NULL")
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
                END $$;
            """)
            db.commit()
        except Exception:
            pass

    # Seed default users if none exist
    count = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    if count == 0:
        default_password = os.environ.get('DEFAULT_USER_PASSWORD', os.urandom(16).hex())
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
        print(f'Seeded default users. Default password env: DEFAULT_USER_PASSWORD')

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
        print(f'Restored {len(ideas)} ideas from backup')
    except Exception as e:
        print(f'Restore error: {e}')


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
        print(f'Restored {len(users)} users from backup')
    except Exception as e:
        print(f'Users restore error: {e}')


# ─── Login page ───────────────────────────────────────────────────────────────
LOGIN_HTML = '''<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ridea — Prihlásenie</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #512D6D; color: #f0e6f6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: rgba(255,255,255,0.08); border-radius: 16px; padding: 48px 40px; width: 100%; max-width: 400px; box-shadow: 0 25px 60px rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.12); backdrop-filter: blur(20px); }
  h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; color: #fff; }
  p { color: rgba(255,255,255,0.5); font-size: 14px; margin-bottom: 32px; }
  label { display: block; font-size: 13px; font-weight: 500; color: rgba(255,255,255,0.7); margin-bottom: 6px; }
  input { width: 100%; padding: 12px 16px; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; color: #fff; font-size: 15px; outline: none; margin-bottom: 16px; }
  input:focus { border-color: rgba(255,255,255,0.4); box-shadow: 0 0 0 3px rgba(255,255,255,0.1); }
  button { width: 100%; padding: 13px; background: #fff; color: #512D6D; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 8px; }
  button:hover { background: #f0e6f6; }
  .error { color: #f87171; font-size: 13px; margin-top: 12px; display: none; }
  .logo { font-size: 32px; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#128161;</div>
  <h1>Ridea</h1>
  <p>Interný nástroj pre zachytávanie nápadov</p>
  <label>E-mail</label>
  <input type="email" id="email" placeholder="vas@email.com" autofocus>
  <label>Heslo</label>
  <input type="password" id="password" placeholder="••••••••">
  <button onclick="doLogin()">Prihlásiť sa</button>
  <div class="error" id="err"></div>
</div>
<script>
  document.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  async function doLogin() {
    const email = document.getElementById('email').value.trim();
    const password = document.getElementById('password').value;
    const err = document.getElementById('err');
    err.style.display = 'none';
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password})
    });
    if (r.ok) {
      window.location.href = '/';
    } else {
      const d = await r.json();
      err.textContent = d.error || 'Nesprávne prihlasovacie údaje';
      err.style.display = 'block';
    }
  }
</script>
</body>
</html>'''


# ─── Routes: Auth ─────────────────────────────────────────────────────────────
@app.route('/login')
def login_page():
    if session.get('authenticated'):
        return redirect('/')
    from flask import Response
    return Response(LOGIN_HTML, mimetype='text/html')


@app.route('/api/login', methods=['POST'])
def api_login():
    ip = request.remote_addr
    now = time.time()
    with _failed_logins_lock:
        attempts = _failed_logins.get(ip, [])
        attempts = [t for t in attempts if now - t < 300]
        if len(attempts) >= 5:
            return jsonify({'error': 'Príliš veľa pokusov. Skúste za 5 minút.'}), 429
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE email = ? AND active = 1', (email,)).fetchone()
    db.close()
    if not user or not check_password_hash(user['password_hash'], password):
        with _failed_logins_lock:
            attempts = _failed_logins.get(ip, [])
            attempts.append(now)
            _failed_logins[ip] = attempts
        return jsonify({'error': 'Nesprávny e-mail alebo heslo'}), 401
    session.permanent = True
    session['authenticated'] = True
    session['user_id'] = user['id']
    session['user_email'] = user['email']
    session['user_name'] = user['display_name']
    session['user_role'] = user['role']
    session['user_department'] = user['department']
    return jsonify({'ok': True, 'name': user['display_name'], 'role': user['role']})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/current-user')
@login_required
def api_current_user():
    return jsonify({
        'id': session['user_id'],
        'name': session['user_name'],
        'email': session['user_email'],
        'role': session['user_role'],
        'department': session.get('user_department', '')
    })


# ─── Routes: Ideas ────────────────────────────────────────────────────────────
@app.route('/api/ideas', methods=['GET'])
@login_required
def api_ideas():
    db = get_db()
    filters = []
    params = []

    dept = request.args.get('department')
    role = request.args.get('role')
    status = request.args.get('status')
    search = request.args.get('search')
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))

    # Submitters only see their own ideas or company-wide ones
    user_role = session.get('user_role')
    if user_role == 'submitter':
        filters.append("(author_id = ? OR visibility = 'company')")
        params.append(session['user_id'])

    if dept:
        filters.append('department = ?')
        params.append(dept)
    if role:
        filters.append('role = ?')
        params.append(role)
    if status:
        filters.append('status = ?')
        params.append(status)
    if search:
        filters.append('(transcript LIKE ? OR author_name LIKE ?)')
        params.extend([f'%{search}%', f'%{search}%'])

    where = ('WHERE ' + ' AND '.join(filters)) if filters else ''
    total = db.execute(f'SELECT COUNT(*) FROM ideas {where}', params).fetchone()[0]
    rows = db.execute(f'SELECT * FROM ideas {where} ORDER BY created_at DESC LIMIT ? OFFSET ?',
                      params + [limit, offset]).fetchall()
    db.close()
    return jsonify({'data': [dict(r) for r in rows], 'total': total})


@app.route('/api/ideas/<int:idea_id>', methods=['GET'])
@login_required
def api_idea_detail(idea_id):
    db = get_db()
    idea = db.execute('SELECT * FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    db.close()
    if not idea:
        return jsonify({'error': 'Nápad nenájdený'}), 404
    user_role = session.get('user_role')
    if user_role == 'submitter':
        if idea['author_id'] != session['user_id'] and idea['visibility'] != 'company':
            return jsonify({'error': 'Prístup zamietnutý'}), 403
    return jsonify(dict(idea))


@app.route('/api/ideas/<int:idea_id>', methods=['PATCH'])
@reviewer_required
def api_idea_update(idea_id):
    data = request.get_json() or {}
    allowed = {'status', 'reviewer_note', 'visibility', 'tags', 'assigned_to', 'deadline'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if 'visibility' in updates and updates['visibility'] not in ('personal', 'company'):
        return jsonify({'error': 'Neplatná hodnota viditeľnosti'}), 400
    if 'status' in updates and updates['status'] not in ('new', 'in_review', 'accepted', 'rejected', 'v_realizacii'):
        return jsonify({'error': 'Neplatný status'}), 400
    if 'tags' in updates:
        try:
            parsed = json.loads(updates['tags']) if isinstance(updates['tags'], str) else updates['tags']
            updates['tags'] = json.dumps([str(t) for t in parsed[:10]], ensure_ascii=False)
        except Exception:
            return jsonify({'error': 'Neplatný formát tagov'}), 400
    if not updates:
        return jsonify({'error': 'Nič na aktualizáciu'}), 400

    db = get_db()
    idea = db.execute('SELECT * FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if not idea:
        db.close()
        return jsonify({'error': 'Nápad nenájdený'}), 404

    if 'status' in updates:
        updates['reviewed_by'] = session['user_name']
        updates['reviewed_at'] = datetime.now().isoformat()

    set_clause = ', '.join(f'{k} = ?' for k in updates)
    values = list(updates.values()) + [idea_id]
    db.execute(f'UPDATE ideas SET {set_clause} WHERE id = ?', values)
    db.commit()
    db.close()
    save_ideas_backup()
    return jsonify({'ok': True})


def _auto_analyze(idea_id):
    """Auto-trigger Claude analysis after transcription."""
    try:
        import anthropic as anthropic_sdk
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            print(f'Auto-analyze: no ANTHROPIC_API_KEY')
            return

        with app.app_context():
            db = get_db()
            idea = db.execute('SELECT * FROM ideas WHERE id = ?', (idea_id,)).fetchone()
            if not idea or not idea['transcript']:
                db.close()
                return

            company_context = _get_company_context_for_prompt()

            prompt = f"""Analyzuj nasledujúci interný nápad od zamestnanca a ohodnoť ho.

{('--- KONTEXT FIRMY ---' + chr(10) + company_context + chr(10) + '--- KONIEC KONTEXTU ---' + chr(10)) if company_context else ''}
Oddelenie: {idea['department']}
Rola: {idea['role']}
Transkript nápadu:
"{idea['transcript']}"

Vráť JSON s týmto formátom (iba JSON, bez markdown):
{{
  "score": <1-10>,
  "clarity": <1-10>,
  "feasibility": <1-10>,
  "relevance": <1-10>,
  "summary": "<2-3 vety zhrnutie nápadu>",
  "strengths": ["<silná stránka 1>", "<silná stránka 2>"],
  "weaknesses": ["<slabá stránka 1>"],
  "next_steps": ["<konkrétny krok 1>", "<konkrétny krok 2>"],
  "category": "<one of: process_improvement|cost_reduction|revenue|product|other>",
  "tags": ["<tag1>", "<tag2>"]
}}

Hodnoť objektívne. score je celkové hodnotenie potenciálu nápadu.
relevance je hodnotenie relevancie nápadu pre firmu (ak je k dispozícii kontext firmy, zohľadni ciele, priority a hodnoty firmy).
Pre tags použi max 5 tagov z tohto zoznamu (alebo vlastné slovenské/anglické slovo): quick_win, cost_reduction, product, process, customer, technical, innovation, urgent, automation, hr, marketing, quality."""

            client = anthropic_sdk.Anthropic(api_key=api_key)
            message = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=1000,
                messages=[{'role': 'user', 'content': prompt}]
            )
            raw = message.content[0].text.strip()
            if raw.startswith('```'):
                raw = raw.split('```')[1]
                if raw.startswith('json'):
                    raw = raw[4:]
            analysis = json.loads(raw)
            score = int(analysis.get('score', 0))
            tags = json.dumps(analysis.get('tags', []), ensure_ascii=False)

            db.execute('UPDATE ideas SET ai_score = ?, ai_analysis = ?, tags = ? WHERE id = ?',
                       (score, json.dumps(analysis, ensure_ascii=False), tags, idea_id))
            db.commit()
            db.close()
            save_ideas_backup()
            print(f'Auto-analyze: idea {idea_id} scored {score}/10')
    except Exception as e:
        print(f'Auto-analyze error for idea {idea_id}: {e}')


def _split_audio_chunks(file_path, max_size_mb=20):
    """Split audio file into chunks under max_size_mb for Whisper API (25MB limit)."""
    file_size = os.path.getsize(file_path)
    if file_size <= max_size_mb * 1024 * 1024:
        return [file_path]

    # Get duration using ffprobe
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
            capture_output=True, text=True, timeout=30
        )
        total_duration = float(result.stdout.strip())
    except Exception:
        return [file_path]  # Fallback: send as-is

    # Calculate chunk duration based on file size ratio
    num_chunks = max(2, int(file_size / (max_size_mb * 1024 * 1024)) + 1)
    chunk_duration = total_duration / num_chunks

    chunks = []
    for i in range(num_chunks):
        start = i * chunk_duration
        chunk_path = file_path + f'.chunk{i}.mp3'
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-i', file_path, '-ss', str(start),
                 '-t', str(chunk_duration), '-ar', '16000', '-ac', '1',
                 '-b:a', '64k', chunk_path],
                capture_output=True, timeout=120
            )
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
                chunks.append(chunk_path)
        except Exception:
            continue

    return chunks if chunks else [file_path]


def _clean_hallucinations(text):
    """Remove repeated phrases that indicate Whisper hallucination."""
    if not text or len(text) < 50:
        return text

    # Split into sentences
    sentences = re_module.split(r'[.!?]+', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) < 3:
        return text

    # Count phrase frequency
    phrase_count = {}
    for s in sentences:
        normalized = s.lower().strip()
        if len(normalized) < 5:
            continue
        phrase_count[normalized] = phrase_count.get(normalized, 0) + 1

    # Find hallucinated phrases (repeated 3+ times)
    hallucinated = set()
    for phrase, count in phrase_count.items():
        if count >= 3 and count > len(sentences) * 0.2:
            hallucinated.add(phrase)

    if not hallucinated:
        return text

    # Remove hallucinated sentences, keep first occurrence
    seen_hallucinated = set()
    clean_sentences = []
    for s in sentences:
        normalized = s.lower().strip()
        if normalized in hallucinated:
            if normalized not in seen_hallucinated:
                clean_sentences.append(s)
                seen_hallucinated.add(normalized)
        else:
            clean_sentences.append(s)

    cleaned = '. '.join(clean_sentences)
    if cleaned and not cleaned.endswith('.'):
        cleaned += '.'

    # If we removed >80% of content, it was mostly hallucination
    if len(cleaned) < len(text) * 0.2:
        return ''

    return cleaned


def _process_upload(job_id, tmp_path, ext, user_id, user_name, department, role, visibility, api_key):
    import openai
    try:
        client = openai.OpenAI(api_key=api_key)

        file_size = os.path.getsize(tmp_path)
        print(f'Upload job {job_id}: file size {file_size / 1024 / 1024:.1f}MB')

        # Split large files into chunks for Whisper 25MB limit
        chunks = _split_audio_chunks(tmp_path)
        print(f'Upload job {job_id}: {len(chunks)} chunk(s)')

        all_text = []
        total_duration = 0

        for i, chunk_path in enumerate(chunks):
            chunk_size = os.path.getsize(chunk_path)
            if chunk_size > 25 * 1024 * 1024:
                print(f'Upload job {job_id}: chunk {i} too large ({chunk_size / 1024 / 1024:.1f}MB), skipping')
                continue

            with open(chunk_path, 'rb') as f:
                transcription = client.audio.transcriptions.create(
                    model='whisper-1',
                    file=f,
                    language='sk',
                    response_format='verbose_json',
                    prompt='Toto je nahravka napadu alebo myslienky v slovencine.' if i == 0 else all_text[-1][-200:] if all_text else ''
                )

            chunk_text = transcription.text or ''
            chunk_dur = int(getattr(transcription, 'duration', 0) or 0)
            total_duration += chunk_dur

            # Clean hallucinations from each chunk
            cleaned = _clean_hallucinations(chunk_text)
            if cleaned:
                all_text.append(cleaned)

            print(f'Upload job {job_id}: chunk {i} -> {len(chunk_text)} chars, cleaned -> {len(cleaned)} chars')

        # Clean up chunk files
        for chunk_path in chunks:
            if chunk_path != tmp_path and os.path.exists(chunk_path):
                try:
                    os.unlink(chunk_path)
                except Exception:
                    pass

        transcript_text = ' '.join(all_text).strip()

        # Final hallucination check on combined text
        transcript_text = _clean_hallucinations(transcript_text)

        if not transcript_text:
            _upload_jobs[job_id] = {
                'status': 'error',
                'error': 'Nahravka neobsahuje rozpoznatelnu rec. Skuste nahrat znova s jasnejsim hlasom.'
            }
            return

        with app.app_context():
            db = get_db()
            cursor = db.execute('''
                INSERT INTO ideas (author_id, author_name, department, role, duration_seconds, transcript, status, visibility)
                VALUES (?, ?, ?, ?, ?, ?, 'new', ?)
            ''', (user_id, user_name, department, role, total_duration, transcript_text, visibility))
            idea_id = cursor.lastrowid
            db.commit()
            db.close()
            save_ideas_backup()

        # Auto-analyze with Claude
        _auto_analyze(idea_id)

        _upload_jobs[job_id] = {
            'status': 'done',
            'result': {
                'id': idea_id,
                'transcript': transcript_text,
                'duration_seconds': total_duration,
                'message': 'Napad uspesne zaznamenany'
            }
        }
    except Exception as e:
        print(f'Upload job {job_id} error: {e}')
        _upload_jobs[job_id] = {'status': 'error', 'error': str(e)}
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


@app.route('/api/ideas/upload', methods=['POST'])
@login_required
def api_ideas_upload():
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'error': 'OpenAI API kľúč nie je nastavený'}), 500

    if 'audio' not in request.files:
        return jsonify({'error': 'Chýba audio súbor'}), 400

    file = request.files['audio']
    if not file.filename:
        return jsonify({'error': 'Prázdny súbor'}), 400

    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        return jsonify({'error': f'Nepodporovaný formát: {ext}'}), 400

    department = (request.form.get('department') or '').strip()
    role = (request.form.get('role') or '').strip()
    visibility = (request.form.get('visibility') or 'personal').strip()
    if visibility not in ('personal', 'company'):
        visibility = 'personal'

    if not department or not role:
        return jsonify({'error': 'Oddelenie a rola sú povinné'}), 400

    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            file.save(tmp)
            tmp_path = tmp.name
    except Exception as e:
        return jsonify({'error': f'Chyba pri ukladaní: {str(e)}'}), 500

    job_id = str(uuid.uuid4())
    user_id = session['user_id']
    user_name = session['user_name']
    _upload_jobs[job_id] = {'status': 'processing'}

    t = threading.Thread(
        target=_process_upload,
        args=(job_id, tmp_path, ext, user_id, user_name, department, role, visibility, api_key),
        daemon=True
    )
    t.start()

    return jsonify({'job_id': job_id, 'status': 'processing'})


@app.route('/api/ideas/job/<job_id>', methods=['GET'])
@login_required
def api_ideas_job(job_id):
    job = _upload_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job nenájdený'}), 404
    if job['status'] == 'done':
        del _upload_jobs[job_id]
        return jsonify(job['result'])
    elif job['status'] == 'error':
        err = job.get('error', 'Neznáma chyba')
        del _upload_jobs[job_id]
        return jsonify({'error': err}), 500
    else:
        return jsonify({'status': 'processing'}), 202


@app.route('/api/ideas/<int:idea_id>/analyze', methods=['POST'])
@login_required
def api_idea_analyze(idea_id):
    import anthropic as anthropic_sdk

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'Anthropic API kľúč nie je nastavený'}), 500

    db = get_db()
    idea = db.execute('SELECT * FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if not idea:
        db.close()
        return jsonify({'error': 'Nápad nenájdený'}), 404

    transcript = idea['transcript']
    if not transcript:
        db.close()
        return jsonify({'error': 'Chýba transkript'}), 400

    company_context = _get_company_context_for_prompt()

    prompt = f"""Analyzuj nasledujúci interný nápad od zamestnanca a ohodnoť ho.

{('--- KONTEXT FIRMY ---' + chr(10) + company_context + chr(10) + '--- KONIEC KONTEXTU ---' + chr(10)) if company_context else ''}
Oddelenie: {idea['department']}
Rola: {idea['role']}
Transkript nápadu:
"{transcript}"

Vráť JSON s týmto formátom (iba JSON, bez markdown):
{{
  "score": <1-10>,
  "clarity": <1-10>,
  "feasibility": <1-10>,
  "relevance": <1-10>,
  "summary": "<2-3 vety zhrnutie nápadu>",
  "strengths": ["<silná stránka 1>", "<silná stránka 2>"],
  "weaknesses": ["<slabá stránka 1>"],
  "next_steps": ["<konkrétny krok 1>", "<konkrétny krok 2>"],
  "category": "<one of: process_improvement|cost_reduction|revenue|product|other>",
  "tags": ["<tag1>", "<tag2>"]
}}

Hodnoť objektívne. score je celkové hodnotenie potenciálu nápadu.
relevance je hodnotenie relevancie nápadu pre firmu (ak je k dispozícii kontext firmy, zohľadni ciele, priority a hodnoty firmy).
Pre tags použi max 5 tagov z tohto zoznamu (alebo vlastné slovenské/anglické slovo): quick_win, cost_reduction, product, process, customer, technical, innovation, urgent, automation, hr, marketing, quality."""

    try:
        client = anthropic_sdk.Anthropic(api_key=api_key)
        message = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = message.content[0].text.strip()
        # Strip markdown if present
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        analysis = json.loads(raw)
        score = int(analysis.get('score', 0))
        tags = json.dumps(analysis.get('tags', []), ensure_ascii=False)

        db.execute('UPDATE ideas SET ai_score = ?, ai_analysis = ?, tags = ? WHERE id = ?',
                   (score, json.dumps(analysis, ensure_ascii=False), tags, idea_id))
        db.commit()
        db.close()
        save_ideas_backup()
        return jsonify({'ok': True, 'analysis': analysis})
    except Exception as e:
        db.close()
        print(f'Analyze error: {e}')
        return jsonify({'error': f'AI analýza zlyhala: {str(e)}'}), 500


@app.route('/api/ideas/<int:idea_id>', methods=['DELETE'])
@admin_required
def api_idea_delete(idea_id):
    db = get_db()
    db.execute('DELETE FROM ideas WHERE id = ?', (idea_id,))
    db.commit()
    db.close()
    save_ideas_backup()
    return jsonify({'ok': True})


@app.route('/api/ideas/text', methods=['POST'])
@login_required
def api_ideas_text():
    """Create an idea from text input."""
    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    department = (data.get('department') or '').strip()
    role = (data.get('role') or '').strip()
    visibility = (data.get('visibility') or 'personal').strip()
    if visibility not in ('personal', 'company'):
        visibility = 'personal'
    if not text:
        return jsonify({'error': 'Text napadu je povinny'}), 400
    if not department or not role:
        return jsonify({'error': 'Oddelenie a rola su povinne'}), 400

    db = get_db()
    cursor = db.execute('''
        INSERT INTO ideas (author_id, author_name, department, role, duration_seconds, transcript, status, visibility)
        VALUES (?, ?, ?, ?, 0, ?, 'new', ?)
    ''', (session['user_id'], session['user_name'], department, role, text, visibility))
    idea_id = cursor.lastrowid
    db.commit()
    db.close()
    save_ideas_backup()

    # Auto-analyze in background
    threading.Thread(target=_auto_analyze, args=(idea_id,), daemon=True).start()

    return jsonify({'ok': True, 'id': idea_id, 'message': 'Napad uspesne vytvoreny'}), 201


@app.route('/api/ideas/upload-document', methods=['POST'])
@login_required
def api_ideas_upload_document():
    """Create an idea from an uploaded document (PDF, DOCX, TXT, image)."""
    if 'document' not in request.files:
        return jsonify({'error': 'Chyba subor'}), 400

    file = request.files['document']
    if not file.filename:
        return jsonify({'error': 'Prazdny subor'}), 400

    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        return jsonify({'error': f'Nepodporovany format: {ext}. Podporovane: PDF, DOCX, TXT, MD, obrazky'}), 400

    department = (request.form.get('department') or '').strip()
    role = (request.form.get('role') or '').strip()
    visibility = (request.form.get('visibility') or 'personal').strip()
    if visibility not in ('personal', 'company'):
        visibility = 'personal'
    if not department or not role:
        return jsonify({'error': 'Oddelenie a rola su povinne'}), 400

    try:
        content = file.read()
        text = ''

        if ext in ('.txt', '.md', '.rtf'):
            text = content.decode('utf-8', errors='replace')
        elif ext == '.pdf':
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(io.BytesIO(content))
                pages = []
                for page in reader.pages:
                    pages.append(page.extract_text() or '')
                text = '\n'.join(pages)
            except ImportError:
                # Fallback: try pdfminer
                try:
                    from pdfminer.high_level import extract_text as pdf_extract
                    text = pdf_extract(io.BytesIO(content))
                except ImportError:
                    text = f'[PDF subor: {file.filename} - kniznica na citanie PDF nie je nainstalovana]'
        elif ext in ('.docx', '.doc'):
            try:
                import docx
                doc = docx.Document(io.BytesIO(content))
                text = '\n'.join([p.text for p in doc.paragraphs])
            except ImportError:
                text = f'[DOCX subor: {file.filename} - kniznica na citanie DOCX nie je nainstalovana]'
        elif ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            # For images, store a placeholder and try OCR if available
            text = f'[Obrazok: {file.filename}]'
            try:
                import pytesseract
                from PIL import Image
                img = Image.open(io.BytesIO(content))
                ocr_text = pytesseract.image_to_string(img, lang='slk+eng')
                if ocr_text.strip():
                    text = f'[Obrazok: {file.filename}]\n\n{ocr_text.strip()}'
            except ImportError:
                pass
            except Exception as ocr_err:
                print(f'OCR error: {ocr_err}')

        if not text.strip():
            text = f'[Importovany subor: {file.filename}]'

        # Limit text length
        if len(text) > 50000:
            text = text[:50000] + '\n\n[... text skrateny, povodny subor mal viac ako 50000 znakov]'

        db = get_db()
        cursor = db.execute('''
            INSERT INTO ideas (author_id, author_name, department, role, duration_seconds, transcript, status, visibility)
            VALUES (?, ?, ?, ?, 0, ?, 'new', ?)
        ''', (session['user_id'], session['user_name'], department, role, text, visibility))
        idea_id = cursor.lastrowid
        db.commit()
        db.close()
        save_ideas_backup()

        # Auto-analyze in background
        threading.Thread(target=_auto_analyze, args=(idea_id,), daemon=True).start()

        return jsonify({'ok': True, 'id': idea_id, 'message': 'Dokument uspesne importovany ako napad', 'transcript': text[:200]}), 201

    except Exception as e:
        print(f'Document upload error: {e}')
        return jsonify({'error': f'Chyba pri spracovani suboru: {str(e)}'}), 500


@app.route('/api/ideas/bulk-delete', methods=['POST'])
@admin_required
def api_ideas_bulk_delete():
    data = request.get_json() or {}
    ids = data.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({'error': 'Žiadne nápady na vymazanie'}), 400
    db = get_db()
    placeholders = ','.join(['?'] * len(ids))
    db.execute(f'DELETE FROM ideas WHERE id IN ({placeholders})', ids)
    db.commit()
    db.close()
    save_ideas_backup()
    return jsonify({'ok': True, 'deleted': len(ids)})


# ─── Routes: Users (admin) ────────────────────────────────────────────────────
@app.route('/api/users', methods=['GET'])
@admin_required
def api_users_list():
    db = get_db()
    rows = db.execute('SELECT id, email, display_name, role, department, active, created_at FROM users ORDER BY display_name').fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/users', methods=['POST'])
@admin_required
def api_users_create():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    name = (data.get('display_name') or '').strip()
    password = data.get('password') or ''
    role = data.get('role', 'submitter')
    department = data.get('department', '')

    if not email or not name or not password:
        return jsonify({'error': 'E-mail, meno a heslo sú povinné'}), 400
    if role not in ('submitter', 'reviewer', 'admin'):
        return jsonify({'error': 'Neplatná rola'}), 400

    db = get_db()
    try:
        db.execute(
            'INSERT INTO users (email, display_name, password_hash, role, department) VALUES (?, ?, ?, ?, ?)',
            (email, name, generate_password_hash(password), role, department)
        )
        db.commit()
        db.close()
        save_users_backup()
        return jsonify({'ok': True}), 201
    except Exception as e:
        db.close()
        return jsonify({'error': 'E-mail už existuje'}), 409


@app.route('/api/users/<int:user_id>', methods=['PATCH'])
@admin_required
def api_users_update(user_id):
    data = request.get_json() or {}
    allowed = {'display_name', 'role', 'department', 'active'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if 'password' in data and data['password']:
        updates['password_hash'] = generate_password_hash(data['password'])
    if not updates:
        return jsonify({'error': 'Nič na aktualizáciu'}), 400
    db = get_db()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE users SET {set_clause} WHERE id = ?', list(updates.values()) + [user_id])
    db.commit()
    db.close()
    save_users_backup()
    return jsonify({'ok': True})


# ─── Routes: Password change (self-service) ─────────────────────────────────
@app.route('/api/change-password', methods=['POST'])
@login_required
def api_change_password():
    data = request.get_json() or {}
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')

    if not current_password or not new_password:
        return jsonify({'error': 'Aktuálne heslo a nové heslo sú povinné'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Nové heslo musí mať aspoň 6 znakov'}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user or not check_password_hash(user['password_hash'], current_password):
        db.close()
        return jsonify({'error': 'Nesprávne aktuálne heslo'}), 401

    db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
               (generate_password_hash(new_password), session['user_id']))
    db.commit()
    db.close()
    save_users_backup()
    return jsonify({'ok': True})


# ─── Routes: CSV Export ───────────────────────────────────────────────────────
@app.route('/api/ideas/export-csv')
@login_required
def api_ideas_export_csv():
    db = get_db()
    filters = []
    params = []

    dept = request.args.get('department')
    role = request.args.get('role')
    status = request.args.get('status')
    search = request.args.get('search')

    user_role = session.get('user_role')
    if user_role == 'submitter':
        filters.append("(author_id = ? OR visibility = 'company')")
        params.append(session['user_id'])

    if dept:
        filters.append('department = ?')
        params.append(dept)
    if role:
        filters.append('role = ?')
        params.append(role)
    if status:
        filters.append('status = ?')
        params.append(status)
    if search:
        filters.append('(transcript LIKE ? OR author_name LIKE ?)')
        params.extend([f'%{search}%', f'%{search}%'])

    where = ('WHERE ' + ' AND '.join(filters)) if filters else ''
    rows = db.execute(f'SELECT * FROM ideas {where} ORDER BY created_at DESC', params).fetchall()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Autor', 'Oddelenie', 'Rola', 'Transkript', 'AI Skóre', 'Status', 'Viditeľnosť', 'Priradené', 'Deadline', 'Tagy', 'Vytvorené'])
    for r in rows:
        d = dict(r)
        writer.writerow([
            d.get('id', ''),
            d.get('author_name', ''),
            d.get('department', ''),
            d.get('role', ''),
            d.get('transcript', ''),
            d.get('ai_score', ''),
            d.get('status', ''),
            d.get('visibility', ''),
            d.get('assigned_to', ''),
            d.get('deadline', ''),
            d.get('tags', ''),
            d.get('created_at', '')
        ])

    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=ridea-napady-{datetime.now().strftime("%Y%m%d")}.csv'}
    )


# ─── Routes: Stats ────────────────────────────────────────────────────────────
@app.route('/api/stats')
@login_required
def api_stats():
    db = get_db()
    total = db.execute('SELECT COUNT(*) FROM ideas').fetchone()[0]
    by_status = {}
    for row in db.execute('SELECT status, COUNT(*) as cnt FROM ideas GROUP BY status').fetchall():
        by_status[row['status']] = row['cnt']
    by_dept = {}
    for row in db.execute('SELECT department, COUNT(*) as cnt FROM ideas GROUP BY department').fetchall():
        by_dept[row['department']] = row['cnt']
    by_visibility = {}
    for row in db.execute('SELECT visibility, COUNT(*) as cnt FROM ideas GROUP BY visibility').fetchall():
        by_visibility[row['visibility']] = row['cnt']
    recent = db.execute('SELECT * FROM ideas ORDER BY created_at DESC LIMIT 5').fetchall()

    # Score distribution for chart
    score_dist = {}
    for row in db.execute('SELECT ai_score, COUNT(*) as cnt FROM ideas WHERE ai_score > 0 GROUP BY ai_score ORDER BY ai_score').fetchall():
        score_dist[str(row['ai_score'])] = row['cnt']

    # Average score by department
    avg_by_dept = {}
    for row in db.execute('SELECT department, AVG(ai_score) as avg_score FROM ideas WHERE ai_score > 0 GROUP BY department').fetchall():
        avg_by_dept[row['department']] = round(float(row['avg_score']), 1)

    # Ideas over time (last 30 days, grouped by date)
    trend = []
    for row in db.execute("""
        SELECT substr(created_at, 1, 10) as day, COUNT(*) as cnt
        FROM ideas
        GROUP BY substr(created_at, 1, 10)
        ORDER BY day DESC
        LIMIT 30
    """).fetchall():
        trend.append({'day': row['day'], 'count': row['cnt']})

    db.close()
    return jsonify({
        'total': total,
        'by_status': by_status,
        'by_department': by_dept,
        'by_visibility': by_visibility,
        'recent': [dict(r) for r in recent],
        'score_distribution': score_dist,
        'avg_score_by_dept': avg_by_dept,
        'trend': trend
    })


# ─── Routes: Company Context ──────────────────────────────────────────────────
COMPANY_CONTEXT_KEYS = [
    'company_description',   # O firme
    'goals_priorities',      # Ciele a priority
    'brand_values',          # Brand hodnoty
    'idea_criteria',         # Čo hľadáme v nápadoch
]


@app.route('/api/company-context', methods=['GET'])
@login_required
def api_company_context_get():
    db = get_db()
    rows = db.execute('SELECT key, value FROM company_context').fetchall()
    db.close()
    result = {k: '' for k in COMPANY_CONTEXT_KEYS}
    for row in rows:
        result[row['key']] = row['value']
    return jsonify(result)


@app.route('/api/company-context', methods=['POST'])
@admin_required
def api_company_context_save():
    data = request.get_json() or {}
    db = get_db()
    for key in COMPANY_CONTEXT_KEYS:
        if key in data:
            value = str(data[key])[:5000]  # max 5000 chars per field
            db.execute(
                'INSERT OR REPLACE INTO company_context (key, value) VALUES (?, ?)',
                (key, value)
            )
    db.commit()
    db.close()
    return jsonify({'ok': True})


def _get_company_context_for_prompt():
    """Build company context string for AI analysis prompt."""
    db = get_db()
    rows = db.execute('SELECT key, value FROM company_context').fetchall()
    db.close()
    context_parts = []
    labels = {
        'company_description': 'O firme',
        'goals_priorities': 'Ciele a priority firmy',
        'brand_values': 'Hodnoty značky',
        'idea_criteria': 'Čo hľadáme v nápadoch',
    }
    for row in rows:
        if row['value'] and row['value'].strip():
            label = labels.get(row['key'], row['key'])
            context_parts.append(f"{label}: {row['value'].strip()}")
    return '\n'.join(context_parts)


# ─── Routes: Kanban ─────────────────────────────────────────────────────────────
@app.route('/api/kanban')
@login_required
def api_kanban():
    db = get_db()
    statuses = ['new', 'in_review', 'accepted', 'v_realizacii', 'rejected']
    result = {}
    for s in statuses:
        rows = db.execute(
            'SELECT id, author_name, transcript, ai_score, assigned_to, deadline, tags FROM ideas WHERE status = ? ORDER BY created_at DESC LIMIT 50',
            (s,)
        ).fetchall()
        result[s] = [dict(r) for r in rows]
    db.close()
    return jsonify(result)


# ─── Routes: Comments ───────────────────────────────────────────────────────────
@app.route('/api/ideas/<int:idea_id>/comments', methods=['GET'])
@login_required
def api_comments_list(idea_id):
    db = get_db()
    rows = db.execute(
        'SELECT * FROM comments WHERE idea_id = ? ORDER BY created_at ASC', (idea_id,)
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/ideas/<int:idea_id>/comments', methods=['POST'])
@login_required
def api_comments_create(idea_id):
    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Text komentára je povinný'}), 400
    if len(text) > 2000:
        return jsonify({'error': 'Komentár je príliš dlhý (max 2000 znakov)'}), 400

    db = get_db()
    idea = db.execute('SELECT id FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if not idea:
        db.close()
        return jsonify({'error': 'Nápad nenájdený'}), 404

    db.execute(
        'INSERT INTO comments (idea_id, user_id, user_name, text) VALUES (?, ?, ?, ?)',
        (idea_id, session['user_id'], session['user_name'], text)
    )
    db.commit()
    db.close()
    return jsonify({'ok': True}), 201


@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
@login_required
def api_comments_delete(comment_id):
    db = get_db()
    comment = db.execute('SELECT * FROM comments WHERE id = ?', (comment_id,)).fetchone()
    if not comment:
        db.close()
        return jsonify({'error': 'Komentár nenájdený'}), 404
    # Only author or admin can delete
    if comment['user_id'] != session['user_id'] and session.get('user_role') != 'admin':
        db.close()
        return jsonify({'error': 'Nemáte oprávnenie'}), 403
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


# ─── Routes: Votes ─────────────────────────────────────────────────────────────
@app.route('/api/ideas/<int:idea_id>/votes', methods=['GET'])
@login_required
def api_votes_get(idea_id):
    db = get_db()
    count = db.execute('SELECT COUNT(*) FROM votes WHERE idea_id = ?', (idea_id,)).fetchone()[0]
    user_voted = db.execute(
        'SELECT COUNT(*) FROM votes WHERE idea_id = ? AND user_id = ?',
        (idea_id, session['user_id'])
    ).fetchone()[0] > 0
    db.close()
    return jsonify({'count': count, 'user_voted': user_voted})


@app.route('/api/ideas/<int:idea_id>/votes', methods=['POST'])
@login_required
def api_votes_toggle(idea_id):
    db = get_db()
    existing = db.execute(
        'SELECT id FROM votes WHERE idea_id = ? AND user_id = ?',
        (idea_id, session['user_id'])
    ).fetchone()
    if existing:
        db.execute('DELETE FROM votes WHERE idea_id = ? AND user_id = ?',
                   (idea_id, session['user_id']))
    else:
        db.execute('INSERT INTO votes (idea_id, user_id) VALUES (?, ?)',
                   (idea_id, session['user_id']))
    db.commit()
    count = db.execute('SELECT COUNT(*) FROM votes WHERE idea_id = ?', (idea_id,)).fetchone()[0]
    user_voted = not bool(existing)
    db.close()
    return jsonify({'count': count, 'user_voted': user_voted})


# ─── Routes: Meetings (porady) ────────────────────────────────────────────────
@app.route('/api/meetings', methods=['GET'])
@login_required
def api_meetings_list():
    db = get_db()
    rows = db.execute('SELECT * FROM meetings ORDER BY meeting_date DESC').fetchall()
    result = []
    for m in rows:
        d = dict(m)
        # Get linked ideas count
        cnt = db.execute('SELECT COUNT(*) FROM meeting_ideas WHERE meeting_id = ?', (m['id'],)).fetchone()[0]
        d['ideas_count'] = cnt
        result.append(d)
    db.close()
    return jsonify(result)


@app.route('/api/meetings', methods=['POST'])
@reviewer_required
def api_meetings_create():
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Názov porady je povinný'}), 400

    db = get_db()
    cursor = db.execute(
        'INSERT INTO meetings (title, description, meeting_date, created_by, created_by_name) VALUES (?, ?, ?, ?, ?)',
        (title, data.get('description', ''), data.get('meeting_date', ''),
         session['user_id'], session['user_name'])
    )
    meeting_id = cursor.lastrowid
    db.commit()
    db.close()
    return jsonify({'ok': True, 'id': meeting_id}), 201


@app.route('/api/meetings/<int:meeting_id>', methods=['GET'])
@login_required
def api_meeting_detail(meeting_id):
    db = get_db()
    m = db.execute('SELECT * FROM meetings WHERE id = ?', (meeting_id,)).fetchone()
    if not m:
        db.close()
        return jsonify({'error': 'Porada nenájdená'}), 404
    d = dict(m)
    # Get linked ideas
    idea_rows = db.execute('''
        SELECT i.* FROM ideas i
        JOIN meeting_ideas mi ON mi.idea_id = i.id
        WHERE mi.meeting_id = ?
        ORDER BY i.created_at DESC
    ''', (meeting_id,)).fetchall()
    d['ideas'] = [dict(r) for r in idea_rows]
    db.close()
    return jsonify(d)


@app.route('/api/meetings/<int:meeting_id>', methods=['PATCH'])
@reviewer_required
def api_meeting_update(meeting_id):
    data = request.get_json() or {}
    allowed = {'title', 'description', 'meeting_date', 'status', 'notes'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'Nič na aktualizáciu'}), 400
    db = get_db()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE meetings SET {set_clause} WHERE id = ?', list(updates.values()) + [meeting_id])
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/meetings/<int:meeting_id>/ideas', methods=['POST'])
@reviewer_required
def api_meeting_add_idea(meeting_id):
    data = request.get_json() or {}
    idea_id = data.get('idea_id')
    if not idea_id:
        return jsonify({'error': 'idea_id je povinné'}), 400
    db = get_db()
    try:
        db.execute('INSERT INTO meeting_ideas (meeting_id, idea_id) VALUES (?, ?)', (meeting_id, idea_id))
        db.commit()
    except Exception:
        pass  # Already linked
    db.close()
    return jsonify({'ok': True})


@app.route('/api/meetings/<int:meeting_id>/ideas/<int:idea_id>', methods=['DELETE'])
@reviewer_required
def api_meeting_remove_idea(meeting_id, idea_id):
    db = get_db()
    db.execute('DELETE FROM meeting_ideas WHERE meeting_id = ? AND idea_id = ?', (meeting_id, idea_id))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/meetings/<int:meeting_id>', methods=['DELETE'])
@admin_required
def api_meeting_delete(meeting_id):
    db = get_db()
    db.execute('DELETE FROM meeting_ideas WHERE meeting_id = ?', (meeting_id,))
    db.execute('DELETE FROM meetings WHERE id = ?', (meeting_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


# ─── Routes: Campaigns ────────────────────────────────────────────────────────
@app.route('/api/campaigns', methods=['GET'])
@login_required
def api_campaigns_list():
    db = get_db()
    rows = db.execute('SELECT * FROM campaigns ORDER BY created_at DESC').fetchall()
    result = []
    for c in rows:
        d = dict(c)
        cnt = db.execute('SELECT COUNT(*) FROM ideas WHERE campaign_id = ?', (c['id'],)).fetchone()[0]
        d['ideas_count'] = cnt
        result.append(d)
    db.close()
    return jsonify(result)


@app.route('/api/campaigns', methods=['POST'])
@reviewer_required
def api_campaigns_create():
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Názov kampane je povinný'}), 400

    db = get_db()
    cursor = db.execute(
        'INSERT INTO campaigns (title, description, start_date, end_date, created_by) VALUES (?, ?, ?, ?, ?)',
        (title, data.get('description', ''), data.get('start_date', ''),
         data.get('end_date', ''), session['user_id'])
    )
    campaign_id = cursor.lastrowid
    db.commit()
    db.close()
    return jsonify({'ok': True, 'id': campaign_id}), 201


@app.route('/api/campaigns/<int:campaign_id>', methods=['GET'])
@login_required
def api_campaign_detail(campaign_id):
    db = get_db()
    c = db.execute('SELECT * FROM campaigns WHERE id = ?', (campaign_id,)).fetchone()
    if not c:
        db.close()
        return jsonify({'error': 'Kampaň nenájdená'}), 404
    d = dict(c)
    ideas = db.execute('SELECT * FROM ideas WHERE campaign_id = ? ORDER BY created_at DESC',
                       (campaign_id,)).fetchall()
    d['ideas'] = [dict(r) for r in ideas]
    db.close()
    return jsonify(d)


@app.route('/api/campaigns/<int:campaign_id>', methods=['PATCH'])
@reviewer_required
def api_campaign_update(campaign_id):
    data = request.get_json() or {}
    allowed = {'title', 'description', 'start_date', 'end_date', 'status'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'Nič na aktualizáciu'}), 400
    db = get_db()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE campaigns SET {set_clause} WHERE id = ?', list(updates.values()) + [campaign_id])
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/campaigns/<int:campaign_id>', methods=['DELETE'])
@admin_required
def api_campaign_delete(campaign_id):
    db = get_db()
    # Unlink ideas from campaign
    db.execute('UPDATE ideas SET campaign_id = NULL WHERE campaign_id = ?', (campaign_id,))
    db.execute('DELETE FROM campaigns WHERE id = ?', (campaign_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


# ─── Routes: Pages ────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    return send_from_directory('static', 'index.html')


@app.route('/admin')
@login_required
def admin_page():
    return send_from_directory('static', 'index.html')


@app.route('/recorder')
@login_required
def recorder_page():
    return send_from_directory('static', 'recorder.html')


@app.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})


# ─── Start ────────────────────────────────────────────────────────────────────
with app.app_context():
    init_db()

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
