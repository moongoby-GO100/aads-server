"""D2 Genspark import: preserve the raw original untouched and convert it to a draft."""
import hashlib
import json

from .generate import GROUPS, draft_version

MAX_RAW_BYTES = 256 * 1024
MAX_ENTRIES = 500
MAX_TEXT = 200
# Accepted source keys per internal group; radius is not imported (colour, spacing, font only).
ALIASES = {'color': ('colors', 'color', 'palette'),
           'spacing': ('spacing', 'space', 'spacings'),
           'typography': ('typography', 'fonts', 'font', 'fontFamily')}


def _raw_bytes(raw):
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return text.encode('utf-8')


def _flatten(node, prefix, out):
    """Flatten nested name -> value maps; {'value': x} leaves are unwrapped. Data only."""
    if len(out) >= MAX_ENTRIES:
        return
    if isinstance(node, dict):
        if set(node) <= {'value', 'type', '$value', '$type'} and ('value' in node or '$value' in node):
            _flatten(node.get('value', node.get('$value')), prefix, out)
            return
        for key in sorted(node, key=str):
            _flatten(node[key], prefix + [str(key)[:MAX_TEXT]], out)
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        name = '-'.join(prefix)
        if name:
            out.setdefault(name, str(node)[:MAX_TEXT])


def convert_genspark(parsed):
    tokens = {group: {} for group in GROUPS}
    for group, names in ALIASES.items():
        for name in names:
            if name in parsed:
                _flatten(parsed[name], [], tokens[group])
    if not any(tokens.values()):
        raise ValueError('Genspark import has no color, spacing or typography tokens')
    return tokens


async def handle_import(trigger, writer, budget):
    payload = trigger['payload']
    if isinstance(payload, str):
        payload = json.loads(payload)
    payload = payload or {}
    if payload.get('kind') != 'genspark_import':
        raise ValueError('payload.kind must be genspark_import')
    raw = payload.get('raw')
    if not isinstance(raw, (str, dict)) or not raw:
        raise ValueError('payload.raw is required')
    data = _raw_bytes(raw)
    if len(data) > MAX_RAW_BYTES:
        raise ValueError('Genspark import exceeds 256KB')
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise ValueError(f'Genspark import is not valid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise ValueError('Genspark import must be a JSON object')
    tokens = convert_genspark(parsed)
    tokens['raw_original'] = raw
    tokens['raw_sha256'] = hashlib.sha256(data).hexdigest()
    return await writer.write('design_token_sets', {
        'tenant_id': writer.tenant_id, 'project_key': writer.project_key,
        'version': draft_version('import', trigger), 'mode': 'light', 'tokens': tokens,
        'created_by': 'design_agent', 'status': 'draft', 'source': 'imported_genspark'})
