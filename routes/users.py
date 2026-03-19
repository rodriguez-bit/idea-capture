"""
Users routes blueprint: list, create, update users (admin only).
"""

from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash

from auth import admin_required
from database import get_db
from services.backup import save_users_backup

users_bp = Blueprint('users', __name__)


@users_bp.route('/api/users', methods=['GET'])
@admin_required
def api_users_list():
    db = get_db()
    rows = db.execute('SELECT id, email, display_name, role, department, active, created_at FROM users ORDER BY display_name').fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@users_bp.route('/api/users', methods=['POST'])
@admin_required
def api_users_create():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    name = (data.get('display_name') or '').strip()
    password = data.get('password') or ''
    role = data.get('role', 'submitter')
    department = data.get('department', '')

    if not email or not name or not password:
        return jsonify({'error': 'E-mail, meno a heslo su povinne', 'code': 'missing_fields'}), 400
    if role not in ('submitter', 'reviewer', 'admin'):
        return jsonify({'error': 'Neplatna rola', 'code': 'invalid_role'}), 400

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
    except Exception:
        db.close()
        return jsonify({'error': 'E-mail uz existuje', 'code': 'duplicate_email'}), 409


@users_bp.route('/api/users/<int:user_id>', methods=['PATCH'])
@admin_required
def api_users_update(user_id):
    data = request.get_json() or {}
    allowed = {'display_name', 'role', 'department', 'active'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if 'password' in data and data['password']:
        updates['password_hash'] = generate_password_hash(data['password'])
    if not updates:
        return jsonify({'error': 'Nic na aktualizaciu', 'code': 'no_updates'}), 400
    db = get_db()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE users SET {set_clause} WHERE id = ?', list(updates.values()) + [user_id])
    db.commit()
    db.close()
    save_users_backup()
    return jsonify({'ok': True})
