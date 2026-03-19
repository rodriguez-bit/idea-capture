"""
GitHub backup services: push/fetch files, save ideas and users backups.
"""

import json
import base64
import threading

import requests

from config import (
    GITHUB_TOKEN, GITHUB_REPO, BACKUP_BRANCH,
    _backup_lock, logger
)

# Module-level flag for branch readiness (thread-safe via _backup_lock)
_branch_ready = False


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
        logger.error('GitHub fetch error: %s', e)
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
        logger.error('GitHub push error: %s', e)


def save_ideas_backup():
    try:
        from database import get_db
        db = get_db()
        # Exclude audio_data from backup to save memory and bandwidth
        cols = ('author_id, author_name, department, role, audio_filename, '
                'duration_seconds, transcript, status, ai_score, ai_analysis, '
                'reviewer_note, reviewed_by, reviewed_at, created_at, visibility, '
                'tags, assigned_to, deadline, campaign_id, transcribed_at, stt_engine, idea_type')
        rows = db.execute(f'SELECT {cols} FROM ideas ORDER BY id').fetchall()
        db.close()
        data = []
        for r in rows:
            d = dict(r)
            data.append(d)
        content = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        with open('ideas_backup.json', 'w', encoding='utf-8') as f:
            f.write(content)
        threading.Thread(target=_github_push_file,
                         args=('ideas_backup.json', content.encode('utf-8'), 'Auto-backup ideas'),
                         daemon=True).start()
    except Exception as e:
        logger.error('Backup error: %s', e)


def save_users_backup():
    try:
        from database import get_db
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
        logger.error('Users backup error: %s', e)
