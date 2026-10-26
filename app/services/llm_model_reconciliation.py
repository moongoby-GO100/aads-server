"""Observation only: unknown causes remain unexplained; retries prove no reselection.

Compiled models describe plans, not proof that a provider answered.
"""
from collections import Counter

SYSTEM_INTENTS = frozenset({'auto_reaction', 'system_trigger'})
NON_MODELS = frozenset({'stopped', 'interrupted', 'unknown', 'error', 'cancelled', 'pending'})
AUTO_MODELS = frozenset({'', 'auto', 'mixture'})


def model_id(value):
    value = str(value or '').strip()
    return None if value.lower() in NON_MODELS | AUTO_MODELS else value


def selection_reason_hint(intent_override=None):
    return 'system_trigger' if intent_override in SYSTEM_INTENTS else None


def check_execution(*, request_override=None, session_selected=None, compiled_model=None,
                    actual_model=None, intent_override=None, retry_count=0):
    # retry_count is intentionally not evidence of model reselection.
    selected = model_id(request_override) or model_id(session_selected)
    compiled = model_id(compiled_model)
    actual = model_id(actual_model)
    mismatch = bool(selected and ((compiled and selected != compiled) or
                                  (actual and selected != actual)))
    reason = selection_reason_hint(intent_override) if mismatch else None
    verdict = ('explained_mismatch' if reason else 'unexplained') if mismatch else (
        'exact_match' if selected and actual else 'unproven')
    return dict(selected_model=selected, compiled_model=compiled, actual_model=actual,
                selected_source=('request_override' if model_id(request_override) else
                                 'session_selection' if selected else 'unavailable'),
                reason=reason, verdict=verdict, ok=verdict != 'unexplained')


def observe_compilation(*, request_override, session_selected, compiled_model, intent_override):
    inputs = dict(request_override=request_override, session_selected=session_selected,
                  compiled_model=compiled_model, intent_override=intent_override)
    return dict(version=1, inputs=inputs, assessment=check_execution(**inputs))


def compare_record(record):
    """Recompute the same recorded inputs, separately from later execution evidence."""
    if not isinstance(record, dict) or record.get('version') != 1:
        return None
    return record.get('assessment') != check_execution(**record['inputs'])


def summarize(rows):
    verdicts, reasons, recorded_verdicts = Counter(), Counter(), Counter()
    result = dict(rows=0, recorded_disagreements=0, missing_record=0)
    for row in rows:
        record = row.get('model_reconciliation')
        disagreement = compare_record(record)
        result['rows'] += 1
        result['missing_record'] += disagreement is None
        result['recorded_disagreements'] += disagreement is True
        if disagreement is not None:
            recorded_verdicts[(record.get('assessment') or {}).get('verdict', 'missing')] += 1
        inputs = dict(record['inputs']) if disagreement is not None else dict(
            compiled_model=row.get('compiled_model'))
        # Never substitute um.model_used or current session state for historical inputs.
        inputs['actual_model'] = row.get('actual_model')
        assessment = check_execution(**inputs)
        verdicts[assessment['verdict']] += 1
        reasons[assessment['reason'] or 'none'] += 1
    return dict(**result, verdicts=dict(verdicts), reasons=dict(reasons),
                recorded_verdicts=dict(recorded_verdicts))
