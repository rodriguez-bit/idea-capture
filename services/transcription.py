"""
Audio transcription services: ElevenLabs Scribe, Whisper fallback,
hallucination detection, audio backup, background processing.
"""

import os
import re as re_module
import string
import shutil
import base64
import subprocess
import tempfile
import threading
from datetime import datetime

from config import upload_jobs, logger
from database import get_db
from services.backup import save_ideas_backup
from services.analysis import _auto_analyze


# ─── Known Whisper hallucination phrases ──────────────────────────────────────
_WHISPER_HALLUCINATION_BLACKLIST = [
    'dakujem za pozornost',
    'dakujem za pozornost',
    'dobre to je vsetko',
    'dobre to je vsetko',
    'dakujem',
    'dakujem',
    'thank you for watching',
    'thanks for watching',
    'thank you',
    'thanks for listening',
    'subscribe',
    'like and subscribe',
    'please subscribe',
    'subtitles by',
    'translated by',
    'amara.org',
    'copyright',
    'music',
    'applause',
    'silence',
    'you',
    'bye',
    'goodbye',
    'dovidenia',
    'na zhledanou',
    'na shledanou',
    'koniec',
    'the end',
    'end',
]


def _is_whisper_hallucination(text):
    """Check if text is a known Whisper hallucination on silent/noisy audio."""
    if not text:
        return True
    normalized = text.lower().strip().rstrip('.!?,;:')
    # Remove all punctuation for comparison
    clean = normalized.translate(str.maketrans('', '', string.punctuation))
    clean = ' '.join(clean.split())  # normalize whitespace
    # Exact match against blacklist
    if clean in _WHISPER_HALLUCINATION_BLACKLIST or normalized in _WHISPER_HALLUCINATION_BLACKLIST:
        return True
    # Very short text that's just punctuation or whitespace
    if len(clean) < 3:
        return True
    # Text is just one or two common words repeated
    words = clean.split()
    if len(words) <= 3 and len(set(words)) == 1:
        return True
    return False


def _clean_hallucinations(text):
    """Remove repeated phrases that indicate STT hallucination."""
    if not text or len(text) < 50:
        return text

    # Phase 1: Detect repeated short patterns within continuous text
    def remove_repeated_patterns(t):
        words = t.split()
        if len(words) < 10:
            return t
        # Try pattern lengths 1-5 words
        for plen in range(1, 6):
            i = 0
            result_words = []
            while i < len(words):
                pattern = words[i:i+plen]
                if len(pattern) < plen:
                    result_words.extend(words[i:])
                    break
                repeat_count = 0
                j = i
                while j + plen <= len(words) and words[j:j+plen] == pattern:
                    repeat_count += 1
                    j += plen
                if repeat_count >= 5:
                    result_words.extend(pattern)
                    i = j
                else:
                    result_words.append(words[i])
                    i += 1
            if len(result_words) < len(words) * 0.7:
                t = ' '.join(result_words)
                words = t.split()
        return t

    text = remove_repeated_patterns(text)

    # Phase 2: Sentence-level repetition detection
    sentences = re_module.split(r'[.!?]+', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) < 3:
        return text

    phrase_count = {}
    for s in sentences:
        normalized = s.lower().strip()
        if len(normalized) < 5:
            continue
        phrase_count[normalized] = phrase_count.get(normalized, 0) + 1

    hallucinated = set()
    for phrase, count in phrase_count.items():
        if count >= 3 and count > len(sentences) * 0.2:
            hallucinated.add(phrase)

    if not hallucinated:
        return text

    seen_hallucinated = set()
    clean_sentences = []
    for s in sentences:
        normalized = s.lower().strip()
        if normalized in hallucinated:
            if normalized not in seen_hallucinated:
                clean_sentences.append(s)
                seen_hallucinated.add(normalized)
        else:
            clean_sentences.append(s)

    cleaned = '. '.join(clean_sentences)
    if cleaned and not cleaned.endswith('.'):
        cleaned += '.'

    if len(cleaned) < len(text) * 0.2:
        return ''

    return cleaned


def _transcribe_with_elevenlabs(file_path, language='slk'):
    """Transcribe audio using ElevenLabs Scribe v2 REST API.
    Returns (transcript, duration, warning)."""
    api_key = os.environ.get('ELEVENLABS_API_KEY')
    if not api_key:
        logger.info('ElevenLabs: API key not set, skipping')
        return None, 0, None

    try:
        file_size = os.path.getsize(file_path)
        logger.info('ElevenLabs Scribe: Transcribing %.1fMB audio via REST API...', file_size / 1024 / 1024)

        import requests as _req
        with open(file_path, 'rb') as f:
            resp = _req.post(
                'https://api.elevenlabs.io/v1/speech-to-text',
                headers={'xi-api-key': api_key},
                files={'file': (os.path.basename(file_path), f)},
                data={
                    'model_id': 'scribe_v2',
                    'language_code': language,
                    'tag_audio_events': 'false',
                    'diarize': 'false',
                },
                timeout=120,
            )

        if resp.status_code == 401:
            body = resp.text[:500]
            if 'unusual_activity' in body or 'abuse' in body.lower() or 'Free Tier' in body:
                warning = 'ElevenLabs zablokoval prepis z dovodu "unusual activity" na zdielanom serveri. Pre odstranenie tohto problemu je potrebny plateny plan ElevenLabs. Prepis bol vykonany cez Whisper.'
                logger.warning('ElevenLabs Scribe: 401 - Free Tier blocked on shared IP (Render). Body: %s', body[:200])
                return None, 0, warning
            logger.warning('ElevenLabs Scribe: 401 Unauthorized - %s', body[:200])
            return None, 0, None

        if resp.status_code == 402:
            warning = 'ElevenLabs kredity boli vycerpane. Prepis bol vykonany cez zalozny system (Whisper). Pre lepsiu kvalitu prepisu doplnte kredity na elevenlabs.io.'
            logger.warning('ElevenLabs Scribe: Credits exhausted (402)')
            return None, 0, warning

        if resp.status_code != 200:
            logger.warning('ElevenLabs Scribe: HTTP %d - %s', resp.status_code, resp.text[:300])
            return None, 0, None

        data = resp.json()
        transcript = data.get('text', '')

        duration = 0
        words = data.get('words', [])
        if words:
            last_word = words[-1]
            duration = int(last_word.get('end', 0))

        lang_code = data.get('language_code', language)
        logger.info('ElevenLabs Scribe: %d chars, duration=%ds, lang=%s', len(transcript), duration, lang_code)

        if transcript and transcript.strip():
            if _is_whisper_hallucination(transcript.strip()):
                logger.info('ElevenLabs Scribe: Hallucination detected: "%s" - treating as valid but short', transcript.strip())
            return transcript.strip(), duration, None

        logger.info('ElevenLabs Scribe: Empty transcript returned (audio may be silent or too short)')
        return '', 0, None

    except Exception as e:
        err_str = str(e).lower()
        warning = None
        credit_keywords = ['insufficient credits', 'out of credits', 'credit limit',
                          'credits have been exhausted', 'no credits remaining']
        is_credit_error = any(kw in err_str for kw in credit_keywords)
        if is_credit_error:
            warning = 'ElevenLabs kredity boli vycerpane. Prepis bol vykonany cez zalozny system (Whisper). Pre lepsiu kvalitu prepisu doplnte kredity na elevenlabs.io.'
            logger.warning('ElevenLabs Scribe: Credits exhausted - %s', e)
        else:
            logger.error('ElevenLabs Scribe error: %s: %s', type(e).__name__, e)
        return None, 0, warning


def _split_audio_chunks(file_path, max_size_mb=20):
    """Split audio file into chunks under max_size_mb for Whisper API (25MB limit)."""
    file_size = os.path.getsize(file_path)
    if file_size <= max_size_mb * 1024 * 1024:
        return [file_path]

    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
            capture_output=True, text=True, timeout=30
        )
        total_duration = float(result.stdout.strip())
    except Exception as e:
        logger.warning('ffprobe failed, sending file as-is: %s', e)
        return [file_path]

    num_chunks = max(2, int(file_size / (max_size_mb * 1024 * 1024)) + 1)
    chunk_duration = total_duration / num_chunks

    chunks = []
    for i in range(num_chunks):
        start = i * chunk_duration
        chunk_path = file_path + f'.chunk{i}.mp3'
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-i', file_path, '-ss', str(start),
                 '-t', str(chunk_duration), '-ar', '16000', '-ac', '1',
                 '-b:a', '64k', chunk_path],
                capture_output=True, timeout=120
            )
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
                chunks.append(chunk_path)
        except Exception as e:
            logger.warning('ffmpeg chunk %d failed: %s', i, e)
            continue

    return chunks if chunks else [file_path]


def _save_audio_backup(tmp_path, ext, job_id):
    """Save raw audio file to backup directory and return (filename, base64_data)."""
    try:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'audio_uploads')
        os.makedirs(backup_dir, exist_ok=True)
        filename = f'{job_id}{ext}'
        dest = os.path.join(backup_dir, filename)
        shutil.copy2(tmp_path, dest)
        with open(dest, 'rb') as f:
            audio_b64 = base64.b64encode(f.read()).decode('ascii')
        logger.info('Audio backup saved: %s (%.0fKB)', filename, os.path.getsize(dest) / 1024)
        return filename, audio_b64
    except Exception as e:
        logger.error('Audio backup error: %s', e)
        return None, None


def _process_upload(app, job_id, tmp_path, ext, user_id, user_name, department, role, visibility, api_key):
    """Phase 1: Save audio to disk + DB immediately, return 'done' to client.
    Phase 2: Transcription + analysis runs in background thread.
    NOTE: app is passed explicitly because this runs in a background thread."""
    try:
        # PHASE 1: FAST - save audio, insert DB row, return done
        audio_filename, audio_data = _save_audio_backup(tmp_path, ext, job_id)

        file_size = os.path.getsize(tmp_path)
        logger.info('Upload job %s: file size %.1fMB (%d bytes) - saving immediately', job_id, file_size / 1024 / 1024, file_size)

        if file_size < 5000:
            logger.warning('Upload job %s: audio file too small (%d bytes), likely empty recording', job_id, file_size)

        # Save idea to DB with placeholder transcript
        idea_id = None
        with app.app_context():
            db = get_db()
            cursor = db.execute('''
                INSERT INTO ideas (author_id, author_name, department, role, audio_filename, audio_data,
                    duration_seconds, transcript, status, visibility, transcribed_at, stt_engine)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'new', ?, ?, 'pending')
            ''', (user_id, user_name, department, role, audio_filename or '', audio_data or '',
                  '[Prebieha prepis nahravky...]', visibility, datetime.now().isoformat()))
            idea_id = cursor.lastrowid
            db.commit()
            db.close()

        # Mark job as DONE immediately
        result = {
            'id': idea_id,
            'transcript': '',
            'duration_seconds': 0,
            'stt_engine': 'pending',
            'message': 'Nahravka ulozena! Prepis prebieha na pozadi.'
        }
        upload_jobs.set(job_id, {'status': 'done', 'result': result})
        logger.info('Upload job %s: SAVED to DB (idea #%s), client notified. Starting background transcription...', job_id, idea_id)

        # PHASE 2: BACKGROUND - transcription + analysis
        threading.Thread(
            target=_process_transcription_background,
            args=(app, job_id, idea_id, tmp_path, ext, api_key),
            daemon=True
        ).start()

    except Exception as e:
        logger.error('Upload job %s error: %s', job_id, e)
        upload_jobs.set(job_id, {'status': 'error', 'error': str(e)})
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception as cleanup_err:
                logger.warning('Failed to clean up tmp file: %s', cleanup_err)


def _process_transcription_background(app, job_id, idea_id, tmp_path, ext, api_key):
    """Background: transcribe audio and update the existing DB row."""
    import openai

    try:
        file_size = os.path.getsize(tmp_path)
        logger.info('Background transcription job %s (idea #%s): %.1fMB (%d bytes)', job_id, idea_id, file_size / 1024 / 1024, file_size)

        transcript_text = ''
        total_duration = 0
        stt_engine = 'none'
        stt_warning = None

        # Skip transcription for very small files
        if file_size < 5000:
            logger.info('Background job %s: Audio too small (%d bytes) - skipping transcription', job_id, file_size)
            transcript_text = '[Nahravka je prilis kratka alebo prazdna. Skuste nahrat znova dlhsiu nahravku.]'
            stt_engine = 'skipped'
            with app.app_context():
                db = get_db()
                db.execute('''
                    UPDATE ideas SET transcript = ?, duration_seconds = 0, stt_engine = ?, transcribed_at = ?
                    WHERE id = ?
                ''', (transcript_text, stt_engine, datetime.now().isoformat(), idea_id))
                db.commit()
                db.close()
            logger.info('Background job %s: Marked as too short for idea #%s', job_id, idea_id)
            return

        # PRIMARY: Try ElevenLabs Scribe v2
        el_text, el_duration, el_warning = _transcribe_with_elevenlabs(tmp_path)
        if el_warning:
            stt_warning = el_warning
        if el_text:
            transcript_text = el_text
            total_duration = el_duration
            stt_engine = 'elevenlabs'
            logger.info('Background job %s: ElevenLabs succeeded (%d chars)', job_id, len(transcript_text))
        elif el_text is not None:
            logger.info('Background job %s: ElevenLabs found no speech - skipping Whisper', job_id)
            transcript_text = ''
            total_duration = el_duration
            stt_engine = 'elevenlabs'
        else:
            # FALLBACK: OpenAI Whisper
            logger.info('Background job %s: ElevenLabs failed, falling back to Whisper', job_id)
            client = openai.OpenAI(api_key=api_key)
            chunks = _split_audio_chunks(tmp_path)
            logger.info('Background job %s: %d chunk(s)', job_id, len(chunks))
            all_text = []

            for i, chunk_path in enumerate(chunks):
                chunk_size = os.path.getsize(chunk_path)
                if chunk_size > 25 * 1024 * 1024:
                    logger.warning('Background job %s: chunk %d too large, skipping', job_id, i)
                    continue
                try:
                    with open(chunk_path, 'rb') as f:
                        transcription = client.audio.transcriptions.create(
                            model='whisper-1', file=f, language='sk',
                            response_format='verbose_json',
                            prompt='Toto je nahravka napadu alebo myslienky v slovencine.' if i == 0 else all_text[-1][-200:] if all_text else ''
                        )
                    chunk_text = transcription.text or ''
                    chunk_dur = int(getattr(transcription, 'duration', 0) or 0)
                    total_duration += chunk_dur
                    cleaned = _clean_hallucinations(chunk_text)
                    if cleaned:
                        all_text.append(cleaned)
                    logger.info('Background job %s: chunk %d -> %d chars', job_id, i, len(cleaned))
                except Exception as we:
                    logger.error('Background job %s: Whisper error chunk %d: %s', job_id, i, we)
                    continue

            for chunk_path in chunks:
                if chunk_path != tmp_path and os.path.exists(chunk_path):
                    try:
                        os.unlink(chunk_path)
                    except Exception as e:
                        logger.warning('Failed to clean up chunk: %s', e)

            transcript_text = ' '.join(all_text).strip()
            stt_engine = 'whisper'
            if transcript_text and _is_whisper_hallucination(transcript_text):
                logger.info('Background job %s: Whisper hallucination - discarding', job_id)
                transcript_text = ''

        # Final hallucination check
        if transcript_text:
            transcript_text = _clean_hallucinations(transcript_text)
            if transcript_text and _is_whisper_hallucination(transcript_text):
                transcript_text = ''

        if not transcript_text:
            audio_filename = f'{job_id}{ext}'
            transcript_text = '[Nahravka ulozena - transkript nedostupny. Audio: ' + audio_filename + ']'

        # Update existing DB row with transcript
        with app.app_context():
            db = get_db()
            db.execute('''
                UPDATE ideas SET transcript = ?, duration_seconds = ?, stt_engine = ?, transcribed_at = ?
                WHERE id = ?
            ''', (transcript_text, total_duration, stt_engine, datetime.now().isoformat(), idea_id))
            db.commit()
            db.close()
            save_ideas_backup()

        logger.info('Background job %s: Transcription complete for idea #%s (%s, %d chars)', job_id, idea_id, stt_engine, len(transcript_text))

        # Auto-analyze with Claude
        threading.Thread(target=_auto_analyze, args=(app, idea_id,), daemon=True).start()

    except Exception as e:
        logger.error('Background transcription error job %s: %s', job_id, e)
        try:
            with app.app_context():
                db = get_db()
                db.execute("UPDATE ideas SET transcript = ?, stt_engine = 'error' WHERE id = ?",
                           (f'[Chyba prepisu: {str(e)[:200]}]', idea_id))
                db.commit()
                db.close()
        except Exception as db_err:
            logger.error('Failed to update DB after transcription error: %s', db_err)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception as e:
                logger.warning('Failed to clean up tmp file: %s', e)
