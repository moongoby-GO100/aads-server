"""D2 draft generation: token extraction and three visual directions. No LLM, no DB reads."""
import json
import os
import re
from pathlib import Path

from app.services.design_audit_service import DEFAULT_EXCLUDED_DIRS, resolve_allowed_project_path

GROUPS = ('color', 'spacing', 'radius', 'typography')
CSS_VAR_GROUP = {'color': 'color', 'space': 'spacing', 'radius': 'radius', 'font': 'typography'}
CSS_SUFFIXES = {'.css', '.scss'}
TAILWIND_RE = re.compile(r'tailwind\.config\.(?:js|cjs|mjs|ts)')
TAILWIND_SECTIONS = {'colors': 'color', 'spacing': 'spacing', 'borderRadius': 'radius',
                     'fontFamily': 'typography'}
MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
MAX_LINES = 20000
MIN_TOUCH_TARGET_PX = 44

CSS_VAR_RE = re.compile(r'--(color|space|radius|font)-([A-Za-z0-9_-]+)\s*:\s*([^;{}]*)')
# One alternative per token; no nested repetition so backtracking stays linear (R-BG 3).
JS_TOKEN_RE = re.compile(
    r'''(?P<open>(?P<okey>'[^']*'|"[^"]*"|[\w$.-]+)\s*:\s*\{)'''
    r'''|(?P<anon>\{)'''
    r'''|(?P<close>\})'''
    r'''|(?P<leaf>(?P<lkey>'[^']*'|"[^"]*"|[\w$.-]+)\s*:\s*'''
    r'''(?P<lval>'[^']*'|"[^"]*"|`[^`]*`|\[[^\]]*\]|-?\d[\w.%]*))'''
    r'''|(?P<arrstart>(?P<akey>'[^']*'|"[^"]*"|[\w$.-]+)\s*:\s*\[[^\]]*$)''')
QUOTED_RE = re.compile(r'''(?:'([^']*)'|"([^"]*)"|`([^`]*)`)''')

STATES = (
    ('기본', '내용이 정상적으로 표시됩니다.', '계속'),
    ('로딩', '불러오는 중입니다. 잠시만 기다려 주세요.', '취소'),
    ('빈 상태', '표시할 내용이 아직 없습니다.', '새로 만들기'),
    ('오류', '문제가 발생했습니다. 입력한 내용은 보존되어 있습니다.', '다시 시도'),
    ('권한 없음', '이 화면을 볼 권한이 없습니다.', '권한 요청'),
    ('오프라인', '네트워크에 연결되어 있지 않습니다.', '연결 확인 후 재시도'),
)
SIZE_RE = re.compile(r'(-?\d*\.?\d+)(px|rem)')


def draft_version(prefix, trigger):
    """design_token_sets.version is TEXT and unique per (tenant, project, version, mode)."""
    try:
        trigger_id = trigger['id']
    except (KeyError, TypeError):
        trigger_id = None
    if not trigger_id:
        raise ValueError('Trigger id is required for a unique draft version')
    return f'{prefix}-{trigger_id}'


def _is_inside(path, root):
    return path == root or root in path.parents


def _candidate_files(root):
    """Regular files under root only; symlinks (files or dirs) are never followed."""
    css, configs = [], []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in DEFAULT_EXCLUDED_DIRS
                         and not os.path.islink(os.path.join(current, d)))
        for name in sorted(files):
            full = Path(current, name)
            suffix = full.suffix
            if suffix in CSS_SUFFIXES:
                bucket = css
            elif TAILWIND_RE.fullmatch(name):
                bucket = configs
            else:
                continue
            if full.is_symlink():
                continue
            bucket.append(full)
            if len(css) + len(configs) >= MAX_FILES:
                return css, configs
    return css, configs


def _read_lines(path, root):
    try:
        real = path.resolve()
        if not _is_inside(real, root) or not real.is_file() or real.stat().st_size > MAX_FILE_BYTES:
            return []
        with real.open(encoding='utf-8', errors='replace') as handle:
            return [line for _, line in zip(range(MAX_LINES), handle)]
    except OSError:
        return []


def _extract_css(lines, tokens):
    for line in lines:
        for kind, name, value in CSS_VAR_RE.findall(line):
            value = value.strip()
            if value:
                tokens[CSS_VAR_GROUP[kind]].setdefault(name, value)


def _strip_key(key):
    return key[1:-1] if key[:1] in '\'"' and key[-1:] == key[:1] and len(key) >= 2 else key


def _first_string(text):
    match = QUOTED_RE.search(text)
    if match:
        return next(g for g in match.groups() if g is not None)
    return '' if text.lstrip().startswith('[') else text.strip()


def _tailwind_section(stack):
    names = [k for k in stack if k is not None]
    if 'theme' not in names:
        return None, []
    rest = names[names.index('theme') + 1:]
    if rest[:1] == ['extend']:
        rest = rest[1:]
    if not rest or rest[0] not in TAILWIND_SECTIONS:
        return None, []
    return TAILWIND_SECTIONS[rest[0]], rest[1:]


def _extract_tailwind(lines, tokens):
    stack = []  # object keys currently open; None for unkeyed braces
    pending = None  # (group, name, items) for a multi-line array
    for line in lines:
        if line.lstrip().startswith(('//', '*', '/*')):
            continue
        text = line
        pos = 0
        if pending:
            end = text.find(']')
            pending[2].extend(next(g for g in m.groups() if g is not None)
                              for m in QUOTED_RE.finditer(text if end < 0 else text[:end]))
            if end < 0:
                continue
            group, name, items = pending
            pending = None
            if items:
                tokens[group].setdefault(name, items[0])
            pos = end + 1
        for match in JS_TOKEN_RE.finditer(text, pos):
            if match['open']:
                stack.append(_strip_key(match['okey']))
            elif match['anon']:
                stack.append(None)
            elif match['close']:
                if stack:
                    stack.pop()
            elif match['leaf'] or match['arrstart']:
                key = _strip_key(match['lkey'] or match['akey'])
                group, trail = _tailwind_section(stack)
                if not group:
                    continue
                name = '-'.join(trail + ([] if key == 'DEFAULT' else [key])) or 'DEFAULT'
                if match['arrstart']:
                    pending = (group, name, [])
                    pending[2].extend(next(g for g in m.groups() if g is not None)
                                      for m in QUOTED_RE.finditer(match['arrstart']))
                    continue
                value = _first_string(match['lval'])
                if value:
                    tokens[group].setdefault(name, value)


def extract_tokens(project_root, project_key):
    """Read CSS custom properties and Tailwind theme values; reject paths outside the allowlist."""
    if not isinstance(project_root, str) or not project_root.strip():
        raise ValueError('project_root is required')
    root = resolve_allowed_project_path(project_key, project_root.strip())
    if not root.is_dir():
        raise ValueError('project_root is not a directory')
    tokens = {group: {} for group in GROUPS}
    css_files, config_files = _candidate_files(root)
    for path in css_files:
        _extract_css(_read_lines(path, root), tokens)
    for path in config_files:
        _extract_tailwind(_read_lines(path, root), tokens)
    if not any(tokens.values()):
        raise ValueError('No design tokens found under project_root')
    return tokens


def _scale(value, factor):
    match = SIZE_RE.fullmatch(value.strip())
    if not match:
        return None
    number = round(float(match.group(1)) * factor, 4)
    return f'{number:g}{match.group(2)}'


def _states():
    return [{'state': name, 'message': message,
             'recovery_button': {'label': label,
                                 'role': 'primary' if name == '기본' else 'recovery',
                                 'min_touch_target_px': MIN_TOUCH_TARGET_PX},
             'min_touch_target_px': MIN_TOUCH_TARGET_PX}
            for name, message, label in STATES]


def build_direction_drafts(tokens):
    """Three deterministic directions, each with six state mockup specs."""
    scale = 1.25
    scaled = {k: s for k, v in tokens.get('spacing', {}).items()
              if (s := _scale(v, scale)) is not None}
    specs = (
        ('keep', '기존 유지', '추출된 토큰을 그대로 사용합니다.', {}),
        ('contrast', '대비 강화', '글자·테두리 대비와 포커스 표시를 높입니다.',
         {'min_text_contrast_ratio': 7.0, 'border_width_px': 2, 'focus_ring_px': 3}),
        ('spacing', '여백 확대', f'간격 토큰을 {scale}배로 넓힙니다.',
         {'spacing_scale': scale, 'scaled_spacing': scaled}),
    )
    return [{'id': key, 'name': name, 'description': desc, 'adjustments': adj,
             'states': _states()} for key, name, desc, adj in specs]


async def handle_generate(trigger, writer, budget):
    payload = trigger['payload']
    if isinstance(payload, str):
        payload = json.loads(payload)
    tokens = extract_tokens((payload or {}).get('project_root'), writer.project_key)
    tokens['directions'] = build_direction_drafts(tokens)
    return await writer.write('design_token_sets', {
        'tenant_id': writer.tenant_id, 'project_key': writer.project_key,
        'version': draft_version('generate', trigger), 'mode': 'light', 'tokens': tokens,
        'created_by': 'design_agent', 'status': 'draft', 'source': 'extracted'})
