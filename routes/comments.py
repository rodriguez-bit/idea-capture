"""
Comments routes blueprint: list, create, delete comments on ideas.
"""

from flask import Blueprint, request, jsonify, session

from auth import login_required
from database import get_db

comments_bp = Blueprint('comments', __name__)


@comments_bp.route('/api/ideas/<int:idea_id>/comments', methods=['GET'])
@login_required
def api_comments_list(idea_id):
    db = get_db()
    rows = db.execute(
        'SELECT * FROM comments WHERE idea_id = ? ORDER BY created_at ASC', (idea_id,)
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@comments_bp.route('/api/ideas/<int:idea_id>/comments', methods=['POST'])
@login_required
def api_comments_create(idea_id):
    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Text komentara je povinny', 'code': 'missing_text'}), 400
    if len(text) > 2000:
        return jsonify({'error': 'Komentar je prilis dlhy (max 2000 znakov)', 'code': 'text_too_long'}), 400

    db = get_db()
    idea = db.execute('SELECT id FROM ideas WHERE id = ?', (idea_id,)).fetchone()
    if not idea:
        db.close()
        return jsonify({'error': 'Napad nenajdeny', 'code': 'not_found'}), 404

    db.execute(
        'INSERT INTO comments (idea_id, user_id, user_name, text) VALUES (?, ?, ?, ?)',
        (idea_id, session['user_id'], session['user_name'], text)
    )
    db.commit()
    db.close()
    return jsonify({'ok': True}), 201


@comments_bp.route('/api/comments/<int:comment_id>', methods=['DELETE'])
@login_required
def api_comments_delete(comment_id):
    db = get_db()
    comment = db.execute('SELECT * FROM comments WHERE id = ?', (comment_id,)).fetchone()
    if not comment:
        db.close()
        return jsonify({'error': 'Komentar nenajdeny', 'code': 'not_found'}), 404
    # Only author or admin can delete
    if comment['user_id'] != session['user_id'] and session.get('user_role') != 'admin':
        db.close()
        return jsonify({'error': 'Nemate opravnenie', 'code': 'forbidden'}), 403
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})
