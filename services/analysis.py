"""
AI analysis service: Claude-based idea evaluation.
"""

import os
import json

from config import logger
from database import get_db
from services.backup import save_ideas_backup


def _get_company_context_for_prompt():
    """Build company context string for AI analysis prompt."""
    db = get_db()
    rows = db.execute('SELECT key, value FROM company_context').fetchall()
    db.close()
    context_parts = []
    labels = {
        'company_description': 'O firme',
        'goals_priorities': 'Ciele a priority firmy',
        'brand_values': 'Hodnoty znacky',
        'idea_criteria': 'Co hladame v napadoch',
    }
    for row in rows:
        if row['value'] and row['value'].strip():
            label = labels.get(row['key'], row['key'])
            context_parts.append(f"{label}: {row['value'].strip()}")
    return '\n'.join(context_parts)


def _auto_analyze(app, idea_id):
    """Auto-trigger Claude analysis after transcription."""
    try:
        import anthropic as anthropic_sdk
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            logger.info('Auto-analyze: no ANTHROPIC_API_KEY')
            return

        with app.app_context():
            db = get_db()
            idea = db.execute('SELECT * FROM ideas WHERE id = ?', (idea_id,)).fetchone()
            if not idea or not idea['transcript']:
                db.close()
                return

            company_context = _get_company_context_for_prompt()

            prompt = f"""Analyzuj nasledujuci interny napad od zamestnanca a ohodno ho.

{('--- KONTEXT FIRMY ---' + chr(10) + company_context + chr(10) + '--- KONIEC KONTEXTU ---' + chr(10)) if company_context else ''}
Oddelenie: {idea['department']}
Rola: {idea['role']}
Transkript napadu:
"{idea['transcript']}"

Vrat JSON s tymto formatom (iba JSON, bez markdown):
{{
  "score": <1-10>,
  "clarity": <1-10>,
  "feasibility": <1-10>,
  "relevance": <1-10>,
  "summary": "<2-3 vety zhrnutie napadu>",
  "strengths": ["<silna stranka 1>", "<silna stranka 2>"],
  "weaknesses": ["<slaba stranka 1>"],
  "next_steps": ["<konkretny krok 1>", "<konkretny krok 2>"],
  "category": "<one of: process_improvement|cost_reduction|revenue|product|other>",
  "tags": ["<tag1>", "<tag2>"]
}}

Hodnot objektivne. score je celkove hodnotenie potencialu napadu.
relevance je hodnotenie relevancie napadu pre firmu (ak je k dispozicii kontext firmy, zohladni ciele, priority a hodnoty firmy).
Pre tags pouzi max 5 tagov z tohto zoznamu (alebo vlastne slovenske/anglicke slovo): quick_win, cost_reduction, product, process, customer, technical, innovation, urgent, automation, hr, marketing, quality."""

            client = anthropic_sdk.Anthropic(api_key=api_key)
            message = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=1000,
                messages=[{'role': 'user', 'content': prompt}]
            )
            raw = message.content[0].text.strip()
            if raw.startswith('```'):
                raw = raw.split('```')[1]
                if raw.startswith('json'):
                    raw = raw[4:]
            analysis = json.loads(raw)
            score = int(analysis.get('score', 0))
            tags = json.dumps(analysis.get('tags', []), ensure_ascii=False)

            db.execute('UPDATE ideas SET ai_score = ?, ai_analysis = ?, tags = ? WHERE id = ?',
                       (score, json.dumps(analysis, ensure_ascii=False), tags, idea_id))
            db.commit()
            db.close()
            save_ideas_backup()
            logger.info('Auto-analyze: idea %s scored %d/10', idea_id, score)
    except Exception as e:
        logger.error('Auto-analyze error for idea %s: %s', idea_id, e)
