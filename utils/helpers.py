"""
Common helper functions.
"""

from flask import jsonify


def api_error(message, code='error', status=400):
    return jsonify({'error': message, 'code': code}), status
