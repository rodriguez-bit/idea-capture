"""
Votes routes blueprint: get and toggle votes on ideas.
"""

from flask import Blueprint, jsonify, session

from auth import login_required
from database import get_db

votes_bp = Blueprint('votes', __name__)


@votes_bp.route('/api/ideas/<int:idea_id>/votes', methods=['GET'])
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


@votes_bp.route('/api/ideas/<int:idea_id>/votes', methods=['POST'])
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
