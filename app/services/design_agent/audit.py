"""D3 post-deploy audit: contrast, touch target, overflow. Code judgement only; no LLM, no DB reads."""
import json
import os
import re
from pathlib import Path

from app.services.design_audit_service import iter_source_files, resolve_allowed_project_path

CONTRAST_MIN = 4.5
TOUCH_MIN_PX = 44
MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
MAX_REQUESTS = 20
MAX_FINDINGS = 500
MAX_ATTR_CHARS = 800
SELECTOR_LOOKBACK = 300

# design_modification_requests.request_type has a CHECK constraint (migration 084) that
# does not contain 'audit_fix'; each rule maps to an allowed value instead.
REQUEST_TYPE = {'contrast': 'color', 'touch_target': 'interaction', 'overflow': 'responsive'}
THRESHOLD = {'contrast': f'{CONTRAST_MIN}:1', 'touch_target': f'{TOUCH_MIN_PX}px',
             'overflow': 'no fixed width without overflow handling'}
CRITERIA = {
    'contrast': f'본문 글자와 배경의 명도 대비가 {CONTRAST_MIN}:1 이상이어야 한다.',
    'touch_target': f'버튼·링크의 터치 영역 높이가 {TOUCH_MIN_PX}px 이상이어야 한다.',
    'overflow': '고정 너비 텍스트 컨테이너는 줄바꿈 또는 overflow 처리로 내용이 넘치지 않아야 한다.',
}
FORBIDDEN_SCOPE = ['다른 파일', '토큰 승인본']

_CLOSERS = {'(': ')', '[': ']', '{': '}'}
BUTTON_TAGS = {'button', 'a', 'Button', 'Link'}
BUTTON_SELECTOR_RE = re.compile(
    r'(?i)(?<![\w.#-])(?:button|a)(?![\w-])|\.[\w-]*(?:btn|button|link)[\w-]*'
    r'|\[role=["\']?button')
COLOR_TOKEN_RE = re.compile(r'#[0-9a-fA-F]{3,8}(?![\w-])|rgba?\([^)]*\)', re.IGNORECASE)
CLASS_ATTR_RE = re.compile(r'(?<![\w-])class(?:Name)?\s*=\s*')
INLINE_STYLE_RE = re.compile(r'''(?<![\w-])style\s*=\s*(?:"([^"]*)"|'([^']*)')''')
STYLE_PREFIX_RE = re.compile(r'(?<![\w-])style\s*=\s*\{?\s*$')
LITERAL_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|`([^`]*)`')
LENGTH_RE = re.compile(r'^(-?\d*\.?\d+)(px|rem|em)$')
APPLY_RE = re.compile(r'@apply\s+([^;}]*)')
TW_LEN_RE = re.compile(r'^(?P<p>size|min-h|h|w)-(?:(?P<n>\d*\.?\d+)|(?P<px>px)|\[(?P<b>[^\]]+)\])$')
TW_PAD_RE = re.compile(r'^(?P<p>py|p)-(?:(?P<n>\d*\.?\d+)|(?P<px>px)|\[(?P<b>[^\]]+)\])$')
TW_TEXT_LH = {'xs': 16, 'sm': 20, 'base': 24, 'lg': 28, 'xl': 28, '2xl': 32, '3xl': 36}
TW_CLIP_RE = re.compile(r'^(?:overflow(?:-x)?-(?:auto|hidden|scroll|clip)|truncate)$')
TW_WRAP_RE = re.compile(r'^(?:text-(?:wrap|balance|pretty)|break-(?:words|all)'
                        r'|whitespace-(?:normal|pre-wrap|pre-line|break-spaces))$')
TW_TEXT_RE = re.compile(r'^(?:text-(?:xs|sm|base|lg|xl|[2-9]xl)|font-[a-z]+|leading-\S+)$')
CSS_CLIPPING = {'hidden', 'auto', 'scroll', 'clip'}
CSS_WRAPPING = {'overflow-wrap': {'break-word', 'anywhere'}, 'word-wrap': {'break-word', 'anywhere'},
                'word-break': {'break-all', 'break-word'},
                'text-wrap': {'wrap', 'balance', 'pretty', 'stable'}}
CSS_TEXT_PROPS = {'font-size', 'line-height', 'font-family', 'font-weight', 'text-align',
                  'white-space'}
MAX_VARIANTS = 32


def _channel(value):
    value = value.strip()
    try:
        if value.endswith('%'):
            return max(0.0, min(255.0, float(value[:-1]) * 2.55))
        return max(0.0, min(255.0, float(value)))
    except ValueError:
        return None


def parse_color(value):
    """(r, g, b) in 0-255, or None when the value cannot be judged (var(), alpha, names)."""
    if not isinstance(value, str):
        return None
    value = value.strip().strip('\'"').lower()
    if value.startswith('#'):
        digits = value[1:]
        if not re.fullmatch(r'[0-9a-f]+', digits):
            return None
        if len(digits) == 3:
            digits = ''.join(c * 2 for c in digits)
        if len(digits) != 6:
            return None
        return tuple(float(int(digits[i:i + 2], 16)) for i in (0, 2, 4))
    match = re.fullmatch(r'rgba?\(([^)]*)\)', value)
    if not match:
        return None
    parts = [p for p in re.split(r'[\s,/]+', match.group(1).strip()) if p]
    if len(parts) not in (3, 4):
        return None
    if len(parts) == 4:
        try:
            alpha = float(parts[3][:-1]) / 100 if parts[3].endswith('%') else float(parts[3])
        except ValueError:
            return None
        if alpha != 1:
            return None
    channels = [_channel(p) for p in parts[:3]]
    return None if None in channels else tuple(channels)


def _luminance(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    """WCAG contrast ratio, or None if either colour cannot be parsed (never 0)."""
    fg_rgb, bg_rgb = parse_color(fg), parse_color(bg)
    if fg_rgb is None or bg_rgb is None:
        return None
    hi, lo = sorted((_luminance(fg_rgb), _luminance(bg_rgb)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _to_px(value):
    match = LENGTH_RE.match(value.strip().lower())
    if not match:
        return None
    number = float(match.group(1))
    return number if match.group(2) == 'px' else number * 16


def _split_decls(body):
    """Split declarations on ; or , outside parentheses and quotes."""
    parts, depth, quote, start = [], 0, '', 0
    for i, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = ''
        elif ch in '\'"`':
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif ch in ';,\n' and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return parts


def _parse_decls(body):
    """Declarations by CSS cascade: the last one wins unless an earlier one is !important.

    The dict order follows the effective declaration, so _latest() can compare properties.
    """
    decls, important = {}, set()
    for piece in _split_decls(body):
        name, sep, value = piece.partition(':')
        name = name.strip().strip('\'"')
        if not sep or not re.fullmatch(r'[A-Za-z-]+', name):
            continue
        name = re.sub(r'([a-z])([A-Z])', r'\1-\2', name).lower()
        value = value.strip().strip('\'"').strip()
        flagged = bool(re.search(r'!\s*important\s*$', value, re.IGNORECASE))
        if flagged:
            value = re.sub(r'\s*!\s*important\s*$', '', value, flags=re.IGNORECASE).strip()
        if name in decls:
            if name in important and not flagged:
                continue
            del decls[name]
        decls[name] = value
        (important.add if flagged else important.discard)(name)
    return decls


def _latest(decls, names):
    """Value of whichever of names was declared last, or '' when none is declared."""
    for name in reversed(list(decls)):
        if name in names:
            return decls[name]
    return ''


def _line_of(content, index):
    return content.count('\n', 0, index) + 1


def _violation(rule, file, line, value):
    return {'rule': rule, 'file': file, 'line': line, 'value': value,
            'threshold': THRESHOLD[rule]}


def _first_color(value):
    match = COLOR_TOKEN_RE.match((value or '').strip())  # gradients/url() stay unjudged
    return match.group(0) if match else None


def _contrast_of(decls, min_ratio):
    fg = _first_color(decls.get('color', ''))
    bg = _first_color(_latest(decls, ('background-color', 'background')))
    if not fg or not bg:
        return None
    ratio = contrast_ratio(fg, bg)
    if ratio is None or ratio >= min_ratio:
        return None
    return f'{fg} on {bg} = {ratio:.2f}:1'


def _tw_len(token, prefix_ok):
    match = TW_LEN_RE.match(token)
    if not match or match['p'] not in prefix_ok:
        return None
    if match['px']:
        return 1.0
    if match['n'] is not None:
        return float(match['n']) * 4
    return _to_px(match['b'].replace('_', ''))


def _tw_height(tokens):
    """Effective height in px from unprefixed Tailwind classes, or None if not judgeable."""
    heights = [v for t in tokens if ':' not in t
               for v in [_tw_len(t, {'h', 'min-h', 'size'})] if v is not None]
    if heights:
        return max(heights)
    if any(t.startswith('leading-') for t in tokens):
        return None
    pads = {}
    for token in tokens:
        match = TW_PAD_RE.match(token)
        if match and ':' not in token:
            pads[match['p']] = 1.0 if match['px'] else (
                float(match['n']) * 4 if match['n'] is not None
                else _to_px(match['b'].replace('_', '')))
    pad = pads.get('py', pads.get('p'))
    if pad is None:
        return None
    line_height = TW_TEXT_LH['base']
    for token in tokens:
        if token.startswith('text-') and token[5:] in TW_TEXT_LH:
            line_height = TW_TEXT_LH[token[5:]]
    return 2 * pad + line_height


def _tw_fixed_width(tokens):
    for token in tokens:
        if ':' not in token and (width := _tw_len(token, {'w'})) is not None:
            return width
    return None


def _touch_value(px):
    return f'height {px:g}px'


def _css_touch(decls, apply_tokens):
    heights = [h for h in (_to_px(decls.get('min-height', '')),
                           _to_px(decls.get('height', ''))) if h is not None]
    if heights:
        return max(heights)
    if apply_tokens:
        return _tw_height(apply_tokens)
    line_height = _to_px(decls.get('line-height', ''))
    font_size = _to_px(decls.get('font-size', ''))
    if line_height is None and font_size is not None:
        line_height = font_size * 1.2  # CSS `normal` line-height
    pad_v = [_to_px(decls.get(k, '')) for k in ('padding-top', 'padding-bottom')]
    if None in pad_v:
        shorthand = decls.get('padding', '').split()
        pad_v = [_to_px(shorthand[0]), _to_px(shorthand[2] if len(shorthand) > 2
                                              else shorthand[0])] if shorthand else [None]
    if line_height is None or None in pad_v:
        return None
    return sum(pad_v) + line_height


def _css_overflow(decls, apply_tokens):
    width = _to_px(decls.get('width', ''))
    nowrap = decls.get('white-space', '').lower() == 'nowrap'
    horizontal = _latest(decls, ('overflow', 'overflow-x')).lower().split()
    clipped = bool(horizontal) and horizontal[0] in CSS_CLIPPING
    wraps = any(decls.get(prop, '').lower() in ok for prop, ok in CSS_WRAPPING.items())
    text = bool(CSS_TEXT_PROPS & set(decls))
    if width is None and apply_tokens:
        width = _tw_fixed_width(apply_tokens)
        nowrap = nowrap or 'whitespace-nowrap' in apply_tokens
        clipped = clipped or any(TW_CLIP_RE.match(t) for t in apply_tokens)
        wraps = wraps or any(TW_WRAP_RE.match(t) for t in apply_tokens)
        text = text or any(TW_TEXT_RE.match(t) for t in apply_tokens)
    # nowrap leaves no wrap opportunity, so only clipping/scrolling contains the text;
    # text-overflow:ellipsis and overflow-wrap:normal contain nothing on their own.
    if not width or clipped or (wraps and not nowrap) or not (nowrap or text):
        return None
    reason = 'white-space:nowrap' if nowrap else 'overflow 미지정'
    return f'width {width:g}px + {reason}'


def _css_blocks(content):
    """Yield (index, selector, body) for brace blocks without nested braces."""
    pos = 0
    while True:
        opener = content.find('{', pos)
        if opener < 0:
            return
        closer = content.find('}', opener + 1)
        if closer < 0:
            return
        nested = content.find('{', opener + 1, closer)
        if nested >= 0:
            pos = opener + 1
            continue
        lo = max(0, opener - SELECTOR_LOOKBACK)
        start = max(content.rfind(c, lo, opener) for c in '};{') + 1
        yield opener, content[start:opener].strip(), content[opener + 1:closer]
        pos = closer + 1


def _tag_before(content, index):
    lt = content.rfind('<', max(0, index - 400), index)
    if lt < 0:
        return ''
    match = re.match(r'<([A-Za-z][\w.]*)', content[lt:lt + 60])
    return match.group(1) if match else ''


def _lex_attr(content, start):
    """Tokens of a className value: ('s', string literal) and ('o', operator/bracket)."""
    end = min(len(content), start + MAX_ATTR_CHARS)
    tokens, i, depth = [], start, 0
    while i < end:
        ch = content[i]
        if ch in '"\'`':
            j = i + 1
            while j < end and content[j] != ch:
                j += 2 if content[j] == '\\' else 1
            if j >= end:
                break
            tokens.append(('s', content[i + 1:j]))
            i = j + 1
            if depth == 0:
                break
            continue
        two = content[i:i + 2]
        if two in ('&&', '||', '??'):
            tokens.append(('o', two))
            i += 2
            continue
        if two == '?.' and not content[i + 2:i + 3].isdigit():
            i += 2
            continue
        if ch in '{([':
            depth += 1
        elif ch in '})]':
            depth -= 1
        if ch in '?:,{}()[]':
            tokens.append(('o', ch))
        i += 1
        if depth <= 0:
            break
    return tokens


def _cap(variants):
    seen, out = set(), []
    for variant in variants:
        key = tuple(variant)
        if key not in seen:
            seen.add(key)
            out.append(variant)
    return out[:MAX_VARIANTS]


def _product(left, right):
    return _cap([a + b for a in left for b in right])


def _parse_variants(tokens, i, stop):
    """Alternative literal lists: ternary branches and `&&`/`||` operands are exclusive."""
    variants = [[]]
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == 'o' and value in stop:
            break
        i += 1
        if kind == 's':
            variants = _cap([v + [value] for v in variants])
        elif value in _CLOSERS:
            inner, i = _parse_variants(tokens, i, {_CLOSERS[value]})
            i += 1
            variants = _product(variants, inner)
        elif value == '?':
            then, i = _parse_variants(tokens, i, stop | {':'})
            other = []
            if i < len(tokens) and tokens[i] == ('o', ':'):
                other, i = _parse_variants(tokens, i + 1, stop | {','})
            variants = _product(variants, then + other)
        elif value in ('&&', '||', '??'):
            operand = [[]]
            if i < len(tokens) and tokens[i][0] == 's':
                operand = [[tokens[i][1]]]
                i += 1
            elif i < len(tokens) and tokens[i][1] in _CLOSERS:
                operand, i = _parse_variants(tokens, i + 1, {_CLOSERS[tokens[i][1]]})
                i += 1
            variants = _cap(variants + _product(variants, operand))
    return variants, i


def _attr_variants(content, start):
    """Each distinct class-token combination a className value can render, as literal lists."""
    tokens = _lex_attr(content, start)
    if not tokens:
        return []
    variants, _ = _parse_variants(tokens, 0, set())
    return [v for v in variants if v]


def _rule_checker(rule):
    def check(content, file='<memory>', **kwargs):
        return [v for v in _judge(content, file, **kwargs) if v['rule'] == rule]
    return check


def _judge(content, file, min_ratio=CONTRAST_MIN, min_touch=TOUCH_MIN_PX):
    out = []
    for opener, selector, body in _css_blocks(content):
        decls = _parse_decls(body)
        apply_tokens = [t for m in APPLY_RE.finditer(body) for t in m.group(1).split()]
        line = _line_of(content, opener)
        inline = (not selector and STYLE_PREFIX_RE.search(content[max(0, opener - 20):opener])
                  ) or STYLE_PREFIX_RE.search(selector)
        button_like = (_tag_before(content, opener) in BUTTON_TAGS if inline
                       else bool(BUTTON_SELECTOR_RE.search(selector)))
        if (value := _contrast_of(decls, min_ratio)):
            out.append(_violation('contrast', file, line, value))
        if button_like:
            px = _css_touch(decls, apply_tokens)
            if px is not None and px < min_touch:
                out.append(_violation('touch_target', file, line, _touch_value(px)))
        if (value := _css_overflow(decls, apply_tokens)):
            out.append(_violation('overflow', file, line, value))
    for match in INLINE_STYLE_RE.finditer(content):
        text = match.group(1) if match.group(1) is not None else match.group(2)
        decls = _parse_decls(text)
        line = _line_of(content, match.start())
        if (value := _contrast_of(decls, min_ratio)):
            out.append(_violation('contrast', file, line, value))
        if _tag_before(content, match.start() + 1) in BUTTON_TAGS:
            px = _css_touch(decls, [])
            if px is not None and px < min_touch:
                out.append(_violation('touch_target', file, line, _touch_value(px)))
        if (value := _css_overflow(decls, [])):
            out.append(_violation('overflow', file, line, value))
    for match in CLASS_ATTR_RE.finditer(content):
        line = _line_of(content, match.start())
        button = _tag_before(content, match.start()) in BUTTON_TAGS
        seen = set()
        for literals in _attr_variants(content, match.end()):
            tokens = [t for lit in literals for t in lit.split() if '${' not in t]
            if not tokens:
                continue
            found = []
            fg = next((_first_color(t[6:-1]) for t in tokens if t.startswith('text-[')
                       and t.endswith(']')), None)
            bg = next((_first_color(t[4:-1]) for t in tokens if t.startswith('bg-[')
                       and t.endswith(']')), None)
            ratio = contrast_ratio(fg, bg) if fg and bg else None
            if ratio is not None and ratio < min_ratio:
                found.append(('contrast', f'{fg} on {bg} = {ratio:.2f}:1'))
            if button:
                px = _tw_height(tokens)
                if px is not None and px < min_touch:
                    found.append(('touch_target', _touch_value(px)))
            if (value := _css_overflow({}, tokens)):
                found.append(('overflow', value))
            for rule, value in found:
                if (rule, value) not in seen:
                    seen.add((rule, value))
                    out.append(_violation(rule, file, line, value))
    return out


check_contrast = _rule_checker('contrast')
check_touch_target = _rule_checker('touch_target')
check_overflow = _rule_checker('overflow')


def audit_text(content, file='<memory>'):
    """All rule violations for one source string."""
    return sorted(_judge(content, file), key=lambda v: (v['line'], v['rule']))


def _is_inside(path, root):
    return path == root or root in path.parents


def scan_project(root, max_files=MAX_FILES):
    """Audit up to max_files source files; files resolving outside root are never read."""
    root = Path(root).resolve()
    violations, scanned = [], 0
    for path in iter_source_files(root, max_files=max_files):
        try:
            real = Path(os.path.realpath(path))
            if not _is_inside(real, root) or not real.is_file() \
                    or real.stat().st_size > MAX_FILE_BYTES:
                continue
            content = real.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        scanned += 1
        relative = real.relative_to(root).as_posix() if root.is_dir() else real.name
        violations.extend(audit_text(content, relative))
    return violations, scanned


def score_of(violations):
    return max(0, 100 - 5 * len(violations))


def group_violations(violations):
    groups = {}
    for violation in violations:
        groups.setdefault((violation['rule'], violation['file']), []).append(violation)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0][1], kv[0][0]))


def build_request(rule, file, items):
    samples = '; '.join(f"{v['line']}행 {v['value']}" for v in items[:5])
    prompt = (f'{file} 의 {rule} 위반 {len(items)}건을 고쳐 주세요 (기준 {THRESHOLD[rule]}). '
              f'감사에서 발견: {samples}')
    return {
        'user_prompt': prompt,
        'normalized_card': {'source': 'audit_fix', 'rule': rule, 'file': file,
                            'violation_count': len(items), 'threshold': THRESHOLD[rule],
                            'violations': items[:20]},
        'request_type': REQUEST_TYPE[rule],
        'allowed_scope': {'target_files': [file]},
        'forbidden_scope': {'notes': list(FORBIDDEN_SCOPE)},
        'acceptance_criteria': [CRITERIA[rule], f'{file} 이외의 파일과 승인된 토큰은 변경하지 않는다.'],
    }


async def handle_deploy_audit(trigger, writer, budget):
    payload = trigger['payload']
    if isinstance(payload, str):
        payload = json.loads(payload)
    project_root = (payload or {}).get('project_root')
    if not isinstance(project_root, str) or not project_root.strip():
        raise ValueError('project_root is required')
    root = resolve_allowed_project_path(writer.project_key, project_root.strip())
    if not root.is_dir():
        raise ValueError('project_root is not a directory')
    violations, scanned = scan_project(root)
    groups = group_violations(violations)
    omitted = {id(v) for _, items in groups[MAX_REQUESTS:] for v in items}
    findings = [dict(v, truncated=True) if id(v) in omitted else dict(v)
                for v in violations[:MAX_FINDINGS]]
    if len(violations) > MAX_FINDINGS:
        findings.append({'rule': 'findings_truncated', 'omitted': len(violations) - MAX_FINDINGS,
                         'truncated': True})
    run_id = await writer.write('design_audit_runs', {
        'tenant_id': writer.tenant_id, 'project_key': writer.project_key, 'status': 'draft',
        'score': int(score_of(violations)), 'findings': findings,
        'scanned_files': int(scanned)})
    for (rule, file), items in groups[:MAX_REQUESTS]:
        await writer.write('design_modification_requests', {
            'tenant_id': writer.tenant_id, 'project_key': writer.project_key,
            'status': 'draft', **build_request(rule, file, items)})
    return run_id
