"""
Meetings routes blueprint: CRUD for meetings and meeting-idea links.
"""

from flask import Blueprint, request, jsonify, session

from config import logger
from auth import login_required, reviewer_required, admin_required
from database import get_db

meetings_bp = Blueprint('meetings', __name__)


@meetings_bp.route('/api/meetings', methods=['GET'])
@login_required
def api_meetings_list():
    db = get_db()
    rows = db.execute('SELECT * FROM meetings ORDER BY meeting_date DESC').fetchall()
    result = []
    for m in rows:
        d = dict(m)
        cnt = db.execute('SELECT COUNT(*) FROM meeting_ideas WHERE meeting_id = ?', (m['id'],)).fetchone()[0]
        d['ideas_count'] = cnt
        result.append(d)
    db.close()
    return jsonify(result)


@meetings_bp.route('/api/meetings', methods=['POST'])
@reviewer_required
def api_meetings_create():
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Nazov porady je povinny', 'code': 'missing_title'}), 400

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


@meetings_bp.route('/api/meetings/<int:meeting_id>', methods=['GET'])
@login_required
def api_meeting_detail(meeting_id):
    db = get_db()
    m = db.execute('SELECT * FROM meetings WHERE id = ?', (meeting_id,)).fetchone()
    if not m:
        db.close()
        return jsonify({'error': 'Porada nenajdena', 'code': 'not_found'}), 404
    d = dict(m)
    idea_rows = db.execute('''
        SELECT i.* FROM ideas i
        JOIN meeting_ideas mi ON mi.idea_id = i.id
        WHERE mi.meeting_id = ?
        ORDER BY i.created_at DESC
    ''', (meeting_id,)).fetchall()
    d['ideas'] = [dict(r) for r in idea_rows]
    db.close()
    return jsonify(d)


@meetings_bp.route('/api/meetings/<int:meeting_id>', methods=['PATCH'])
@reviewer_required
def api_meeting_update(meeting_id):
    data = request.get_json() or {}
    allowed = {'title', 'description', 'meeting_date', 'status', 'notes'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'Nic na aktualizaciu', 'code': 'no_updates'}), 400
    db = get_db()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE meetings SET {set_clause} WHERE id = ?', list(updates.values()) + [meeting_id])
    db.commit()
    db.close()
    return jsonify({'ok': True})


@meetings_bp.route('/api/meetings/<int:meeting_id>/ideas', methods=['POST'])
@reviewer_required
def api_meeting_add_idea(meeting_id):
    data = request.get_json() or {}
    idea_id = data.get('idea_id')
    if not idea_id:
        return jsonify({'error': 'idea_id je povinne', 'code': 'missing_idea_id'}), 400
    db = get_db()
    try:
        db.execute('INSERT INTO meeting_ideas (meeting_id, idea_id) VALUES (?, ?)', (meeting_id, idea_id))
        db.commit()
    except Exception as e:
        logger.warning('Meeting-idea link already exists or error: %s', e)
    db.close()
    return jsonify({'ok': True})


@meetings_bp.route('/api/meetings/<int:meeting_id>/ideas/<int:idea_id>', methods=['DELETE'])
@reviewer_required
def api_meeting_remove_idea(meeting_id, idea_id):
    db = get_db()
    db.execute('DELETE FROM meeting_ideas WHERE meeting_id = ? AND idea_id = ?', (meeting_id, idea_id))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@meetings_bp.route('/api/meetings/<int:meeting_id>', methods=['DELETE'])
@admin_required
def api_meeting_delete(meeting_id):
    db = get_db()
    db.execute('DELETE FROM meeting_ideas WHERE meeting_id = ?', (meeting_id,))
    db.execute('DELETE FROM meetings WHERE id = ?', (meeting_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})
