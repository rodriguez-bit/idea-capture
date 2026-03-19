"""
Pages routes blueprint: static pages, download proxy, health check, debug endpoints.
"""

import os
import io
import wave
import struct
import math
import tempfile
from datetime import datetime

from flask import Blueprint, request, jsonify, send_from_directory, redirect, Response, current_app
import requests

from config import _RELEASE_ASSETS, logger
from auth import login_required
from services.transcription import _transcribe_with_elevenlabs

pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
@login_required
def index():
    return send_from_directory('static', 'index.html')


@pages_bp.route('/admin')
@login_required
def admin_page():
    return send_from_directory('static', 'index.html')


@pages_bp.route('/recorder')
@login_required
def recorder_page():
    return send_from_directory('static', 'recorder.html')


@pages_bp.route('/electron-recorder')
def electron_recorder_page():
    # No login_required - the recorder page handles its own auth flow
    return send_from_directory('electron-app', 'recorder.html')


@pages_bp.route('/download')
def download_page():
    return send_from_directory('static', 'download.html')


@pages_bp.route('/dl/<platform>')
def download_asset(platform):
    asset = _RELEASE_ASSETS.get(platform)
    if not asset:
        return jsonify({'error': 'Not found', 'code': 'not_found'}), 404
    try:
        r = requests.get(asset['url'], stream=True, timeout=30, allow_redirects=True)
        r.raise_for_status()
        headers = {
            'Content-Disposition': f'attachment; filename="{asset["filename"]}"',
            'Content-Type': asset['mime'],
        }
        cl = r.headers.get('Content-Length')
        if cl:
            headers['Content-Length'] = cl
        return Response(r.iter_content(chunk_size=65536), headers=headers)
    except Exception as e:
        logger.error('Download proxy error for %s: %s', platform, e)
        return redirect(asset['url'])


@pages_bp.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@pages_bp.route('/health')
def health():
    el_key = os.environ.get('ELEVENLABS_API_KEY', '')
    oa_key = os.environ.get('OPENAI_API_KEY', '')
    el_import_ok = False
    el_import_error = ''
    el_version = ''
    try:
        from elevenlabs.client import ElevenLabs as _EL
        el_import_ok = True
        try:
            import elevenlabs
            el_version = getattr(elevenlabs, '__version__', 'unknown')
        except Exception as e:
            logger.warning('Could not get elevenlabs version: %s', e)
            el_version = 'unknown'
    except Exception as e:
        el_import_error = f'{type(e).__name__}: {e}'
    return jsonify({
        'status': 'ok',
        'time': datetime.now().isoformat(),
        'version': '3.2.3',
        'elevenlabs_key_set': bool(el_key),
        'elevenlabs_key_prefix': el_key[:8] + '...' if el_key else 'NOT SET',
        'elevenlabs_import_ok': el_import_ok,
        'elevenlabs_import_error': el_import_error,
        'elevenlabs_version': el_version,
        'openai_key_set': bool(oa_key),
    })


@pages_bp.route('/debug/test-elevenlabs')
@login_required
def debug_test_elevenlabs():
    """Test ElevenLabs Scribe directly on the server via REST API."""
    sample_rate = 16000
    duration = 1.0
    num_samples = int(sample_rate * duration)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(num_samples):
            val = int(16000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            wf.writeframes(struct.pack('<h', val))
    buf.seek(0)
    tmp = os.path.join(tempfile.gettempdir(), 'el_test.wav')
    with open(tmp, 'wb') as f:
        f.write(buf.read())

    result = {'step': 'init'}
    try:
        api_key = os.environ.get('ELEVENLABS_API_KEY', '')
        result['key_set'] = bool(api_key)
        result['key_prefix'] = api_key[:8] + '...' if api_key else 'NONE'

        result['step'] = 'rest_api'
        import requests as _req
        with open(tmp, 'rb') as f:
            resp = _req.post(
                'https://api.elevenlabs.io/v1/speech-to-text',
                headers={'xi-api-key': api_key},
                files={'file': ('test.wav', f)},
                data={'model_id': 'scribe_v2', 'language_code': 'slk',
                      'tag_audio_events': 'false', 'diarize': 'false'},
                timeout=60,
            )
        result['http_status'] = resp.status_code
        result['response_body'] = resp.text[:500]
        if resp.status_code == 200:
            data = resp.json()
            result['text'] = data.get('text', '')
            result['text_len'] = len(result['text'])
            result['success'] = True
        else:
            result['success'] = False
    except Exception as e:
        result['success'] = False
        result['error_type'] = type(e).__name__
        result['error'] = str(e)[:500]
    finally:
        try:
            os.unlink(tmp)
        except Exception as e:
            logger.warning('Failed to clean up test file: %s', e)
    return jsonify(result)
