import os
import json
import base64
import tempfile
import threading
import time
import functools
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, session, send_from_directory, redirect, url_for
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
                tags TEXT DEFAULT '[]'
            );

            CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas (status);
            CREATE INDEX IF NOT EXISTS idx_ideas_created ON ideas (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ideas_dept ON ideas (department);
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


# ─── Login page ───────────────────────────────────────────────────────────────
LOGIN_HTML = '''<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ridea — Prihlásenie</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f172a; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #1e293b; border-radius: 16px; padding: 48px 40px; width: 100%; max-width: 400px; box-shadow: 0 25px 50px rgba(0,0,0,0.5); }
  h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; }
  p { color: #94a3b8; font-size: 14px; margin-bottom: 32px; }
  label { display: block; font-size: 13px; font-weight: 500; color: #cbd5e1; margin-bottom: 6px; }
  input { width: 100%; padding: 12px 16px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; font-size: 15px; outline: none; margin-bottom: 16px; }
  input:focus { border-color: #6366f1; }
  button { width: 100%; padding: 13px; background: #6366f1; color: white; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 8px; }
  button:hover { background: #4f46e5; }
  .error { color: #f87171; font-size: 13px; margin-top: 12px; display: none; }
  .logo { font-size: 32px; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">💡</div>
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
    allowed = {'status', 'reviewer_note', 'visibility', 'tags'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if 'visibility' in updates and updates['visibility'] not in ('personal', 'company'):
        return jsonify({'error': 'Neplatná hodnota viditeľnosti'}), 400
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


@app.route('/api/ideas/upload', methods=['POST'])
@login_required
def api_ideas_upload():
    import openai

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

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            file.save(tmp)
            tmp_path = tmp.name

        client = openai.OpenAI(api_key=api_key)
        with open(tmp_path, 'rb') as f:
            transcription = client.audio.transcriptions.create(
                model='whisper-1',
                file=f,
                language='sk',
                response_format='verbose_json'
            )

        transcript_text = transcription.text or ''
        duration = int(getattr(transcription, 'duration', 0) or 0)

        db = get_db()
        cursor = db.execute('''
            INSERT INTO ideas (author_id, author_name, department, role, duration_seconds, transcript, status, visibility)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?)
        ''', (
            session['user_id'],
            session['user_name'],
            department,
            role,
            duration,
            transcript_text,
            visibility
        ))
        idea_id = cursor.lastrowid
        db.commit()
        db.close()
        save_ideas_backup()

        return jsonify({
            'id': idea_id,
            'transcript': transcript_text,
            'duration_seconds': duration,
            'message': 'Nápad úspešne zaznamenaný'
        })

    except Exception as e:
        print(f'Upload error: {e}')
        return jsonify({'error': f'Chyba pri spracovaní: {str(e)}'}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


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

    prompt = f"""Analyzuj nasledujúci interný nápad od zamestnanca a ohodnoť ho.

Oddelenie: {idea['department']}
Rola: {idea['role']}
Transkript nápadu:
"{transcript}"

Vráť JSON s týmto formátom (iba JSON, bez markdown):
{{
  "score": <1-10>,
  "clarity": <1-10>,
  "feasibility": <1-10>,
  "summary": "<2-3 vety zhrnutie nápadu>",
  "strengths": ["<silná stránka 1>", "<silná stránka 2>"],
  "weaknesses": ["<slabá stránka 1>"],
  "next_steps": ["<konkrétny krok 1>", "<konkrétny krok 2>"],
  "category": "<one of: process_improvement|cost_reduction|revenue|product|other>",
  "tags": ["<tag1>", "<tag2>"]
}}

Hodnoť objektívne. score je celkové hodnotenie potenciálu nápadu.
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
    return jsonify({'ok': True})


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
    db.close()
    return jsonify({
        'total': total,
        'by_status': by_status,
        'by_department': by_dept,
        'by_visibility': by_visibility,
        'recent': [dict(r) for r in recent]
    })


# ─── Routes: Pages ────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def index():
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
