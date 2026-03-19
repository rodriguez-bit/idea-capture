"""
Ridea - Idea Capture Application
Entry point: Flask app factory, middleware, blueprint registration.
v3.2.3
"""

import os
import logging
from datetime import timedelta

from flask import Flask, request, jsonify

from config import _ALLOWED_ORIGINS

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger('ridea')


def create_app():
    app = Flask(__name__, static_folder='static')
    app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'None'
    # FLASK_DEBUG env var is string 'false' - bool('false') is True in Python!
    # So we explicitly check for truthy values
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_DEBUG', '').lower() not in ('true', '1', 'yes')
    app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB

    # ─── CORS + Security headers ──────────────────────────────────────────
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
        # Cache headers for static assets
        path = request.path
        if path.startswith('/static/') or path in ('/manifest.json',):
            response.headers['Cache-Control'] = 'public, max-age=86400'  # 1 day
        elif path.endswith(('.png', '.ico', '.svg', '.woff2')):
            response.headers['Cache-Control'] = 'public, max-age=604800'  # 7 days
        elif path == '/sw.js':
            response.headers['Cache-Control'] = 'no-cache'  # always revalidate SW
        return response

    # ─── OPTIONS handler ──────────────────────────────────────────────────
    @app.route('/api/<path:path>', methods=['OPTIONS'])
    def handle_options(path):
        return '', 204

    # ─── CSRF protection ─────────────────────────────────────────────────
    @app.before_request
    def csrf_check():
        if request.method in ('POST', 'PATCH', 'DELETE'):
            origin = request.headers.get('Origin', '')
            if origin and origin not in _ALLOWED_ORIGINS:
                return jsonify({'error': 'Origin not allowed', 'code': 'csrf_error'}), 403

    # ─── Register blueprints ──────────────────────────────────────────────
    from auth import auth_bp
    from routes.ideas import ideas_bp
    from routes.users import users_bp
    from routes.comments import comments_bp
    from routes.votes import votes_bp
    from routes.meetings import meetings_bp
    from routes.campaigns import campaigns_bp
    from routes.stats import stats_bp
    from routes.pages import pages_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(ideas_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(comments_bp)
    app.register_blueprint(votes_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(campaigns_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(pages_bp)

    return app


app = create_app()

with app.app_context():
    from database import init_db
    init_db()

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
# v3.2.3
