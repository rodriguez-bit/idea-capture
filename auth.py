"""
Authentication blueprint: login/logout, decorators, password change.
"""

import time
import functools

from flask import Blueprint, request, jsonify, session, redirect, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

from config import _failed_logins, _failed_logins_lock, logger
from database import get_db
from services.backup import save_users_backup

auth_bp = Blueprint('auth', __name__)


# ─── Auth decorators ─────────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            if request.path.startswith('/api/'):
                logger.warning(
                    'AUTH FAIL: %s %s from origin=%s UA=%s',
                    request.method, request.path,
                    request.headers.get('Origin', '?'),
                    request.headers.get('User-Agent', '?')[:60]
                )
                return jsonify({'error': 'Unauthorized', 'code': 'unauthorized'}), 401
            return redirect('/login?next=' + request.path)
        return f(*args, **kwargs)
    return decorated


def reviewer_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Unauthorized', 'code': 'unauthorized'}), 401
        if session.get('user_role') not in ('reviewer', 'admin'):
            return jsonify({'error': 'Forbidden', 'code': 'forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Unauthorized', 'code': 'unauthorized'}), 401
        if session.get('user_role') != 'admin':
            return jsonify({'error': 'Forbidden', 'code': 'forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


# ─── Routes ──────────────────────────────────────────────────────────────────
@auth_bp.route('/login')
def login_page():
    if session.get('authenticated'):
        nxt = request.args.get('next', '/recorder')
        return redirect(nxt)
    return send_from_directory('static', 'login.html')


@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    ip = request.remote_addr
    now = time.time()
    with _failed_logins_lock:
        attempts = _failed_logins.get(ip, [])
        attempts = [t for t in attempts if now - t < 300]
        if len(attempts) >= 5:
            return jsonify({'error': 'Prilis vela pokusov. Skuste za 5 minut.', 'code': 'rate_limited'}), 429
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
        return jsonify({'error': 'Nespravny e-mail alebo heslo', 'code': 'invalid_credentials'}), 401
    session.permanent = True
    session['authenticated'] = True
    session['user_id'] = user['id']
    session['user_email'] = user['email']
    session['user_name'] = user['display_name']
    session['user_role'] = user['role']
    session['user_department'] = user['department']
    return jsonify({'ok': True, 'name': user['display_name'], 'role': user['role']})


@auth_bp.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@auth_bp.route('/api/current-user')
@login_required
def api_current_user():
    return jsonify({
        'id': session['user_id'],
        'name': session['user_name'],
        'email': session['user_email'],
        'role': session['user_role'],
        'department': session.get('user_department', '')
    })


@auth_bp.route('/api/change-password', methods=['POST'])
@login_required
def api_change_password():
    data = request.get_json() or {}
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')

    if not current_password or not new_password:
        return jsonify({'error': 'Aktualne heslo a nove heslo su povinne', 'code': 'missing_fields'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Nove heslo musi mat aspon 6 znakov', 'code': 'password_too_short'}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user or not check_password_hash(user['password_hash'], current_password):
        db.close()
        return jsonify({'error': 'Nespravne aktualne heslo', 'code': 'invalid_password'}), 401

    db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
               (generate_password_hash(new_password), session['user_id']))
    db.commit()
    db.close()
    save_users_backup()
    return jsonify({'ok': True})
