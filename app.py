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
def set_headers(response):
    origin = request.headers.get('Origin', '')
    if origin in _ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response

@app.route('/api/<path:path>', methods=['OPTIONS'])
@app.route('/api/', methods=['OPTIONS'])
def options_handler(path=''):
    return jsonify({}), 200


# ─── Auth helpers ─────────────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if not session.get('user_id'):
                return jsonify({'error': 'Unauthorized'}), 401
            if session.get('role') not in roles:
                return jsonify({'error': 'Forbidden'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# ─── DB init ──────────────────────────────────────────────────────────────────
def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        # Read and execute schema
        schema_file = 'schema_pg.sql' if DATABASE_URL else 'schema.sql'
        if os.path.exists(schema_file):
            with open(schema_file) as f:
                sql = f.read()
            if DATABASE_URL:
                # Execute PostgreSQL schema statements
                statements = [s.strip() for s in sql.split(';') if s.strip()]
                for stmt in statements:
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        # Ignore errors for IF NOT EXISTS
                        pass
            else:
                cur.executescript(sql)
        conn.commit()

        # Ensure default admin exists
        if DATABASE_URL:
            cur.execute("SELECT id FROM users WHERE username = %s", ('admin',))
        else:
            cur.execute("SELECT id FROM users WHERE username = ?", ('admin',))
        if not cur.fetchone():
            pw = generate_password_hash(os.environ.get('ADMIN_PASSWORD', 'admin123'))
            if DATABASE_URL:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role, department) VALUES (%s, %s, %s, %s)",
                    ('admin', pw, 'c-level', 'management')
                )
            else:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role, department) VALUES (?, ?, ?, ?)",
                    ('admin', pw, 'c-level', 'management')
                )
            conn.commit()


# ─── GitHub backup helpers ────────────────────────────────────────────────────
def _gh_headers():
    return {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
    }


def _ensure_backup_branch():
    global _branch_ready
    with _backup_lock:
        if _branch_ready:
            return
        base = f'https://api.github.com/repos/{GITHUB_REPO}'
        r = requests.get(f'{base}/branches/{BACKUP_BRANCH}', headers=_gh_headers(), timeout=10)
        if r.status_code == 200:
            _branch_ready = True
            return
        # Get default branch SHA
        r2 = requests.get(f'{base}/git/refs/heads/main', headers=_gh_headers(), timeout=10)
        if r2.status_code != 200:
            r2 = requests.get(f'{base}/git/refs/heads/master', headers=_gh_headers(), timeout=10)
        if r2.status_code != 200:
            return
        sha = r2.json()['object']['sha']
        requests.post(
            f'{base}/git/refs',
            headers=_gh_headers(),
            json={'ref': f'refs/heads/{BACKUP_BRANCH}', 'sha': sha},
            timeout=10
        )
        _branch_ready = True


def backup_db_to_github():
    if not GITHUB_TOKEN:
        return
    threading.Thread(target=_do_backup, daemon=True).start()


def _do_backup():
    try:
        _ensure_backup_branch()
        with open(DB_PATH, 'rb') as f:
            content = base64.b64encode(f.read()).decode()
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        path = f'backups/ideas_{ts}.db'
        base = f'https://api.github.com/repos/{GITHUB_REPO}'
        requests.put(
            f'{base}/contents/{path}',
            headers=_gh_headers(),
            json={
                'message': f'backup {ts}',
                'content': content,
                'branch': BACKUP_BRANCH,
            },
            timeout=30
        )
    except Exception:
        pass


# ─── Auth routes ──────────────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(force=True)
    username = data.get('username', '').strip()
    password = data.get('password', '')

    # Rate-limit failed logins
    now = time.time()
    with _failed_logins_lock:
        entry = _failed_logins.get(username, {'count': 0, 'until': 0})
        if now < entry['until']:
            remaining = int(entry['until'] - now)
            return jsonify({'error': f'Too many failed attempts. Try again in {remaining}s'}), 429

    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("SELECT id, password_hash, role, department FROM users WHERE username = %s", (username,))
        else:
            cur.execute("SELECT id, password_hash, role, department FROM users WHERE username = ?", (username,))
        row = cur.fetchone()

    if not row or not check_password_hash(row[1], password):
        with _failed_logins_lock:
            entry = _failed_logins.get(username, {'count': 0, 'until': 0})
            entry['count'] += 1
            if entry['count'] >= 5:
                entry['until'] = now + 300  # 5 min lockout
                entry['count'] = 0
            _failed_logins[username] = entry
        return jsonify({'error': 'Invalid credentials'}), 401

    # Clear failed logins on success
    with _failed_logins_lock:
        _failed_logins.pop(username, None)

    session.permanent = True
    session['user_id'] = row[0]
    session['username'] = username
    session['role'] = row[2]
    session['department'] = row[3]
    return jsonify({'username': username, 'role': row[2], 'department': row[3]})


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/me')
def me():
    if not session.get('user_id'):
        return jsonify({'error': 'Not logged in'}), 401
    return jsonify({
        'username': session.get('username'),
        'role': session.get('role'),
        'department': session.get('department'),
    })


@app.route('/api/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.get_json(force=True)
    current = data.get('current_password', '')
    new_pw = data.get('new_password', '')
    if not current or not new_pw:
        return jsonify({'error': 'Missing fields'}), 400
    if len(new_pw) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    with get_db() as conn:
        cur = conn.cursor()
        uid = session['user_id']
        if DATABASE_URL:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", (uid,))
        else:
            cur.execute("SELECT password_hash FROM users WHERE id = ?", (uid,))
        row = cur.fetchone()
        if not row or not check_password_hash(row[0], current):
            return jsonify({'error': 'Current password is incorrect'}), 403
        new_hash = generate_password_hash(new_pw)
        if DATABASE_URL:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, uid))
        else:
            cur.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, uid))
        conn.commit()
    return jsonify({'ok': True})


# ─── User management ──────────────────────────────────────────────────────────
@app.route('/api/users', methods=['GET'])
@role_required('c-level', 'majo-markech')
def list_users():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, department FROM users ORDER BY username")
        rows = cur.fetchall()
        cols = get_column_names(cur)
    return jsonify([dict(zip(cols, r)) for r in rows])


@app.route('/api/users', methods=['POST'])
@role_required('c-level', 'majo-markech')
def create_user():
    data = request.get_json(force=True)
    username = data.get('username', '').strip()
    password = data.get('password', '')
    role = data.get('role', 'employee')
    department = data.get('department', 'other')
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if role not in ROLES:
        return jsonify({'error': 'Invalid role'}), 400
    if department not in DEPARTMENTS:
        return jsonify({'error': 'Invalid department'}), 400
    pw_hash = generate_password_hash(password)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if DATABASE_URL:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role, department) VALUES (%s, %s, %s, %s) RETURNING id",
                    (username, pw_hash, role, department)
                )
                new_id = cur.fetchone()[0]
            else:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role, department) VALUES (?, ?, ?, ?)",
                    (username, pw_hash, role, department)
                )
                new_id = cur.lastrowid
            conn.commit()
    except Exception as e:
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': 'Username already exists'}), 409
        return jsonify({'error': str(e)}), 500
    return jsonify({'id': new_id, 'username': username, 'role': role, 'department': department}), 201


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@role_required('c-level', 'majo-markech')
def update_user(user_id):
    data = request.get_json(force=True)
    role = data.get('role')
    department = data.get('department')
    password = data.get('password')
    if role and role not in ROLES:
        return jsonify({'error': 'Invalid role'}), 400
    if department and department not in DEPARTMENTS:
        return jsonify({'error': 'Invalid department'}), 400
    with get_db() as conn:
        cur = conn.cursor()
        if role:
            if DATABASE_URL:
                cur.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
            else:
                cur.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        if department:
            if DATABASE_URL:
                cur.execute("UPDATE users SET department = %s WHERE id = %s", (department, user_id))
            else:
                cur.execute("UPDATE users SET department = ? WHERE id = ?", (department, user_id))
        if password:
            if len(password) < 6:
                return jsonify({'error': 'Password too short'}), 400
            pw_hash = generate_password_hash(password)
            if DATABASE_URL:
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (pw_hash, user_id))
            else:
                cur.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@role_required('c-level', 'majo-markech')
def delete_user(user_id):
    if user_id == session.get('user_id'):
        return jsonify({'error': 'Cannot delete yourself'}), 400
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        else:
            cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    return jsonify({'ok': True})


# ─── Ideas CRUD ───────────────────────────────────────────────────────────────
def _idea_visible(cur, idea_row, cols):
    """Check if the current user can see this idea."""
    role = session.get('role', '')
    dept = session.get('department', '')
    uid = session.get('user_id')
    idea = dict(zip(cols, idea_row))
    if role in ('c-level', 'majo-markech'):
        return True
    if role == 'manager':
        return idea.get('department') == dept
    # employee: own ideas + approved/implementing/done in their dept
    if idea.get('user_id') == uid:
        return True
    if idea.get('department') == dept and idea.get('status') in ('approved', 'v_realizacii', 'done'):
        return True
    return False


@app.route('/api/ideas', methods=['GET'])
@login_required
def list_ideas():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM ideas ORDER BY created_at DESC")
        rows = cur.fetchall()
        cols = get_column_names(cur)
    role = session.get('role', '')
    dept = session.get('department', '')
    uid = session.get('user_id')
    if role in ('c-level', 'majo-markech'):
        visible = rows
    elif role == 'manager':
        visible = [r for r in rows if dict(zip(cols, r)).get('department') == dept]
    else:
        visible = [r for r in rows if _idea_visible(cur, r, cols)]
    return jsonify([dict(zip(cols, r)) for r in visible])


@app.route('/api/ideas', methods=['POST'])
@login_required
def create_idea():
    data = request.get_json(force=True)
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()
    department = data.get('department', session.get('department', 'other'))
    assigned_to = data.get('assigned_to', '').strip() if data.get('assigned_to') else None
    deadline = data.get('deadline') or None
    if not title:
        return jsonify({'error': 'Title required'}), 400
    if department not in DEPARTMENTS:
        department = session.get('department', 'other')
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute(
                """INSERT INTO ideas (title, description, department, status, user_id, username, created_at, updated_at, assigned_to, deadline)
                   VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s) RETURNING id""",
                (title, description, department, session['user_id'], session['username'], now, now, assigned_to, deadline)
            )
            new_id = cur.fetchone()[0]
        else:
            cur.execute(
                """INSERT INTO ideas (title, description, department, status, user_id, username, created_at, updated_at, assigned_to, deadline)
                   VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
                (title, description, department, session['user_id'], session['username'], now, now, assigned_to, deadline)
            )
            new_id = cur.lastrowid
        conn.commit()
    # Trigger async analysis
    threading.Thread(target=_analyze_idea, args=(new_id,), daemon=True).start()
    return jsonify({'id': new_id, 'title': title}), 201


@app.route('/api/ideas/<int:idea_id>', methods=['GET'])
@login_required
def get_idea(idea_id):
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("SELECT * FROM ideas WHERE id = %s", (idea_id,))
        else:
            cur.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,))
        row = cur.fetchone()
        cols = get_column_names(cur)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    if not _idea_visible(cur, row, cols):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(dict(zip(cols, row)))


@app.route('/api/ideas/<int:idea_id>', methods=['PUT'])
@login_required
def update_idea(idea_id):
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("SELECT * FROM ideas WHERE id = %s", (idea_id,))
        else:
            cur.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,))
        row = cur.fetchone()
        cols = get_column_names(cur)
        if not row:
            return jsonify({'error': 'Not found'}), 404
        idea = dict(zip(cols, row))

        role = session.get('role')
        uid = session.get('user_id')

        data = request.get_json(force=True)
        updates = {}

        # Status transitions
        new_status = data.get('status')
        if new_status:
            allowed_statuses = ['pending', 'approved', 'rejected', 'v_realizacii', 'done']
            if new_status not in allowed_statuses:
                return jsonify({'error': 'Invalid status'}), 400
            if role in ('c-level', 'majo-markech'):
                updates['status'] = new_status
            elif role == 'manager':
                if new_status in ('approved', 'rejected', 'v_realizacii', 'done'):
                    updates['status'] = new_status
                else:
                    return jsonify({'error': 'Forbidden status transition'}), 403
            else:
                return jsonify({'error': 'Forbidden'}), 403

        # Field updates
        if 'title' in data and (role in ('c-level', 'majo-markech') or idea.get('user_id') == uid):
            updates['title'] = data['title']
        if 'description' in data and (role in ('c-level', 'majo-markech') or idea.get('user_id') == uid):
            updates['description'] = data['description']
        if 'department' in data and role in ('c-level', 'majo-markech'):
            if data['department'] in DEPARTMENTS:
                updates['department'] = data['department']
        if 'assigned_to' in data and role in ('c-level', 'majo-markech', 'manager'):
            updates['assigned_to'] = data['assigned_to'] or None
        if 'deadline' in data and role in ('c-level', 'majo-markech', 'manager'):
            updates['deadline'] = data['deadline'] or None
        if 'priority' in data and role in ('c-level', 'majo-markech', 'manager'):
            updates['priority'] = data['priority']
        if 'estimated_effort' in data and role in ('c-level', 'majo-markech', 'manager'):
            updates['estimated_effort'] = data['estimated_effort']
        if 'tags' in data:
            updates['tags'] = data['tags']

        if not updates:
            return jsonify({'error': 'Nothing to update'}), 400

        updates['updated_at'] = datetime.utcnow().isoformat()
        set_clause = ', '.join(
            [f"{k} = %s" for k in updates] if DATABASE_URL else [f"{k} = ?" for k in updates]
        )
        vals = list(updates.values()) + [idea_id]
        if DATABASE_URL:
            cur.execute(f"UPDATE ideas SET {set_clause} WHERE id = %s", vals)
        else:
            cur.execute(f"UPDATE ideas SET {set_clause} WHERE id = ?", vals)
        conn.commit()
    return jsonify({'ok': True})


@app.route('/api/ideas/<int:idea_id>', methods=['DELETE'])
@login_required
def delete_idea(idea_id):
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("SELECT user_id FROM ideas WHERE id = %s", (idea_id,))
        else:
            cur.execute("SELECT user_id FROM ideas WHERE id = ?", (idea_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        role = session.get('role')
        uid = session.get('user_id')
        if role not in ('c-level', 'majo-markech') and row[0] != uid:
            return jsonify({'error': 'Forbidden'}), 403
        if DATABASE_URL:
            cur.execute("DELETE FROM ideas WHERE id = %s", (idea_id,))
        else:
            cur.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
        conn.commit()
    return jsonify({'ok': True})


# ─── CSV Export ───────────────────────────────────────────────────────────────
@app.route('/api/ideas/export/csv')
@login_required
def export_ideas_csv():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM ideas ORDER BY created_at DESC")
        rows = cur.fetchall()
        cols = get_column_names(cur)
    role = session.get('role', '')
    dept = session.get('department', '')
    uid = session.get('user_id')
    if role in ('c-level', 'majo-markech'):
        visible = rows
    elif role == 'manager':
        visible = [r for r in rows if dict(zip(cols, r)).get('department') == dept]
    else:
        visible = [r for r in rows if _idea_visible(cur, r, cols)]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=cols)
    writer.writeheader()
    for row in visible:
        writer.writerow(dict(zip(cols, row)))
    csv_content = output.getvalue()

    return Response(
        csv_content,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=ideas_export.csv'}
    )


# ─── Comments ─────────────────────────────────────────────────────────────────
@app.route('/api/ideas/<int:idea_id>/comments', methods=['GET'])
@login_required
def list_comments(idea_id):
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("SELECT id, idea_id, user_id, username, content, created_at FROM comments WHERE idea_id = %s ORDER BY created_at", (idea_id,))
        else:
            cur.execute("SELECT id, idea_id, user_id, username, content, created_at FROM comments WHERE idea_id = ? ORDER BY created_at", (idea_id,))
        rows = cur.fetchall()
        cols = get_column_names(cur)
    return jsonify([dict(zip(cols, r)) for r in rows])


@app.route('/api/ideas/<int:idea_id>/comments', methods=['POST'])
@login_required
def add_comment(idea_id):
    data = request.get_json(force=True)
    content = data.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Content required'}), 400
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute(
                "INSERT INTO comments (idea_id, user_id, username, content, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (idea_id, session['user_id'], session['username'], content, now)
            )
            new_id = cur.fetchone()[0]
        else:
            cur.execute(
                "INSERT INTO comments (idea_id, user_id, username, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (idea_id, session['user_id'], session['username'], content, now)
            )
            new_id = cur.lastrowid
        conn.commit()
    return jsonify({'id': new_id}), 201


# ─── Votes ────────────────────────────────────────────────────────────────────
@app.route('/api/ideas/<int:idea_id>/vote', methods=['POST'])
@login_required
def vote_idea(idea_id):
    data = request.get_json(force=True)
    value = data.get('value', 1)
    if value not in (1, -1, 0):
        return jsonify({'error': 'Invalid vote value'}), 400
    uid = session['user_id']
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("SELECT id, value FROM votes WHERE idea_id = %s AND user_id = %s", (idea_id, uid))
        else:
            cur.execute("SELECT id, value FROM votes WHERE idea_id = ? AND user_id = ?", (idea_id, uid))
        existing = cur.fetchone()
        if value == 0:
            if existing:
                if DATABASE_URL:
                    cur.execute("DELETE FROM votes WHERE idea_id = %s AND user_id = %s", (idea_id, uid))
                else:
                    cur.execute("DELETE FROM votes WHERE idea_id = ? AND user_id = ?", (idea_id, uid))
        elif existing:
            if DATABASE_URL:
                cur.execute("UPDATE votes SET value = %s WHERE idea_id = %s AND user_id = %s", (value, idea_id, uid))
            else:
                cur.execute("UPDATE votes SET value = ? WHERE idea_id = ? AND user_id = ?", (value, idea_id, uid))
        else:
            if DATABASE_URL:
                cur.execute("INSERT INTO votes (idea_id, user_id, value) VALUES (%s, %s, %s)", (idea_id, uid, value))
            else:
                cur.execute("INSERT INTO votes (idea_id, user_id, value) VALUES (?, ?, ?)", (idea_id, uid, value))
        # Update vote count on idea
        if DATABASE_URL:
            cur.execute("SELECT COALESCE(SUM(value),0) FROM votes WHERE idea_id = %s", (idea_id,))
        else:
            cur.execute("SELECT COALESCE(SUM(value),0) FROM votes WHERE idea_id = ?", (idea_id,))
        total = cur.fetchone()[0]
        if DATABASE_URL:
            cur.execute("UPDATE ideas SET votes = %s WHERE id = %s", (total, idea_id))
        else:
            cur.execute("UPDATE ideas SET votes = ? WHERE id = ?", (total, idea_id))
        conn.commit()
    return jsonify({'votes': total})


# ─── Audio transcription ──────────────────────────────────────────────────────
@app.route('/api/transcribe', methods=['POST'])
@login_required
def transcribe_audio():
    if 'audio' not in request.files:
        return jsonify({'error': 'No audio file'}), 400
    f = request.files['audio']
    ext = os.path.splitext(secure_filename(f.filename))[1].lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        return jsonify({'error': f'Unsupported audio format: {ext}'}), 400
    api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'OpenAI API key not configured'}), 503
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name
    try:
        with open(tmp_path, 'rb') as audio_file:
            resp = requests.post(
                'https://api.openai.com/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {api_key}'},
                files={'file': (f'audio{ext}', audio_file, 'application/octet-stream')},
                data={'model': 'whisper-1'},
                timeout=60
            )
        if resp.status_code != 200:
            return jsonify({'error': 'Transcription failed', 'detail': resp.text}), 502
        return jsonify({'text': resp.json().get('text', '')})
    finally:
        os.unlink(tmp_path)


# ─── Async upload ─────────────────────────────────────────────────────────────
@app.route('/api/upload-async', methods=['POST'])
@login_required
def upload_async():
    if 'audio' not in request.files:
        return jsonify({'error': 'No audio file'}), 400
    f = request.files['audio']
    ext = os.path.splitext(secure_filename(f.filename))[1].lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        return jsonify({'error': f'Unsupported audio format: {ext}'}), 400
    job_id = str(uuid.uuid4())
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name
    _upload_jobs[job_id] = {'status': 'processing'}
    threading.Thread(target=_process_upload, args=(job_id, tmp_path, ext), daemon=True).start()
    return jsonify({'job_id': job_id}), 202


def _process_upload(job_id, tmp_path, ext):
    try:
        api_key = os.environ.get('OPENAI_API_KEY', '')
        if not api_key:
            _upload_jobs[job_id] = {'status': 'error', 'error': 'OpenAI API key not configured'}
            return
        with open(tmp_path, 'rb') as audio_file:
            resp = requests.post(
                'https://api.openai.com/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {api_key}'},
                files={'file': (f'audio{ext}', audio_file, 'application/octet-stream')},
                data={'model': 'whisper-1'},
                timeout=120
            )
        if resp.status_code != 200:
            _upload_jobs[job_id] = {'status': 'error', 'error': resp.text}
            return
        text = resp.json().get('text', '')
        _upload_jobs[job_id] = {'status': 'done', 'text': text}
    except Exception as e:
        _upload_jobs[job_id] = {'status': 'error', 'error': str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.route('/api/upload-status/<job_id>')
@login_required
def upload_status(job_id):
    job = _upload_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


# ─── AI idea analysis ─────────────────────────────────────────────────────────
def _analyze_idea(idea_id):
    """Run GPT analysis on a newly created idea and save results."""
    api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        return
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if DATABASE_URL:
                cur.execute("SELECT title, description FROM ideas WHERE id = %s", (idea_id,))
            else:
                cur.execute("SELECT title, description FROM ideas WHERE id = ?", (idea_id,))
            row = cur.fetchone()
        if not row:
            return
        title, description = row
        prompt = (
            f"Analyze this business idea briefly:\n"
            f"Title: {title}\n"
            f"Description: {description or 'N/A'}\n\n"
            f"Respond in JSON with keys: summary (1 sentence), potential (high/medium/low), "
            f"risks (list of up to 3 short strings), next_steps (list of up to 3 short strings)."
        )
        resp = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': 'gpt-4o-mini',
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': 300,
                'response_format': {'type': 'json_object'},
            },
            timeout=30
        )
        if resp.status_code != 200:
            return
        content = resp.json()['choices'][0]['message']['content']
        analysis = json.loads(content)
        analysis_str = json.dumps(analysis)
        with get_db() as conn:
            cur = conn.cursor()
            if DATABASE_URL:
                cur.execute(
                    "UPDATE ideas SET ai_analysis = %s, updated_at = %s WHERE id = %s",
                    (analysis_str, datetime.utcnow().isoformat(), idea_id)
                )
            else:
                cur.execute(
                    "UPDATE ideas SET ai_analysis = ?, updated_at = ? WHERE id = ?",
                    (analysis_str, datetime.utcnow().isoformat(), idea_id)
                )
            conn.commit()
    except Exception:
        pass


@app.route('/api/ideas/<int:idea_id>/analyze', methods=['POST'])
@login_required
def analyze_idea(idea_id):
    """Trigger AI analysis for an idea (re-run or initial)."""
    threading.Thread(target=_analyze_idea, args=(idea_id,), daemon=True).start()
    return jsonify({'ok': True, 'message': 'Analysis started'})


# ─── Stats ────────────────────────────────────────────────────────────────────
@app.route('/api/stats')
@login_required
def stats():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*) FROM ideas GROUP BY status")
        by_status = dict(cur.fetchall())
        cur.execute("SELECT department, COUNT(*) FROM ideas GROUP BY department")
        by_dept = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM ideas")
        total = cur.fetchone()[0]
    return jsonify({'total': total, 'by_status': by_status, 'by_department': by_dept})


# ─── Serve SPA ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('static', path)


if __name__ == '__main__':
    init_db()
    debug = bool(os.environ.get('FLASK_DEBUG'))
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=debug)
