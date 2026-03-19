"""
Application configuration, constants, globals, and logging setup.
"""

import os
import logging
import threading

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger('ridea')

# ─── Database ─────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL', '')
DB_PATH = 'ideas.db'

# ─── GitHub backup ────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO = os.environ.get('GITHUB_REPO', 'dajanarodriguez/ridea')
BACKUP_BRANCH = 'data-backups'

_branch_ready = False
_backup_lock = threading.Lock()

# ─── File extensions ──────────────────────────────────────────────────────────
ALLOWED_AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.m4a', '.mp4', '.flac', '.webm', '.mpeg', '.opus'}
ALLOWED_DOCUMENT_EXTENSIONS = {'.pdf', '.docx', '.doc', '.txt', '.md', '.rtf', '.png', '.jpg', '.jpeg', '.gif', '.webp'}

# ─── Domain constants ────────────────────────────────────────────────────────
DEPARTMENTS = ['development', 'marketing', 'production', 'management', 'other']
ROLES = ['c-level', 'manager', 'employee']

COMPANY_CONTEXT_KEYS = [
    'company_description',   # O firme
    'goals_priorities',      # Ciele a priority
    'brand_values',          # Brand hodnoty
    'idea_criteria',         # Co hladame v napadoch
]

# ─── Release assets ───────────────────────────────────────────────────────────
_RELEASE_ASSETS = {
    'android': {
        'url': 'https://github.com/rodriguez-bit/idea-capture/releases/download/v2.5.0/Ridea-2.5.0.apk',
        'filename': 'Ridea-2.5.0.apk',
        'mime': 'application/vnd.android.package-archive',
    },
    'windows': {
        'url': 'https://github.com/rodriguez-bit/idea-capture/releases/download/v2.5.0/Ridea-Setup-2.5.0.exe',
        'filename': 'Ridea-Setup-2.5.0.exe',
        'mime': 'application/octet-stream',
    },
    'mac': {
        'url': 'https://github.com/rodriguez-bit/idea-capture/releases/download/v2.5.0/Ridea-2.5.0-mac.zip',
        'filename': 'Ridea-2.5.0-mac.zip',
        'mime': 'application/zip',
    },
}

# ─── CORS ─────────────────────────────────────────────────────────────────────
_ALLOWED_ORIGINS = {
    'null',  # Electron file:// origin
    'http://localhost:5000', 'http://localhost:5001',
    'https://ridea.onrender.com',
}

# ─── Failed login tracking ───────────────────────────────────────────────────
_failed_logins = {}
_failed_logins_lock = threading.Lock()


# ─── Thread-safe job store ────────────────────────────────────────────────────
class JobStore:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def set(self, job_id, data):
        with self._lock:
            self._jobs[job_id] = data

    def delete(self, job_id):
        with self._lock:
            self._jobs.pop(job_id, None)

    def update(self, job_id, key, value):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id][key] = value

    def __len__(self):
        with self._lock:
            return len(self._jobs)


upload_jobs = JobStore()
