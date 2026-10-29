"""Tenant-fenced intake gates on the caller's database connection.

Each session represents one round; all saved questions are required. Callers
create sessions with round 1..3 and pass the same tenant to every operation.
"""
import json

ANSWER_SOURCES = frozenset({
    'ceo_choice', 'ceo_input', 'recommended_accepted', 'estimate_confirmed',
})
SPECIAL_OPTIONS = ('기타(직접 입력)', '잘 모름—권장값')


async def _lock_session(conn, tenant_id, session_id):
    if not tenant_id:
        raise ValueError('tenant_id is required')
    row = await conn.fetchrow('''SELECT * FROM design_intake_sessions
        WHERE id=$1 AND tenant_id=$2 FOR UPDATE''', session_id, tenant_id)
    if row is None:
        raise ValueError('intake session not found')
    if not 1 <= row['round'] <= 3:
        raise ValueError('round must be between 1 and 3')
    return row


async def save_question(conn, tenant_id, session_id, area, question, options, recommended=None):
    """Save at most six questions per round, serializing on the session row."""
    if not isinstance(options, list) or any(not isinstance(x, str) for x in options):
        raise ValueError('options must be a list of strings')
    examples = list(dict.fromkeys(x.strip() for x in options
                                 if x.strip() and x.strip() not in SPECIAL_OPTIONS))
    if len(examples) < 3:
        raise ValueError('options require at least 3 ordinary examples')
    async with conn.transaction():
        await _lock_session(conn, tenant_id, session_id)
        count = await conn.fetchval('''SELECT count(*) FROM design_intake_questions
            WHERE session_id=$1 AND tenant_id=$2''', session_id, tenant_id)
        if count >= 6:
            raise ValueError('round cannot contain more than 6 questions')
        result = await conn.fetchval('''INSERT INTO design_intake_questions
            (session_id, tenant_id, area, question, options, recommended)
            SELECT id, tenant_id, $3, $4, $5::jsonb, $6 FROM design_intake_sessions
            WHERE id=$1 AND tenant_id=$2 RETURNING id''',
            session_id, tenant_id, area, question, json.dumps(examples + list(SPECIAL_OPTIONS)), recommended)
        await conn.execute('''UPDATE design_intake_sessions SET brief_confirmed_at=NULL
            WHERE id=$1 AND tenant_id=$2''', session_id, tenant_id)
        return result


async def record_answer(conn, tenant_id, session_id, question_id, answer, answer_source,
                        confirmed_at=None):
    """Confirmation must be supplied by the trusted CEO-answer handler.

    In particular, estimates without explicit confirmation remain unconfirmed.
    """
    if answer_source not in ANSWER_SOURCES:
        raise ValueError('invalid answer_source')
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError('answer is required')
    async with conn.transaction():
        await _lock_session(conn, tenant_id, session_id)
        row = await conn.fetchrow('''UPDATE design_intake_questions
            SET answer=$4, answer_source=$5, confirmed_at=$6
            WHERE id=$1 AND session_id=$2 AND tenant_id=$3 RETURNING id''',
            question_id, session_id, tenant_id, answer, answer_source, confirmed_at)
        if row is None:
            raise ValueError('intake question not found')
        await conn.execute('''UPDATE design_intake_sessions SET brief_confirmed_at=NULL
            WHERE id=$1 AND tenant_id=$2''', session_id, tenant_id)


async def confirm_brief(conn, tenant_id, session_id, brief):
    async with conn.transaction():
        await _lock_session(conn, tenant_id, session_id)
        counts = await conn.fetchrow('''SELECT count(*) AS total,
            count(*) FILTER (WHERE answer IS NOT NULL AND btrim(answer) <> ''
                AND answer_source IN ('ceo_choice','ceo_input','recommended_accepted','estimate_confirmed')
                AND confirmed_at IS NOT NULL) AS confirmed
            FROM design_intake_questions WHERE session_id=$1 AND tenant_id=$2''',
            session_id, tenant_id)
        if not counts['total'] or counts['total'] != counts['confirmed']:
            raise ValueError('all required questions need confirmed answers')
        return await conn.fetchval('''UPDATE design_intake_sessions
            SET brief=$3::jsonb, brief_confirmed_at=now()
            WHERE id=$1 AND tenant_id=$2 RETURNING brief_confirmed_at''',
            session_id, tenant_id, json.dumps(brief))


async def assert_brief_confirmed(conn, tenant_id, session_id):
    if not tenant_id:
        raise ValueError('tenant_id is required')
    confirmed = await conn.fetchval('''SELECT brief_confirmed_at FROM design_intake_sessions
        WHERE id=$1 AND tenant_id=$2''', session_id, tenant_id)
    if confirmed is None:
        raise ValueError('brief must be confirmed before mockup generation')
