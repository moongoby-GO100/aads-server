#!/usr/bin/env python3
"""Read-only reconciliation. Historical missing request snapshots stay unproven."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.llm_model_reconciliation import summarize
from scripts.error_book import psql

CHAT_SQL = """
SELECT COALESCE(json_agg(r), '[]'::json)::text FROM (
 SELECT p.execution_id, p.provenance->'model_reconciliation' AS model_reconciliation,
        p.provenance->>'execution_model_id' AS compiled_model, te.actual_model
 FROM compiled_prompt_provenance p
 LEFT JOIN chat_turn_executions te ON te.id = p.execution_id
 WHERE p.created_at >= NOW() - INTERVAL '24 hours'
) r
"""


def parse_psql_rows(raw):
    """Remove exact transaction command tags, preserving the complete JSON text."""
    payload = '\n'.join(line for line in raw.splitlines()
                        if line.strip() not in {'BEGIN', 'SET', 'ROLLBACK'})
    rows = json.loads(payload)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Expected a JSON array of row objects')
    return rows


def main():
    try:
        raw = psql('BEGIN READ ONLY; SET LOCAL statement_timeout = 15000; ' + CHAT_SQL + '; ROLLBACK;')
        # psql also emits transaction command tags; select the JSON payload only.
        rows = parse_psql_rows(raw)
        result = summarize(rows)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result["recorded_disagreements"] or result["verdicts"].get("unexplained", 0):
            return 1
        return 2 if result["missing_record"] or not rows else 0
    except Exception as exc:
        print(json.dumps(dict(status='unverified', error=type(exc).__name__)), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
