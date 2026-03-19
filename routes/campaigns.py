"""
Campaigns routes blueprint: CRUD for idea campaigns.
"""

from flask import Blueprint, request, jsonify, session

from auth import login_required, reviewer_required, admin_required
from database import get_db

campaigns_bp = Blueprint('campaigns', __name__)


@campaigns_bp.route('/api/campaigns', methods=['GET'])
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


@campaigns_bp.route('/api/campaigns', methods=['POST'])
@reviewer_required
def api_campaigns_create():
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Nazov kampane je povinny', 'code': 'missing_title'}), 400

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


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['GET'])
@login_required
def api_campaign_detail(campaign_id):
    db = get_db()
    c = db.execute('SELECT * FROM campaigns WHERE id = ?', (campaign_id,)).fetchone()
    if not c:
        db.close()
        return jsonify({'error': 'Kampan nenajdena', 'code': 'not_found'}), 404
    d = dict(c)
    ideas = db.execute('SELECT id, author_name, department, role, transcript, ai_score, status, visibility, tags, created_at FROM ideas WHERE campaign_id = ? ORDER BY created_at DESC',
                       (campaign_id,)).fetchall()
    d['ideas'] = [dict(r) for r in ideas]
    db.close()
    return jsonify(d)


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['PATCH'])
@reviewer_required
def api_campaign_update(campaign_id):
    data = request.get_json() or {}
    allowed = {'title', 'description', 'start_date', 'end_date', 'status'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'Nic na aktualizaciu', 'code': 'no_updates'}), 400
    db = get_db()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE campaigns SET {set_clause} WHERE id = ?', list(updates.values()) + [campaign_id])
    db.commit()
    db.close()
    return jsonify({'ok': True})


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['DELETE'])
@admin_required
def api_campaign_delete(campaign_id):
    db = get_db()
    db.execute('UPDATE ideas SET campaign_id = NULL WHERE campaign_id = ?', (campaign_id,))
    db.execute('DELETE FROM campaigns WHERE id = ?', (campaign_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})
