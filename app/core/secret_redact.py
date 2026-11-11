"""도구 출력·요약 공용 비밀값 가림 (단일 정본).

llmops_store.mask_secrets 와 ToolExecutor.execute 가 같은 함수를 쓴다.

두 함수로 나뉜다.
- redact_value(obj): dict/list/tuple 을 구조 단위로 내려가며, 키 이름에 비밀 키워드가
  '포함'되면 값을 통째로 '***' 로 바꾼다. 그 외 str 값은 redact_text 로 처리한다.
  직렬화된 문자열에 정규식을 거는 것보다 경계 사례가 적다. 원본은 바꾸지 않는다.
- redact_text(text): 평문(로그·명령 출력·직렬화 JSON)용 패턴 가림.

'이미 가려짐' 은 값 전체가 '*' 뿐이거나 정확히 [REDACTED] / ***MASKED*** 일 때만 인정한다.
값 안에 '***' 가 섞여 있다고 건너뛰면 뒤쪽 평문이 남는다.

키워드가 든 이름의 값을 남기는 예외는 하나뿐이다: 이름이 수량·통계형(max_tokens,
input_tokens, TOKEN_COUNT …)이고 값이 숫자일 때. SECRET_FILE·API_KEY_PATH 처럼 이름만 보고
문자열 값을 통과시키면 비밀값이 샌다(2026-11-11 3차 리뷰, runner-e82c7a44).
"""
from __future__ import annotations

import re
from typing import Any, Callable

MASK = "***"

_KEYWORDS = r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|DATABASE_URL|DSN)"
_KEY_HAS_KEYWORD = re.compile(r"(?i)" + _KEYWORDS)

# 수량·통계 이름 (max_tokens, input_tokens, TOKEN_COUNT, token_usage …).
# 이 이름이어도 값이 숫자일 때만 남긴다 — _skip_name / _redact_keyword_value 참고.
_METRIC_NAME = re.compile(
    r"(?i)(?:tokens|(?:tokens?|secret|key)_?"
    r"(?:count|limit|ttl|max|min|usage|used|remaining|total|expires?|expiry|length|len|size|budget))$"
)
_NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?[,;)\]}]?$")

# 이미 가려짐: 값 전체가 별표 / [REDACTED] / <redacted> / ***MASKED***
_ALREADY = re.compile(r"(?i)^(?:\*+|\[redacted\]|<redacted>|\*+masked\*+)$")
_MASKED_URL_VALUE = re.compile(r"^[A-Za-z][\w+.\-]*://[^\s@/]*:\*+@")

_SCHEMES = r"(?:postgres(?:ql)?|mysql|mariadb|redis|rediss|amqps?|mongodb(?:\+srv)?|https?)"
# scheme://user:PASSWORD@host — user 는 비어 있어도 됨, 비밀번호에 '@' 포함 허용
_URL_RE = re.compile(
    r"(?i)\b(" + _SCHEMES + r"://)([^\s:/@'\"\\]*):([^\s/'\"\\]+)@"
)
# 잘려서 '@' 가 없는 꼬리: scheme://user:pass<끝>.
# 비밀번호 없는 URL 은 건드리지 않는다: 포트 숫자로 끝나거나 포트 뒤에 ?·# 가 오는 경우
# (https://example.com:8443?x=1), IPv6 대괄호 호스트(https://[::1]:8443) — 3차 리뷰.
# 한계: 숫자만으로 된 비밀번호가 '@' 직전에서 잘리면 포트와 구분할 수 없어 남는다.
_URL_TAIL_RE = re.compile(
    r"(?i)\b(" + _SCHEMES + r"://)([^\s:/@\[\]'\"\\]*):((?!\d*(?:[?#]|\Z))[^\s/@'\"\\]+)\Z"
)

_NAME = r"(?:--?)?[A-Za-z0-9_.\-]*" + _KEYWORDS + r"[A-Za-z0-9_.\-]*"
_NAME_START = r"(?<![A-Za-z0-9_.\-])"
_DQ_BODY = r'(?:[^"\\]|\\.|\\\Z)*'
# 직렬화(JSON 문자열) 안에 다시 들어간 큰따옴표 값 \"…\" 의 본문. 한 단위씩 읽는다.
#   \\\\ + 한 단위 : 원래 값의 이스케이프. 원래 \" 는 직렬화 후 \\\" 가 되므로 닫는 따옴표가 아니다.
#   \\x           : 직렬화가 만든 제어문자 이스케이프 (\n, \t, \uXXXX)
#   그 외 한 글자. 맨 끝·감싼 문자열 끝(맨 따옴표)에서 잘린 이스케이프도 받는다.
# 단위끼리 첫 두 글자가 겹치지 않아 백트래킹이 늘어나지 않는다.
_ESC_UNIT = r'(?:\\\\|\\"|\\[^"\\]|[^"\\]|(?=")|\Z)'
_ESC_DQ_BODY = r'(?:\\\\' + _ESC_UNIT + r'|\\[^"\\]|[^"\\])*'
# NAME=value / NAME: value (.env, docker inspect, YAML, 명령행). 값은 아래 네 형태 중 하나.
#  1) \"..\" (직렬화 JSON 문자열 안의 따옴표)  2) "..."  3) '...'  4) 따옴표 없는 값
_ENV_RE = re.compile(
    r"(?i)" + _NAME_START + "(?P<name>" + _NAME + r")(?P<sep>\s*[=:]\s*)(?P<val>"
    r'\\"' + _ESC_DQ_BODY + r'(?:\\"|(?=")|\\*\Z)'
    r"|\"" + _DQ_BODY + r"(?:\"|\Z)"
    r"|'[^']*(?:'|\Z)"
    r"|(?:[^\s\"'\\]|\\(?![nrt\"']))+"
    r")"
)
# "NAME": value  /  'NAME': 'value' (JSON·파이썬 repr). 잘려서 닫히지 않은 값도 끝까지 덮는다.
_JSON_RE = re.compile(
    r"(?i)(?P<pre>(?P<q>[\"'])(?P<name>" + _NAME + r")(?P=q)\s*:\s*)(?P<val>"
    r"\"" + _DQ_BODY + r"(?:\"|\Z)"
    r"|'(?:[^'\\]|\\.|\\\Z)*(?:'|\Z)"
    r"|[^\s,}\]\"']+"
    r")"
)
# 직렬화 JSON 이 다시 문자열에 들어간 \"NAME\": \"value\"
_JSON_ESC_RE = re.compile(
    r'(?i)(?P<pre>\\"(?P<name>' + _NAME + r')\\"\s*:\s*\\")'
    r'(?P<val>' + _ESC_DQ_BODY + r'(?:\\+\Z)?)(?=\\"|"|\Z)'
)
# mysql -pPASSWORD
_MYSQL_P_RE = re.compile(r"(\bmysql(?:dump|admin)?\b[^\n]*?\s-p)(\S+)")

_PREFIX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    (re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"), "sk-***"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{20,}"), "AIza***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh_***"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "xox-***"),
    (re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"), "AKIA***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "jwt.***"),
)


def _already_masked(value: str) -> bool:
    """값 전체가 이미 가려진 표시일 때만 True — 일부만 가려진 값은 비밀이 남아 있을 수 있다."""
    return not value or bool(_ALREADY.match(value))


def _skip_name(name: str, value: str) -> bool:
    """수량·통계 이름(max_tokens, TOKEN_COUNT …)이고 값이 숫자일 때만 가리지 않는다.

    이름만으로 통과시키지 않는다 — SECRET_FILE=…, API_KEY_PATH=… 도 가린다.
    """
    return bool(_METRIC_NAME.search(name) and _NUMERIC.match(value))


def _env_sub(m: re.Match[str]) -> str:
    name, sep, value = m.group("name"), m.group("sep"), m.group("val")
    if value.startswith('\\"'):
        quote = '\\"'
        inner = value[2:-2] if value.endswith('\\"') and len(value) >= 4 else value[2:]
    elif value[0] in "\"'":
        quote = value[0]
        inner = value[1:-1] if len(value) >= 2 and value.endswith(quote) else value[1:]
    else:
        quote, inner = "", value
    tail = ""
    if not quote and inner.endswith("]") and "[" not in inner:
        # docker inspect 의 `[A=b C=d]` 마지막 값 뒤 닫는 대괄호는 값이 아니다
        inner, tail = inner[:-1], "]"
    if _skip_name(name, inner) or _already_masked(inner) or _MASKED_URL_VALUE.match(inner):
        return m.group(0)
    return f"{name}{sep}{quote}{MASK}{quote}{tail}"


def _json_sub(m: re.Match[str]) -> str:
    name, val = m.group("name"), m.group("val")
    if val[0] in "\"'":
        quote = val[0]
        inner = val[1:-1] if len(val) >= 2 and val.endswith(quote) else val[1:]
    else:
        quote, inner = '"', val
        if inner.lower() in ("null", "true", "false"):
            return m.group(0)
    if _skip_name(name, inner) or _already_masked(inner):
        return m.group(0)
    return f"{m.group('pre')}{quote}{MASK}{quote}"


def _json_esc_sub(m: re.Match[str]) -> str:
    if _skip_name(m.group("name"), m.group("val")) or _already_masked(m.group("val")):
        return m.group(0)
    return m.group("pre") + MASK


def redact_text_counted(text: str) -> tuple[str, int]:
    """(가려진 텍스트, 가린 건수)."""
    if not text:
        return text, 0
    count = 0

    def _run(pattern: re.Pattern[str], repl: Callable[[re.Match[str]], str]) -> None:
        nonlocal text, count

        def _sub(m: re.Match[str]) -> str:
            nonlocal count
            out = repl(m)
            if out != m.group(0):
                count += 1
            return out

        text = pattern.sub(_sub, text)

    def _url(m: re.Match[str]) -> str:
        if _already_masked(m.group(3)):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}:{MASK}@"

    def _url_tail(m: re.Match[str]) -> str:
        if _already_masked(m.group(3)):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}:{MASK}"

    _run(_URL_RE, _url)
    _run(_URL_TAIL_RE, _url_tail)
    _run(_ENV_RE, _env_sub)
    _run(_JSON_RE, _json_sub)
    _run(_JSON_ESC_RE, _json_esc_sub)
    _run(_MYSQL_P_RE, lambda m: m.group(0) if _already_masked(m.group(2)) else f"{m.group(1)}{MASK}")
    for pat, rep in _PREFIX_PATTERNS:
        _run(pat, lambda m, rep=rep: rep)
    return text, count


def redact_text(text: str) -> str:
    return redact_text_counted(text)[0]


def redact_tail(text: str) -> str:
    """잘린 문자열 꼬리에 남은 '닫히지 않은 비밀값' 을 끝까지 덮는다 (자르기 직후 1회).

    redact_text 의 패턴은 값이 문자열 끝(\\Z)에서 끝나는 경우도 덮으므로 같은 함수를 재적용한다.
    """
    return redact_text(text)


def _redact_keyword_value(name: str, v: Any) -> tuple[Any, int]:
    """키 이름에 비밀 키워드가 든 값 — 통째로 가린다.

    남기는 것: None·bool, 이미 가려진 문자열, 수량·통계 이름의 숫자(max_tokens: 100),
    수량·통계 이름의 묶음(token_usage: {...} 은 내려가서 개별 처리).
    """
    if v is None or isinstance(v, bool):
        return v, 0
    metric = bool(_METRIC_NAME.search(name))
    if isinstance(v, str):
        if _already_masked(v) or _MASKED_URL_VALUE.match(v) or (metric and _NUMERIC.match(v)):
            return redact_text_counted(v)
        return MASK, 1
    if isinstance(v, (int, float)):
        return (v, 0) if metric else (MASK, 1)
    if metric and isinstance(v, (dict, list, tuple)):
        return redact_value_counted(v)
    return MASK, 1


def redact_value_counted(obj: Any) -> tuple[Any, int]:
    """dict/list/tuple 을 재귀로 내려가며 가린다. (새 객체, 가린 건수) — 원본 불변."""
    if isinstance(obj, str):
        return redact_text_counted(obj)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        total = 0
        for k, v in obj.items():
            name = k if isinstance(k, str) else str(k)
            if _KEY_HAS_KEYWORD.search(name):
                nv, n = _redact_keyword_value(name, v)
            else:
                nv, n = redact_value_counted(v)
            out[k] = nv
            total += n
        return out, total
    if isinstance(obj, (list, tuple)):
        items, total = [], 0
        for v in obj:
            nv, n = redact_value_counted(v)
            items.append(nv)
            total += n
        return (tuple(items) if isinstance(obj, tuple) else items), total
    return obj, 0


def redact_value(obj: Any) -> Any:
    return redact_value_counted(obj)[0]


# 호환 별칭
redact_secrets = redact_text
redact_secrets_counted = redact_text_counted
