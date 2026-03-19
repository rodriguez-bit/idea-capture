"""
Stats routes blueprint: dashboard stats, kanban, company context.
"""

from flask import Blueprint, request, jsonify

from config import COMPANY_CONTEXT_KEYS
from auth import login_required, admin_required
from database import get_db
from utils.validation import validate_limit

stats_bp = Blueprint('stats', __name__)


@stats_bp.route('/api/stats')
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
    recent = db.execute('SELECT id, author_name, department, transcript, ai_score, status, created_at FROM ideas ORDER BY created_at DESC LIMIT 5').fetchall()

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


@stats_bp.route('/api/kanban')
@login_required
def api_kanban():
    db = get_db()
    kanban_limit = validate_limit(request.args.get('limit'), default=50, max_val=200)
    statuses = ['new', 'in_review', 'accepted', 'v_realizacii', 'rejected']
    result = {}
    for s in statuses:
        rows = db.execute(
            'SELECT id, author_name, transcript, ai_score, assigned_to, deadline, tags FROM ideas WHERE status = ? ORDER BY created_at DESC LIMIT ?',
            (s, kanban_limit)
        ).fetchall()
        result[s] = [dict(r) for r in rows]
    db.close()
    return jsonify(result)


@stats_bp.route('/api/company-context', methods=['GET'])
@login_required
def api_company_context_get():
    db = get_db()
    rows = db.execute('SELECT key, value FROM company_context').fetchall()
    db.close()
    result = {k: '' for k in COMPANY_CONTEXT_KEYS}
    for row in rows:
        result[row['key']] = row['value']
    return jsonify(result)


@stats_bp.route('/api/company-context', methods=['POST'])
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
