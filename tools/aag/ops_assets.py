"""AAG S2 — 운영 자산 노드·간선 (스크립트 · systemd · crontab 파일 · nginx · docker-compose).

scan_aads.py 가 호출하는 순수 함수 모음이다. 파일을 직접 순회하지 않고 호출자가 넘긴
파일 목록과 `read(key) -> str` 콜백만 쓴다.

원격 호스트(contabo14, Python 3.8)로 scan_aads.py·code_deps.py 와 함께 복사되어 실행된다.
3.9+ 전용 문법·API(`ast.unparse`, `str.removeprefix`, `dict | dict`, 런타임 `list[str]`)를
쓰지 않는다.

간선 종류(전부 추가만 — 기존 kind 는 바꾸지 않는다).
- invokes : 스크립트 → 스크립트/모듈. `python -m`, `python x.py`, `bash x.sh`, `./x.sh`,
            `source x.sh`, 인라인 `python -c`/heredoc 의 import, subprocess 인자의 경로.
- runs    : timer → service, service → 스크립트/모듈, crontab 파일 → 스크립트/모듈.
- proxies : nginx location → API 네임스페이스(`ns:`) 또는 upstream.
- uses_image / builds : compose 서비스 → 이미지 / Dockerfile.

규칙 한 줄 요약.
- 저장소에 **커밋된** 상태만 다룬다. 운영 서버의 crontab·활성 timer 는 ops_observation.py 가 따로 적재한다.
- 해석하지 못한 경로는 간선 없이 unresolved 로만 센다(추측 금지). 저장소 밖 절대경로는 external 로 센다.
- 같은 (from, to, kind) 는 한 줄로 합친다.
- 정규식은 중첩 반복을 쓰지 않고 줄 단위로 훑는다(R-BG: 파국적 백트래킹 금지).
"""

from __future__ import annotations

import ast
import os
import posixpath
import re
import shlex
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from tools.aag import code_deps as _cd
except ModuleNotFoundError:  # direct `python tools/aag/scan_aads.py` execution
    import code_deps as _cd

FLAG_ENV = "AAG_SCAN_OPS_ASSETS"
_OFF_VALUES = {"0", "false", "off", "no", "disabled"}
UNRESOLVED_SAMPLE_LIMIT = 20

EdgeSet = _cd.EdgeSet
PyIndex = _cd.PyIndex

# 변수 확장이 만드는 "가상 저장소 루트". 실제 배포 경로가 아니라 내부 표식이다.
VIRTUAL_ROOT = "/__repo__"
# 작업 디렉터리를 알 수 없을 때의 표식. 이 아래의 상대 경로는 어떤 저장소 파일에도 연결하지 않는다.
UNKNOWN_CWD = "/__aag_unknown_cwd__"
_DYN = "\x00"

_SCRIPT_SUFFIXES = (".py", ".sh", ".bash")
_PY_INTERPRETER_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# 첫 단어가 이 중 하나면 파일을 "실행"하지 않고 다루기만 한다(cat x.sh, cp x.sh …).
_NON_INVOKING = {
    "cat", "cp", "mv", "rm", "ls", "grep", "egrep", "fgrep", "rg", "sed", "awk", "chmod",
    "chown", "scp", "rsync", "tar", "head", "tail", "wc", "diff", "git", "echo", "printf",
    "test", "[", "[[", "stat", "touch", "mkdir", "ln", "install", "find", "less", "more",
    "vim", "vi", "nano", "readlink", "realpath", "dirname", "basename", "md5sum",
    "sha256sum", "shellcheck", "ssh", "curl", "wget", "tee", "zip", "unzip", "gzip", "ruff",
    "pytest", "sort", "uniq", "cut", "unset", "export", "local", "readonly",
    "declare", "mapfile", "read", "trap", "set", "type", "which", "file",
}
_CONTROL_WORDS = {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "}", "fi",
                  "done", "esac", "case", "for", "in", "time"}

_ASGI_SERVERS = {"uvicorn", "gunicorn", "hypercorn", "daphne"}
_APP_TARGET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.()]*$")
_ENV_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+(?:-[A-Za-z]+\s+)?)?"
    r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?([-=+])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_SCRIPT_DIR_SUBST_RE = re.compile(
    r"\$\(\s*(?:cd\s+\"?\$\(\s*)?dirname\s+\"?\$\{?(?:BASH_SOURCE\[0\]|0)\}?\"?\s*\)?"
    r"((?:/\.\.)*)\"?(?:\s*&&\s*pwd(?:\s+-P)?)?\s*\)"
)
_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./@+~=-]+$")
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


_WRAPPERS = {
    "sudo", "env", "nohup", "timeout", "nice", "ionice", "flock", "exec", "setsid", "stdbuf",
    "time", "command", "builtin", "xargs", "runuser", "su", "sg", "chrt", "taskset", "unbuffer",
    "docker", "podman", "systemd-cat", "strace", "ltrace", "watch", "chronic", "dumb-init",
    "tini", "gosu", "su-exec",
}
_CONTAINER_LAUNCHERS = {"docker", "podman"}
_SHELL_C_WRAPPERS = {"flock", "su", "runuser", "sg"}
_BARE_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*$")


def _launches_command(toks: Sequence[str], j: int) -> bool:
    """docker/podman 뒤가 `exec|run|compose run|compose exec` 일 때만 컨테이너 안 명령을 실행한다."""
    words = [t for t in toks[j:] if not t.startswith("-")][:2]
    if not words:
        return False
    if words[0] in ("exec", "run"):
        return True
    return words[0] == "compose" and len(words) > 1 and words[1] in ("exec", "run")


def _display_ref(token: str) -> str:
    return token.replace(VIRTUAL_ROOT + "/", "").replace(_DYN, "${?}")[:160]


def enabled(environ: Optional[Dict[str, str]] = None) -> bool:
    """AAG_SCAN_OPS_ASSETS=0/false/off/no 이면 끈다. 미설정은 켜짐."""
    env = os.environ if environ is None else environ
    return str(env.get(FLAG_ENV, "1")).strip().lower() not in _OFF_VALUES


# ══════════════════════════════════════════════════════════════════════
# 경로 해석
# ══════════════════════════════════════════════════════════════════════


def _strip_prefix(raw: str, prefixes: Sequence[str]) -> Optional[str]:
    """배포 경로 접두를 저장소 상대경로로. 해당 없으면 None.

    prefixes 는 `/root/aads/aads-server/` 처럼 슬래시로 끝난다. 접두 자신(슬래시 없음)은 ""(루트).
    """
    for prefix in prefixes:
        if raw == prefix.rstrip("/"):
            return ""
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return None


class RepoPaths:
    """문자열 경로 → 저장소 파일 키. 파일 키는 `files` 에 있는 것만 돌려준다."""

    def __init__(self, files: Iterable[str], prefix_map: Sequence[str] = ()) -> None:
        self.files: Set[str] = set(files)
        prefixes = [VIRTUAL_ROOT + "/"]
        for item in prefix_map:
            item = str(item).strip()
            if item:
                prefixes.append(item if item.endswith("/") else item + "/")
        self.prefixes = sorted(set(prefixes), key=len, reverse=True)

    def to_rel(self, raw: str, bases: Sequence[str]) -> Tuple[Optional[str], str]:
        """→ (저장소 상대 경로|None, 분류). 분류: repo | relative_miss | external | dynamic"""
        raw = raw.strip()
        if not raw:
            return None, "dynamic"
        if "$" in raw or "`" in raw or "*" in raw or "{" in raw or "%" in raw:
            return None, "dynamic"
        if raw.startswith("/"):
            rel = _strip_prefix(raw, self.prefixes)
            if rel is None:
                return None, "external"
            return self._norm(rel), "repo"
        if raw.startswith("~"):
            return None, "external"
        cands: List[str] = []
        directional = raw.startswith("./") or raw.startswith("../")
        for base in bases:
            cands.append(posixpath.join(base, raw) if base else raw)
        for cand in cands:
            norm = self._norm(cand)
            if norm is not None:
                if norm.startswith("/"):
                    return None, "external"
                return norm, "repo"
        return None, "relative_miss"

    @staticmethod
    def _norm(rel: str) -> Optional[str]:
        norm = posixpath.normpath(rel) if rel else ""
        if norm in (".", ""):
            return ""
        if norm.startswith("../") or norm == "..":
            return None
        return norm

    def resolve_file(self, raw: str, bases: Sequence[str]) -> Tuple[Optional[str], str]:
        """to_rel 후 실제 파일인 첫 후보. → (파일 키|None, 분류)"""
        raw = raw.strip()
        if not raw:
            return None, "dynamic"
        if "$" in raw or "`" in raw or "*" in raw or "{" in raw or "%" in raw:
            return None, "dynamic"
        if raw.startswith("/") or raw.startswith("~"):
            rel, kind = self.to_rel(raw, bases)
            if rel is not None and rel in self.files:
                return rel, "repo"
            return None, ("not_in_repo" if kind == "repo" else kind)
        repo_checked = unknown = False
        for base in bases:
            cand = posixpath.join(base, raw) if base else raw
            if cand.startswith("/"):
                # 절대 작업 디렉터리: 배포 접두로 저장소에 대응될 때만 저장소 파일이다.
                if base == UNKNOWN_CWD:
                    unknown = True
                    continue
                rel = _strip_prefix(posixpath.normpath(cand), self.prefixes)
                if rel is None:
                    continue
                repo_checked = True
                norm = self._norm(rel)
            else:
                repo_checked = True
                norm = self._norm(cand)
            if norm and norm in self.files:
                return norm, "repo"
        if not repo_checked:
            return None, ("unknown_cwd" if unknown else "external")
        return None, "not_in_repo"


# ══════════════════════════════════════════════════════════════════════
# 변수 확장 (셸·systemd·compose 공용, 상수 기본값만)
# ══════════════════════════════════════════════════════════════════════


def expand_vars(text: str, env: Dict[str, str], depth: int = 0) -> str:
    """`$X`, `${X}`, `${X:-default}` 를 env/기본값으로 바꾼다. 못 바꾸면 `$` 가 남는다."""
    if "$" not in text or depth > 5:
        return text

    def repl(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(4)
        op, arg = m.group(2), m.group(3)
        if name in env and env[name] != "":
            return env[name]
        if op in ("-", "=") and arg is not None:
            return expand_vars(arg, env, depth + 1)
        return m.group(0)

    out = text
    for _ in range(5):
        new = _VAR_RE.sub(repl, out)
        if new == out:
            break
        out = new
    return out


def _script_dir_value(value: str, script_dir: str) -> Optional[str]:
    """`$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)` 류 → 가상 경로. 아니면 None."""
    if "dirname" not in value or ("BASH_SOURCE" not in value and "$0" not in value):
        return None
    ups = value.split("dirname", 1)[1].count("/..")
    base = script_dir
    for _ in range(ups):
        base = posixpath.dirname(base)
    return VIRTUAL_ROOT + ("/" + base if base else "")


def subst_script_dir(line: str, script_dir: str) -> str:
    """`$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)` 류를 가상 저장소 경로로 바꾼다."""
    if "dirname" not in line:
        return line

    def repl(m: "re.Match[str]") -> str:
        base = script_dir
        for _ in range(m.group(1).count("/..")):
            base = posixpath.dirname(base)
        return VIRTUAL_ROOT + ("/" + base if base else "")

    return _SCRIPT_DIR_SUBST_RE.sub(repl, line)


def collect_shell_vars(lines: Sequence[str], script_dir: str, base_env: Dict[str, str]) -> Dict[str, str]:
    """스크립트 안의 단순 대입(`NAME=value`, `NAME="${ENV:-default}"`)을 모은다."""
    env = dict(base_env)
    for line in lines:
        m = _ASSIGN_RE.match(line)
        if not m:
            continue
        name, rest = m.group(1), m.group(2).strip()
        sd = _script_dir_value(rest, script_dir)
        if sd is not None:
            env[name] = sd
            continue
        if "$(" in rest or "`" in rest:
            continue
        try:
            toks = shlex.split(rest, comments=True)
        except ValueError:
            continue
        if not toks:
            env[name] = ""
            continue
        value = expand_vars(toks[0], env)
        if "$" in value:
            continue
        env[name] = value
    return env


# ══════════════════════════════════════════════════════════════════════
# 명령 줄 분석
# ══════════════════════════════════════════════════════════════════════


class Findings:
    """한 자산에서 나온 호출 대상 모음."""

    def __init__(self) -> None:
        self.files: List[Tuple[str, int, str]] = []
        self.unresolved: List[Dict[str, Any]] = []
        self.external = 0

    def add_file(self, rel: str, lineno: int, via: str) -> None:
        self.files.append((rel, lineno, via))


class Resolver:
    """명령 토큰열 → 저장소 대상. 모든 자산 종류가 공유한다."""

    def __init__(self, paths: RepoPaths, py_index: PyIndex) -> None:
        self.paths = paths
        self.py_index = py_index

    def module_file(self, dotted: str) -> Optional[str]:
        return self.py_index.module(dotted) or self.py_index.module(dotted + ".__main__")

    def inline_python(self, code: str, lineno: int, owner: str, out: Findings) -> None:
        imports, err = _cd.extract_py_imports(code)
        if err:
            return
        targets, _ext, bad = _cd.resolve_py_imports(owner, imports, self.py_index)
        for rel, _ln in targets:
            out.add_file(rel, lineno, "inline_import")
        for item in bad:
            if item["import"].startswith("."):
                continue
            out.unresolved.append({"file": owner, "lineno": lineno, "ref": item["import"],
                                   "reason": "inline_" + item["reason"]})

    def _ref(self, token: str, bases: Sequence[str], lineno: int, owner: str, via: str,
             out: Findings) -> None:
        if _DYN in token or "$" in token:
            if token.endswith(_SCRIPT_SUFFIXES):
                out.unresolved.append({"file": owner, "lineno": lineno, "ref": _display_ref(token),
                                       "reason": "dynamic_path"})
            return
        if not _PATH_TOKEN_RE.match(token):
            return
        rel, why = self.paths.resolve_file(token, bases)
        if rel:
            out.add_file(rel, lineno, via)
        elif why == "external":
            out.external += 1
        elif why == "unknown_cwd":
            out.unresolved.append({"file": owner, "lineno": lineno, "ref": _display_ref(token),
                                   "reason": "unknown_cwd"})
        elif why in ("not_in_repo", "relative_miss"):
            out.unresolved.append({"file": owner, "lineno": lineno, "ref": _display_ref(token),
                                   "reason": "not_in_repo"})

    def cwd_bases(self, raw: str, bases: Sequence[str], allow_relative: bool = True) -> List[str]:
        """명시된 작업 디렉터리 → 상대 경로 해석 기준. 저장소 밖·미상이면 저장소 파일에 닿지 않는 기준."""
        raw = raw.strip()
        if not raw or _DYN in raw or any(ch in raw for ch in "$%`*{") or raw.startswith("~"):
            return [UNKNOWN_CWD]
        if not raw.startswith("/") and not allow_relative:
            return [UNKNOWN_CWD]
        rel, kind = self.paths.to_rel(raw, bases)
        if kind == "repo" and rel is not None:
            return [rel]
        if raw.startswith("/"):
            return [posixpath.normpath(raw)]
        return [UNKNOWN_CWD]

    def tokens(self, toks: Sequence[str], bases: Sequence[str], lineno: int, owner: str,
               out: Findings, depth: int = 0) -> None:
        """한 단순 명령의 토큰열에서 실제로 실행되는 대상만 찾는다. 인자는 호출이 아니다."""
        i = 0
        n = len(toks)
        while i < n and (toks[i] in _CONTROL_WORDS or _ENV_TOKEN_RE.match(toks[i])):
            i += 1
        if i >= n or posixpath.basename(toks[i]) in _NON_INVOKING:
            return
        wrapper = ""
        prev_opt = False
        cur_bases = list(bases)
        while i < n:
            tok = toks[i]
            base = posixpath.basename(tok)
            if wrapper and base in _NON_INVOKING:
                return
            if base in _ASGI_SERVERS:
                self._asgi(toks, i + 1, lineno, owner, out)
                return
            if _PY_INTERPRETER_RE.match(base):
                self._python(toks, i + 1, cur_bases, lineno, owner, out)
                return
            if base in _SHELLS:
                self._shell(toks, i + 1, cur_bases, lineno, owner, out, depth)
                return
            if base in ("source", ".") and not wrapper:
                if i + 1 < n and toks[i + 1].endswith(_SCRIPT_SUFFIXES):
                    self._ref(toks[i + 1], cur_bases, lineno, owner, "path", out)
                return
            if tok.endswith(_SCRIPT_SUFFIXES) and not tok.startswith("-") \
                    and not (wrapper and ":" in tok):
                self._ref(tok, cur_bases, lineno, owner, "path", out)
                return
            if wrapper in _CONTAINER_LAUNCHERS and tok in ("exec", "run", "compose"):
                i += 1  # 서브커맨드 단어는 래퍼 이름(`exec`)과 같아도 명령이 아니다.
                continue
            if base in _WRAPPERS:
                if base in _CONTAINER_LAUNCHERS and not _launches_command(toks, i + 1):
                    return
                wrapper = base
                prev_opt = False
                i += 1
                continue
            if not wrapper:
                if tok.startswith("$") and any(t.endswith(_SCRIPT_SUFFIXES) for t in toks[i + 1:]):
                    out.unresolved.append({"file": owner, "lineno": lineno, "ref": _display_ref(tok),
                                           "reason": "dynamic_command"})
                return  # 알 수 없는 프로그램: 뒤따르는 토큰은 그 프로그램의 인자다.
            if tok in ("-c", "-lc") and wrapper in _SHELL_C_WRAPPERS and i + 1 < n and depth < 2:
                self.shell_text(toks[i + 1], cur_bases, lineno, owner, out, depth + 1)
                return
            if wrapper in _CONTAINER_LAUNCHERS and tok in ("-w", "--workdir") and i + 1 < n:
                cur_bases = self.cwd_bases(toks[i + 1], cur_bases, allow_relative=False)
                i += 2
                continue
            if wrapper in _CONTAINER_LAUNCHERS and tok.startswith("--workdir="):
                cur_bases = self.cwd_bases(tok.split("=", 1)[1], cur_bases, allow_relative=False)
                i += 1
                continue
            if wrapper not in _CONTAINER_LAUNCHERS and _BARE_WORD_RE.match(tok) and not prev_opt:
                return  # 래퍼 뒤의 알 수 없는 프로그램
            prev_opt = tok.startswith("-") and "=" not in tok
            i += 1

    def _shell(self, toks: Sequence[str], j: int, bases: Sequence[str], lineno: int, owner: str,
               out: Findings, depth: int) -> None:
        """`bash [옵션] 스크립트 인자…` | `bash -c '문자열' …` — 첫 피연산자만 실행 대상이다."""
        n = len(toks)
        while j < n:
            tok = toks[j]
            if _is_c_flag(tok):
                if j + 1 < n and depth < 2:
                    self.shell_text(toks[j + 1], bases, lineno, owner, out, depth + 1)
                return
            if tok in ("-o", "+o", "-O", "+O"):
                j += 2
                continue
            if tok == "--":
                j += 1
                continue
            if tok.startswith("-") or tok.startswith("+"):
                j += 1
                continue
            if tok.endswith(_SCRIPT_SUFFIXES):
                self._ref(tok, bases, lineno, owner, "path", out)
            return

    def _asgi(self, toks: Sequence[str], j: int, lineno: int, owner: str, out: Findings) -> None:
        for tok in toks[j:]:
            if _APP_TARGET_RE.match(tok):
                self._module(tok.split(":", 1)[0], lineno, owner, out)
                return

    def _python(self, toks: Sequence[str], i: int, bases: Sequence[str], lineno: int,
                owner: str, out: Findings) -> int:
        """python 인터프리터 뒤의 옵션을 건너뛰고 -m/-c/스크립트를 처리한다. → 다음 토큰 위치"""
        n = len(toks)
        while i < n:
            tok = toks[i]
            if tok == "-m" and i + 1 < n:
                if toks[i + 1] in _ASGI_SERVERS:
                    self._asgi(toks, i + 2, lineno, owner, out)
                else:
                    self._module(toks[i + 1], lineno, owner, out)
                return i + 2
            if tok == "-c" and i + 1 < n:
                self.inline_python(toks[i + 1], lineno, owner, out)
                return i + 2
            if tok in ("-W", "-X", "-Q") and i + 1 < n:
                i += 2
                continue
            if tok.startswith("-") and tok != "-":
                i += 1
                continue
            if tok == "-":
                return i + 1
            if tok.endswith(_SCRIPT_SUFFIXES):
                self._ref(tok, bases, lineno, owner, "path", out)
            return i + 1
        return i

    def _module(self, dotted: str, lineno: int, owner: str, out: Findings) -> None:
        if not dotted or _DYN in dotted or "$" in dotted:
            return
        if not self.py_index.is_project_name(dotted):
            return
        hit = self.module_file(dotted)
        if hit:
            out.add_file(hit, lineno, "module")
        else:
            out.unresolved.append({"file": owner, "lineno": lineno, "ref": "-m " + dotted,
                                   "reason": "module_not_found"})

    def shell_text(self, text: str, bases: Sequence[str], lineno: int, owner: str,
                   out: Findings, depth: int = 0, env: Optional[Dict[str, str]] = None) -> List[str]:
        """여러 줄이 아닌 한 명령 문자열(cron 명령·ExecStart·`bash -c` 인자)."""
        env = env or {}
        cwd_bases = list(bases)
        for seg in split_segments(text):
            seg = [expand_vars(t, env) if "$" in t else t for t in seg]
            if not seg:
                continue
            head = _first_word(seg)
            if head == "cd" and len(seg) > seg.index("cd") + 1:
                operands = [t for t in seg[seg.index("cd") + 1:] if not t.startswith("-")]
                cwd_bases = self.cwd_bases(operands[0] if operands else "", cwd_bases)
                continue
            self.tokens(seg, cwd_bases, lineno, owner, out, depth)
        return cwd_bases


def _is_c_flag(tok: str) -> bool:
    return tok.startswith("-") and not tok.startswith("--") and tok.endswith("c") and len(tok) <= 4


def _first_word(seg: Sequence[str]) -> str:
    for tok in seg:
        if tok in _CONTROL_WORDS:
            continue
        if _ENV_TOKEN_RE.match(tok):
            continue
        return posixpath.basename(tok) if tok.startswith("/") else tok
    return ""


def split_segments(line: str) -> List[List[str]]:
    """한 줄을 `; && || |` 기준 단순 명령들로 쪼개고 리다이렉션을 버린다."""
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = "#"
        toks = list(lex)
    except ValueError:
        toks = line.split()
    segments: List[List[str]] = []
    cur: List[str] = []
    skip_next = False
    for tok in toks:
        if skip_next:
            skip_next = False
            continue
        if tok and all(ch in "();<>|&" for ch in tok):
            if "<" in tok or ">" in tok:
                skip_next = not tok.endswith("&")
                continue
            if cur:
                segments.append(cur)
                cur = []
            continue
        cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def logical_lines(text: str) -> List[Tuple[int, str]]:
    """`\\` 줄 이음을 합친 (시작 줄번호, 내용). 주석·빈 줄 제외."""
    out: List[Tuple[int, str]] = []
    buf = ""
    start = 0
    for no, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = no
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        buf += raw
        stripped = buf.strip()
        if stripped and not stripped.startswith("#"):
            out.append((start, stripped))
        buf = ""
    if buf.strip() and not buf.strip().startswith("#"):
        out.append((start, buf.strip()))
    return out


def analyze_shell_script(text: str, rel: str, resolver: Resolver,
                         base_env: Optional[Dict[str, str]] = None) -> Findings:
    """셸 스크립트 본문 → 호출 대상. heredoc 은 python 일 때만 본다."""
    out = Findings()
    script_dir = posixpath.dirname(rel)
    lines = [(no, subst_script_dir(ln, script_dir)) for no, ln in logical_lines(text)]
    env = collect_shell_vars([ln for _, ln in lines], script_dir, base_env or {})
    env.setdefault("PWD", VIRTUAL_ROOT + ("/" + script_dir if script_dir else ""))
    bases = [script_dir, ""] if script_dir else [""]
    phys = text.splitlines()
    skip_until: Optional[Tuple[str, bool]] = None
    heredoc_py: Optional[Tuple[str, bool, int, List[str]]] = None
    handled: Set[int] = set()
    # 물리 줄을 먼저 훑어 heredoc 본문 줄번호를 걸러낸다.
    body_lines: Set[int] = set()
    py_bodies: List[Tuple[int, str]] = []
    for no, raw in enumerate(phys, 1):
        if skip_until is not None:
            delim, dash = skip_until
            body_lines.add(no)
            if (raw.strip() if dash else raw.rstrip()) == delim:
                skip_until = None
                if heredoc_py is not None:
                    py_bodies.append((heredoc_py[2], "\n".join(heredoc_py[3])))
                    heredoc_py = None
            elif heredoc_py is not None:
                heredoc_py[3].append(raw)
            continue
        m = _HEREDOC_RE.search(raw)
        if m and not raw.lstrip().startswith("#"):
            dash, delim = bool(m.group(1)), m.group(3)
            head = raw[:m.start()]
            is_py = bool(re.search(r"(?:^|[\s/])python(?:\d+(?:\.\d+)*)?(?:\s|$)", head))
            skip_until = (delim, dash)
            heredoc_py = (delim, dash, no, []) if is_py else None
    for lineno, line in lines:
        if lineno in body_lines or lineno in handled:
            continue
        bases = resolver.shell_text(line, bases, lineno, rel, out, env=env)
    for lineno, code in py_bodies:
        resolver.inline_python(code, lineno, rel, out)
    return out


# ══════════════════════════════════════════════════════════════════════
# Python 스크립트 (subprocess / runpy 인자)
# ══════════════════════════════════════════════════════════════════════

_SUBPROCESS_CALLEES = {
    "run", "call", "check_call", "check_output", "Popen", "system", "popen", "execv", "execve",
    "execvp", "execl", "execlp", "spawnl", "spawnv", "create_subprocess_exec",
    "create_subprocess_shell", "getoutput", "getstatusoutput",
}
_RUNPY_CALLEES = {"run_module", "run_path"}


def _dirname(value: str) -> str:
    return _DYN if _DYN in value else posixpath.dirname(value)


def _join(parts: Sequence[str]) -> str:
    return posixpath.join(*parts) if parts else _DYN


class _PyEval:
    """AST 표현식 → 문자열. `__file__` 기준 경로와 단순 상수 대입만 값을 가진다.

    알 수 없는 조각은 \x00 이며, 저장소 루트라고 추측하지 않는다.
    """

    def __init__(self, rel: str, names: Optional[Dict[str, str]] = None) -> None:
        self.rel = rel
        self.names: Dict[str, str] = names if names is not None else {}

    def __call__(self, node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                else self(v.value) if isinstance(v, ast.FormattedValue) else _DYN
                for v in node.values
            )
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return VIRTUAL_ROOT + "/" + self.rel
            return self.names.get(node.id, _DYN)
        if isinstance(node, ast.Attribute):
            if node.attr == "executable":
                return "python3"
            if node.attr == "parent":
                return _dirname(self(node.value))
            return _DYN
        if isinstance(node, ast.Subscript):
            value = node.value
            index = getattr(node.slice, "value", node.slice) \
                if node.slice.__class__.__name__ == "Index" else node.slice
            if isinstance(value, ast.Attribute) and value.attr == "parents" \
                    and isinstance(index, ast.Constant) and isinstance(index.value, int) \
                    and 0 <= index.value < 8:
                cur = self(value.value)
                for _ in range(index.value + 1):
                    cur = _dirname(cur)
                return cur
            return _DYN
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return _join([self(node.left), self(node.right)])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self(node.left) + self(node.right)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("resolve", "absolute") and isinstance(func, ast.Attribute) and not node.args:
                return self(func.value)
            if name in ("abspath", "realpath", "normpath", "fspath", "str") and len(node.args) == 1:
                value = self(node.args[0])
                if name != "str" and _DYN not in value and value:
                    value = posixpath.normpath(value)
                return value
            if name == "dirname" and len(node.args) == 1:
                return _dirname(self(node.args[0]))
            if name in ("join", "Path", "PurePosixPath", "PurePath") and node.args:
                return _join([self(a) for a in node.args])
        return _DYN


def _py_constants(tree: ast.AST, ev: _PyEval) -> None:
    """한 번만 대입되고 값이 확정되는 이름(`ROOT = Path(__file__).parents[1]`)을 ev.names 에 모은다."""
    counts: Dict[str, int] = {}
    shadowed: Set[str] = set()
    assigns: List[Tuple[int, str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            shadowed.add(node.arg)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            counts[name] = counts.get(name, 0) + 1
            assigns.append((getattr(node, "lineno", 0), name, node.value))
        elif isinstance(node, (ast.AugAssign, ast.For, ast.With, ast.AnnAssign)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    counts[sub.id] = counts.get(sub.id, 0) + 2
    for _no, name, value in sorted(assigns, key=lambda item: item[0]):
        if counts.get(name, 0) != 1 or name in shadowed:
            continue
        got = ev(value)
        if got and _DYN not in got:
            ev.names[name] = got


def analyze_python_script(source: str, rel: str, resolver: Resolver) -> Findings:
    """subprocess·os.system·runpy 호출 인자에서 호출 대상을 뽑는다. import 는 S1 의 몫이다."""
    out = Findings()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return out
    script_dir = posixpath.dirname(rel)
    default_bases = [script_dir, ""] if script_dir else [""]
    ev = _PyEval(rel)
    _py_constants(tree, ev)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        lineno = getattr(node, "lineno", 0)
        first = node.args[0]
        if callee in _RUNPY_CALLEES:
            text = ev(first)
            if callee == "run_module":
                resolver._module(text, lineno, rel, out)
            else:
                resolver._ref(text, default_bases, lineno, rel, "runpy", out)
            continue
        if callee not in _SUBPROCESS_CALLEES:
            continue
        # subprocess 의 cwd= 는 상대 경로의 기준이다. 저장소 밖·미상이면 저장소 파일에 닿지 않는다.
        bases = default_bases
        for kw in node.keywords:
            if kw.arg == "cwd" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None):
                bases = resolver.cwd_bases(ev(kw.value), default_bases)
        if isinstance(first, (ast.List, ast.Tuple)):
            toks = [ev(el) for el in first.elts]
            if callee in ("execl", "execlp", "spawnl"):
                toks = [ev(a) for a in node.args]
            resolver.tokens(toks, bases, lineno, rel, out)
        elif callee in ("execv", "execve", "execvp") and len(node.args) > 1 \
                and isinstance(node.args[1], (ast.List, ast.Tuple)):
            toks = [ev(el) for el in node.args[1].elts]
            resolver.tokens(toks, bases, lineno, rel, out)
        else:
            text = ev(first).replace(_DYN, "$__aag_dyn__")
            if text.strip():
                resolver.shell_text(text, bases, lineno, rel, out)
    return out


# ══════════════════════════════════════════════════════════════════════
# systemd
# ══════════════════════════════════════════════════════════════════════

_EXEC_KEYS = ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost",
              "ExecReload", "ExecCondition")
_TIMER_KEYS = ("OnCalendar", "OnBootSec", "OnStartupSec", "OnActiveSec", "OnUnitActiveSec",
               "OnUnitInactiveSec")
UNIT_SUFFIXES = (".service", ".timer")


def parse_unit(text: str) -> Dict[str, List[Tuple[str, str, int]]]:
    """→ {섹션: [(키, 값, 줄번호)]}. `\\` 줄 이음 처리, 중복 키 보존."""
    sections: Dict[str, List[Tuple[str, str, int]]] = {}
    current = ""
    buf = ""
    start = 0
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not buf:
            start = no
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        stripped = buf.strip()
        buf = ""
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections.setdefault(current, [])
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        sections.setdefault(current, []).append((key.strip(), value.strip(), start))
    return sections


def unit_info(rel: str, text: str) -> Dict[str, Any]:
    sections = parse_unit(text)
    name = posixpath.basename(rel)
    utype = "timer" if name.endswith(".timer") else "service" if name.endswith(".service") else "other"
    info: Dict[str, Any] = {"rel": rel, "name": name, "type": utype, "sections": sections}
    timer = [f"{k}={v}" for k, v, _ in sections.get("Timer", []) if k in _TIMER_KEYS]
    info["schedule"] = "; ".join(timer)
    unit_val = next((v for k, v, _ in sections.get("Timer", []) if k == "Unit"), "")
    info["activates"] = unit_val or (name[:-len(".timer")] + ".service" if utype == "timer" else "")
    return info


def _unit_lookup_names(unit: str) -> List[str]:
    names = [unit]
    if "@" in unit:
        head, _, tail = unit.partition("@")
        suffix = "." + tail.rsplit(".", 1)[-1] if "." in tail else ""
        names.append(head + "@" + suffix)
    return names


def link_systemd(units: Dict[str, Dict[str, Any]], resolver: Resolver,
                 edges: EdgeSet) -> Dict[str, Any]:
    """timer→service, service→스크립트/모듈 `runs` 간선과 집계."""
    by_name: Dict[str, List[str]] = {}
    for rel, info in units.items():
        by_name.setdefault(info["name"], []).append(rel)
    stats = {"timer_links": 0, "timer_unresolved": 0, "timer_ambiguous": 0,
             "exec_commands": 0, "exec_resolved": 0, "external": 0}
    unresolved: List[Dict[str, Any]] = []
    for rel in sorted(units):
        info = units[rel]
        if info["type"] == "timer":
            target = None
            for cand_name in _unit_lookup_names(info["activates"]):
                found = by_name.get(cand_name, [])
                if len(found) == 1:
                    target = found[0]
                elif len(found) > 1:
                    same_dir = [f for f in found if posixpath.dirname(f) == posixpath.dirname(rel)]
                    if len(same_dir) == 1:
                        target = same_dir[0]
                    else:
                        stats["timer_ambiguous"] += 1
                        unresolved.append({"file": rel, "ref": info["activates"],
                                           "reason": "ambiguous_unit"})
                        target = ""
                if target is not None:
                    break
            if target:
                if edges.add(rel, target, "runs", 0, via="timer"):
                    stats["timer_links"] += 1
            elif target is None:
                stats["timer_unresolved"] += 1
                unresolved.append({"file": rel, "ref": info["activates"], "reason": "unit_not_in_repo"})
            continue
        if info["type"] != "service":
            continue
        sections = info["sections"]
        env: Dict[str, str] = {}
        for key, value, _ in sections.get("Service", []):
            if key == "Environment":
                try:
                    for item in shlex.split(value):
                        if "=" in item:
                            k, _, v = item.partition("=")
                            env[k] = v
                except ValueError:
                    pass
        workdir = next((v for k, v, _ in sections.get("Service", []) if k == "WorkingDirectory"), "")
        bases = [""]
        if workdir:
            # `-` 접두는 "없어도 무시"일 뿐 경로가 아니다. 저장소 밖·`~`·`%h` 는 저장소 파일에 닿지 않는다.
            bases = resolver.cwd_bases(expand_vars(workdir.lstrip("-"), env), [""], allow_relative=False)
        for key, value, lineno in sections.get("Service", []):
            if key not in _EXEC_KEYS:
                continue
            command = value.lstrip("-@:+!")
            if not command:
                continue
            stats["exec_commands"] += 1
            found = Findings()
            resolver.shell_text(command, bases, lineno, rel, found, env=env)
            for dst, ln, via in found.files:
                edges.add(rel, dst, "runs", ln, via=via)
            stats["exec_resolved"] += 1 if found.files else 0
            stats["external"] += found.external
            unresolved.extend(found.unresolved)
    stats["unresolved"] = unresolved
    return stats


# ══════════════════════════════════════════════════════════════════════
# crontab
# ══════════════════════════════════════════════════════════════════════

_CRON_MACROS = {"@reboot", "@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually",
                "@midnight"}
_CRON_FIELD_RE = re.compile(r"^[0-9*/,\-?A-Za-z#]+$")
_CRON_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


def _cron_schedule(parts: List[str]) -> Optional[Tuple[str, int]]:
    """→ (일정 문자열, 소비한 토큰 수) 또는 None."""
    if not parts:
        return None
    if parts[0] in _CRON_MACROS:
        return parts[0], 1
    if len(parts) >= 6 and all(_CRON_FIELD_RE.match(p) for p in parts[:5]) \
            and any(ch.isdigit() or ch == "*" for ch in parts[0]):
        return " ".join(parts[:5]), 5
    return None


def parse_crontab(text: str, user_field: bool = False) -> Dict[str, Any]:
    """crontab 텍스트 → {entries, env, commented_entries}.

    user_field=True 면 /etc/cron.d 형식(일정 뒤에 사용자 필드)이다.
    `# DISABLED_x */5 …` 처럼 주석 처리된 일정 줄은 commented_entries 로만 센다.
    """
    entries: List[Dict[str, Any]] = []
    env: Dict[str, str] = {}
    commented = 0
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            parts = body.split()
            if parts and not _CRON_ENV_RE.match(body):
                if _cron_schedule(parts) or (len(parts) > 6 and _cron_schedule(parts[1:])):
                    commented += 1
            continue
        if _CRON_ENV_RE.match(line):
            key, _, value = line.partition("=")
            val = value.strip().strip("'\"")
            env[key.strip()] = val
            continue
        parts = line.split()
        got = _cron_schedule(parts)
        if not got:
            continue
        schedule, used = got
        rest = parts[used:]
        user = ""
        if user_field and rest:
            user, rest = rest[0], rest[1:]
        if not rest:
            continue
        entries.append({"lineno": no, "schedule": schedule, "user": user,
                        "command": " ".join(rest)})
    return {"entries": entries, "env": env, "commented_entries": commented}


def link_crontab(rel: str, parsed: Dict[str, Any], resolver: Resolver, edges: EdgeSet) -> Dict[str, Any]:
    out = Findings()
    resolved = 0
    env = dict(parsed.get("env") or {})
    for entry in parsed["entries"]:
        found = Findings()
        resolver.shell_text(entry["command"], [""], entry["lineno"], rel, found, env=env)
        hit = False
        for dst, ln, via in found.files:
            edges.add(rel, dst, "runs", ln, via=via, schedule=entry["schedule"])
            hit = True
        resolved += 1 if hit else 0
        out.unresolved.extend(found.unresolved)
        out.external += found.external
    return {"entries": len(parsed["entries"]), "resolved": resolved,
            "commented_entries": parsed["commented_entries"],
            "external": out.external, "unresolved": out.unresolved}


# ══════════════════════════════════════════════════════════════════════
# nginx
# ══════════════════════════════════════════════════════════════════════


def _nginx_tokens(text: str) -> List[Tuple[str, int]]:
    """→ [(토큰, 줄번호)]. 토큰은 단어 | '{' | '}' | ';'. 주석·따옴표 처리."""
    toks: List[Tuple[str, int]] = []
    i, n, line = 0, len(text), 1
    buf = ""
    buf_line = 1

    def flush() -> None:
        nonlocal buf
        if buf:
            toks.append((buf, buf_line))
            buf = ""

    while i < n:
        ch = text[i]
        if ch == "\n":
            flush()
            line += 1
            i += 1
        elif ch in " \t\r":
            flush()
            i += 1
        elif ch == "#":
            flush()
            while i < n and text[i] != "\n":
                i += 1
        elif ch in "{};":
            flush()
            toks.append((ch, line))
            i += 1
        elif ch in "'\"":
            if not buf:
                buf_line = line
            quote = ch
            i += 1
            chunk = ""
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    chunk += text[i + 1]
                    i += 2
                    continue
                if text[i] == "\n":
                    line += 1
                chunk += text[i]
                i += 1
            i += 1
            buf += chunk
        else:
            if not buf:
                buf_line = line
            buf += ch
            i += 1
    flush()
    return toks


class NginxBlock:
    def __init__(self, name: str, args: List[str], lineno: int) -> None:
        self.name = name
        self.args = args
        self.lineno = lineno
        self.directives: List[Tuple[str, List[str], int]] = []
        self.blocks: List["NginxBlock"] = []


def parse_nginx(text: str) -> NginxBlock:
    root = NginxBlock("", [], 0)
    stack = [root]
    words: List[Tuple[str, int]] = []
    for tok, line in _nginx_tokens(text):
        if tok == ";":
            if words:
                stack[-1].directives.append((words[0][0], [w for w, _ in words[1:]], words[0][1]))
            words = []
        elif tok == "{":
            if words:
                block = NginxBlock(words[0][0], [w for w, _ in words[1:]], words[0][1])
                stack[-1].blocks.append(block)
                stack.append(block)
            words = []
        elif tok == "}":
            words = []
            if len(stack) > 1:
                stack.pop()
        else:
            words.append((tok, line))
    return root


def looks_like_nginx(text: str) -> bool:
    return "server" in text and "location" in text and "{" in text


def _walk_blocks(block: NginxBlock) -> Iterable[NginxBlock]:
    for child in block.blocks:
        yield child
        for sub in _walk_blocks(child):
            yield sub


_REGEX_META = set("([{.*+?|$\\^")


def _regex_literal_prefix(pattern: str) -> str:
    pat = pattern[1:] if pattern.startswith("^") else pattern
    out = ""
    for ch in pat:
        if ch in _REGEX_META:
            break
        out += ch
    if "/" not in out:
        return ""
    return out[:out.rfind("/") + 1] if out and not out.endswith("/") else out


def parse_proxy_pass(value: str) -> Dict[str, Any]:
    """`http://host:port/uri` → {scheme, host, uri(None 이면 URI 없음), dynamic}"""
    if "$" in value:
        return {"dynamic": True}
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://([^/]*)(/.*)?$", value)
    if not m:
        return {"dynamic": True}
    return {"scheme": m.group(1), "host": m.group(2), "uri": m.group(3), "dynamic": False}


def nginx_locations(rel: str, text: str) -> Dict[str, Any]:
    """→ {locations: [...], upstreams: {name: [servers]}}"""
    root = parse_nginx(text)
    upstreams: Dict[str, List[str]] = {}
    for block in _walk_blocks(root):
        if block.name == "upstream" and block.args:
            upstreams[block.args[0]] = sorted(
                a[0] for d, a, _ in block.directives if d == "server" and a
                for a in [a]
            )
    locations: List[Dict[str, Any]] = []
    seen_ids: Dict[str, int] = {}
    for server in [b for b in _walk_blocks(root) if b.name == "server"]:
        names = next((d_args for d, d_args, _ in server.directives if d == "server_name"), [])
        listen = next((d_args for d, d_args, _ in server.directives if d == "listen"), [])
        label = ",".join(names) or "_"
        port = listen[0] if listen else ""
        label = label + ("@" + port if port else "")
        for loc in _walk_blocks(server):
            if loc.name != "location" or not loc.args:
                continue
            modifier = loc.args[0] if loc.args[0] in ("=", "~", "~*", "^~") and len(loc.args) > 1 else ""
            path = loc.args[-1]
            lit = _regex_literal_prefix(path) if modifier in ("~", "~*") else path
            pass_args = next((a for d, a, _ in loc.directives if d == "proxy_pass"), None)
            handler = "proxy" if pass_args else (
                "static" if any(d in ("root", "alias") for d, _, _ in loc.directives) else
                "return" if any(d == "return" for d, _, _ in loc.directives) else "other")
            loc_id = "nginx:%s::%s::%s%s" % (rel, label, modifier + " " if modifier else "", path)
            seen_ids[loc_id] = seen_ids.get(loc_id, 0) + 1
            if seen_ids[loc_id] > 1:
                loc_id += "~%d" % seen_ids[loc_id]
            locations.append({
                "id": loc_id, "file": rel, "server": label, "path": path, "modifier": modifier,
                "literal_prefix": lit, "regex": modifier in ("~", "~*"), "handler": handler,
                "lineno": loc.lineno,
                "proxy_pass": parse_proxy_pass(pass_args[0]) if pass_args else None,
            })
    return {"locations": locations, "upstreams": upstreams}


def link_nginx(parsed: Dict[str, Any], api_roots: Sequence[str],
               ns_of: Callable[[str], str], ns_nodes: Set[str],
               add: Callable[..., None], edges: EdgeSet) -> Dict[str, Any]:
    """location 노드와 proxies 간선. 반환은 집계."""
    stats = {"locations": 0, "proxied": 0, "ns_linked": 0, "ns_missing": 0,
             "prefix_only": 0, "dynamic": 0}
    missing: List[Dict[str, Any]] = []
    upstream_servers = parsed["upstreams"]
    roots = [[s for s in r.split("/") if s] for r in api_roots]
    for loc in parsed["locations"]:
        stats["locations"] += 1
        extra: Dict[str, Any] = {
            "origin": "repo", "file": loc["file"], "server": loc["server"], "path": loc["path"],
            "handler": loc["handler"], "lineno": loc["lineno"],
        }
        if loc["modifier"]:
            extra["modifier"] = loc["modifier"]
        add(loc["id"], "nginx_location", **extra)
        pp = loc["proxy_pass"]
        if not pp:
            continue
        stats["proxied"] += 1
        if pp.get("dynamic"):
            stats["dynamic"] += 1
            continue
        host = pp["host"]
        up_id = "upstream:" + host
        servers = upstream_servers.get(host)
        up_extra: Dict[str, Any] = {"origin": "repo", "host": host}
        if servers is not None:
            up_extra["servers"] = servers
        add(up_id, "upstream", **up_extra)
        edges.add(loc["id"], up_id, "proxies", loc["lineno"], via="upstream")
        backend = pp["uri"] if pp.get("uri") is not None else loc["literal_prefix"]
        if not backend:
            continue
        segs = [s for s in backend.split("/") if s]
        matched_root = None
        for rseg in sorted(roots, key=len, reverse=True):
            if rseg and segs[:len(rseg)] == rseg:
                matched_root = rseg
                break
        if matched_root is None:
            continue
        rest = segs[len(matched_root):]
        if not rest:
            stats["prefix_only"] += 1
            continue
        ns = ns_of(backend)
        if "ns:" + ns in ns_nodes:
            if edges.add(loc["id"], "ns:" + ns, "proxies", loc["lineno"], via="namespace"):
                stats["ns_linked"] += 1
        else:
            stats["ns_missing"] += 1
            missing.append({"file": loc["file"], "lineno": loc["lineno"], "ref": ns,
                            "reason": "namespace_not_in_graph"})
    stats["unresolved"] = missing
    return stats


# ══════════════════════════════════════════════════════════════════════
# docker-compose
# ══════════════════════════════════════════════════════════════════════

_COMPOSE_NAME_RE = re.compile(r"^(?:docker-)?compose(?:[._-].+)?\.ya?ml$")


def is_compose_name(rel: str) -> bool:
    return bool(_COMPOSE_NAME_RE.match(posixpath.basename(rel)))


def _as_tokens(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    return []


def link_compose(rel: str, text: str, resolver: Resolver, exists: Callable[[str], bool],
                 add: Callable[..., None], edges: EdgeSet) -> Dict[str, Any]:
    """compose 파일 한 개 → 서비스·이미지·Dockerfile 노드와 uses_image/builds/runs 간선."""
    stats: Dict[str, Any] = {"services": 0, "images": 0, "builds": 0, "runs": 0, "error": ""}
    unresolved: List[Dict[str, Any]] = []
    try:
        import yaml
        data = yaml.safe_load(text) or {}
    except Exception as exc:  # noqa: BLE001 — 한 파일 때문에 스캔을 막지 않는다
        stats["error"] = "%s: %s" % (type(exc).__name__, exc)
        stats["unresolved"] = unresolved
        return stats
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        stats["unresolved"] = unresolved
        return stats
    file_dir = posixpath.dirname(rel)
    for name in sorted(services, key=str):
        svc = services[name]
        if not isinstance(svc, dict):
            continue
        sid = "compose:%s:%s" % (rel, name)
        extra: Dict[str, Any] = {"origin": "repo", "file": rel, "service": str(name)}
        if svc.get("container_name"):
            extra["container_name"] = str(svc["container_name"])
        add(sid, "compose_service", **extra)
        stats["services"] += 1
        image = svc.get("image")
        if isinstance(image, str) and image.strip():
            ref = expand_vars(image.strip(), {})
            add("image:" + ref, "image", origin="repo", ref=ref)
            if edges.add(sid, "image:" + ref, "uses_image", 0):
                stats["images"] += 1
        build = svc.get("build")
        if build:
            ctx_dir = build if isinstance(build, str) else (build.get("context") or ".")
            dockerfile = "Dockerfile" if isinstance(build, str) else (build.get("dockerfile") or "Dockerfile")
            ctx_rel = posixpath.normpath(posixpath.join(file_dir, str(ctx_dir)))
            df_rel = posixpath.normpath(posixpath.join(ctx_rel, str(dockerfile)))
            if "$" not in df_rel and not df_rel.startswith("..") and exists(df_rel):
                add(df_rel, "dockerfile", origin="repo")
                if edges.add(sid, df_rel, "builds", 0):
                    stats["builds"] += 1
            else:
                unresolved.append({"file": rel, "ref": df_rel, "reason": "dockerfile_not_in_repo"})
        workdir = svc.get("working_dir")
        bases = [""]
        if isinstance(workdir, str):
            bases = resolver.cwd_bases(expand_vars(workdir, {}), [""], allow_relative=False)
        # 실제 argv 는 entrypoint + command 다. 둘을 따로 분석하면 `entrypoint: [python3]` +
        # `command: [-m, mod]` 같은 조합의 대상이 사라진다.
        entry = [expand_vars(t, {}) for t in _as_tokens(svc.get("entrypoint"))]
        cmd = [expand_vars(t, {}) for t in _as_tokens(svc.get("command"))]
        argv = entry + cmd
        if argv:
            field = "entrypoint+command" if entry and cmd else "entrypoint" if entry else "command"
            found = Findings()
            if not entry and argv[0].startswith("-"):
                unresolved.append({"file": rel, "lineno": 0, "ref": "%s: %s" % (name, " ".join(argv)[:120]),
                                   "reason": "image_entrypoint_unknown"})
            resolver.tokens(argv, bases, 0, rel, found)
            for dst, _ln, via in found.files:
                add(dst, "script" if dst.endswith((".sh", ".bash")) else "module")
                if edges.add(sid, dst, "runs", 0, via=via, field=field):
                    stats["runs"] += 1
            unresolved.extend(found.unresolved)
    stats["unresolved"] = unresolved
    return stats


# ══════════════════════════════════════════════════════════════════════
# 파일 분류
# ══════════════════════════════════════════════════════════════════════


def is_crontab_name(rel: str) -> bool:
    name = posixpath.basename(rel).lower()
    if name.endswith(".cron"):
        return True
    return "crontab" in name and name.endswith((".txt", ".cron", "crontab"))


def crontab_has_user_field(rel: str) -> bool:
    return rel.endswith(".cron") or "/cron.d/" in rel


def classify_ops_file(rel: str) -> str:
    """파일 키 → script | systemd | crontab | nginx_candidate | compose | ''."""
    name = posixpath.basename(rel)
    if name.endswith(UNIT_SUFFIXES):
        return "systemd"
    if is_compose_name(rel):
        return "compose"
    if is_crontab_name(rel):
        return "crontab"
    if name.endswith(".conf") and ("nginx" in rel.lower()):
        return "nginx_candidate"
    return ""


# ══════════════════════════════════════════════════════════════════════
# 오케스트레이션
# ══════════════════════════════════════════════════════════════════════

NODE_KINDS = ("script", "systemd_unit", "crontab", "nginx_location", "compose_service",
              "image", "upstream", "dockerfile")
EDGE_KINDS = ("invokes", "runs", "proxies", "uses_image", "builds")


def _script_kind(rel: str) -> str:
    return "script" if rel.endswith((".sh", ".bash")) else "module"


def build(
    cfg: Dict[str, Any],
    script_files: Sequence[str],
    asset_files: Sequence[str],
    py_files: Sequence[str],
    read: Callable[[str], str],
    import_roots: Sequence[str],
    api_roots: Sequence[str],
    ns_of: Callable[[str], str],
    existing_ids: Set[str],
    add: Callable[..., None],
    node_attrs: Callable[[str], Dict[str, Any]],
) -> Dict[str, Any]:
    """운영 자산을 그래프에 더한다. 반환: {edges, stats, script_files}

    add(node_id, kind, **extra) 는 이미 있는 id 를 덮어쓰지 않는다. node_attrs(id) 는 노드 dict
    (속성 덧붙이기용)를 돌려준다.
    """
    edges = EdgeSet()
    scripts = sorted(set(script_files))
    assets = sorted(set(asset_files) - set(scripts))
    all_files = set(scripts) | set(assets) | set(py_files)
    paths = RepoPaths(all_files, cfg.get("path_prefix_map") or [])
    py_index = PyIndex(sorted(set(py_files) | {f for f in scripts if f.endswith(".py")}),
                       list(import_roots) or [""])
    resolver = Resolver(paths, py_index)
    unresolved: List[Dict[str, Any]] = []
    errors: List[str] = []
    external = 0
    sh_count = py_count = 0

    def guarded(label: str, rel: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — 한 파일 때문에 스캔 전체를 막지 않는다
            errors.append("%s %s: %s: %s" % (label, rel, type(exc).__name__, exc))

    # ── 스크립트 ────────────────────────────────────────────────────
    for rel in scripts:
        lang = "py" if rel.endswith(".py") else "sh"
        add(rel, _script_kind(rel), origin="repo", lang=lang)
        attrs = node_attrs(rel)
        attrs.setdefault("origin", "repo")
        attrs.setdefault("lang", lang)
        if not attrs.get("role"):
            attrs["role"] = "script"
        text = read(rel)
        if lang == "sh":
            sh_count += 1
            found_box: List[Findings] = []
            guarded("script", rel, lambda: found_box.append(
                analyze_shell_script(text, rel, resolver)))
        else:
            py_count += 1
            found_box = []
            guarded("script", rel, lambda: found_box.append(
                analyze_python_script(text, rel, resolver)))
        for found in found_box:
            external += found.external
            unresolved.extend(found.unresolved)
            for dst, ln, via in found.files:
                add(dst, _script_kind(dst), origin="repo")
                edges.add(rel, dst, "invokes", ln, via=via)

    # ── systemd ─────────────────────────────────────────────────────
    units: Dict[str, Dict[str, Any]] = {}
    crons: Dict[str, Dict[str, Any]] = {}
    nginx_files: List[str] = []
    compose_files: List[str] = []
    for rel in assets:
        kind = classify_ops_file(rel)
        if kind == "systemd":
            def load_unit(rel: str = rel) -> None:
                info = unit_info(rel, read(rel))
                units[rel] = info
            guarded("systemd", rel, load_unit)
        elif kind == "crontab":
            def load_cron(rel: str = rel) -> None:
                parsed = parse_crontab(read(rel), user_field=crontab_has_user_field(rel))
                if parsed["entries"]:
                    crons[rel] = parsed
            guarded("crontab", rel, load_cron)
        elif kind == "nginx_candidate":
            nginx_files.append(rel)
        elif kind == "compose":
            compose_files.append(rel)

    for rel, info in sorted(units.items()):
        extra: Dict[str, Any] = {"origin": "repo", "unit_type": info["type"], "unit": info["name"]}
        if info["schedule"]:
            extra["schedule"] = info["schedule"]
        add(rel, "systemd_unit", **extra)
    sysd: Dict[str, Any] = {}
    guarded("systemd", "*", lambda: sysd.update(link_systemd(units, resolver, edges)))
    unresolved.extend(sysd.pop("unresolved", []))
    external += sysd.get("external", 0)

    # ── crontab ─────────────────────────────────────────────────────
    cron_stats = {"files": 0, "entries": 0, "resolved": 0, "commented_entries": 0}
    for rel, parsed in sorted(crons.items()):
        add(rel, "crontab", origin="repo", entries=len(parsed["entries"]),
            schedules=sorted({e["schedule"] for e in parsed["entries"]})[:20])
        box: Dict[str, Any] = {}
        guarded("crontab", rel, lambda: box.update(link_crontab(rel, parsed, resolver, edges)))
        cron_stats["files"] += 1
        cron_stats["entries"] += box.get("entries", 0)
        cron_stats["resolved"] += box.get("resolved", 0)
        cron_stats["commented_entries"] += box.get("commented_entries", 0)
        external += box.get("external", 0)
        unresolved.extend(box.get("unresolved", []))

    # ── nginx ───────────────────────────────────────────────────────
    ng_stats = {"files": 0, "locations": 0, "proxied": 0, "ns_linked": 0, "ns_missing": 0,
                "prefix_only": 0, "dynamic": 0}
    ns_nodes = {i for i in existing_ids if i.startswith("ns:")}
    for rel in nginx_files:
        text = read(rel)
        if not looks_like_nginx(text):
            continue
        ng_stats["files"] += 1
        box = {}

        def do_nginx(rel: str = rel, text: str = text) -> None:
            parsed = nginx_locations(rel, text)
            box.update(link_nginx(parsed, api_roots, ns_of, ns_nodes, add, edges))

        guarded("nginx", rel, do_nginx)
        for key in ("locations", "proxied", "ns_linked", "ns_missing", "prefix_only", "dynamic"):
            ng_stats[key] += box.get(key, 0)
        unresolved.extend(box.get("unresolved", []))

    # ── compose ─────────────────────────────────────────────────────
    cp_stats = {"files": 0, "services": 0, "images": 0, "builds": 0, "runs": 0, "errors": 0}
    for rel in compose_files:
        text = read(rel)
        if "services" not in text:
            continue
        box = {}
        guarded("compose", rel, lambda: box.update(link_compose(
            rel, text, resolver, lambda r: r in all_files, add, edges)))
        if "services" in box or box:
            cp_stats["files"] += 1
        for key in ("services", "images", "builds", "runs"):
            cp_stats[key] += box.get(key, 0)
        if box.get("error"):
            cp_stats["errors"] += 1
            errors.append("compose %s: %s" % (rel, box["error"]))
        unresolved.extend(box.get("unresolved", []))

    for edge in edges.edges:
        for end in (edge["from"], edge["to"]):
            add(end, _script_kind(end), origin="repo")
    by_edge: Dict[str, int] = {}
    for edge in edges.edges:
        by_edge[edge["kind"]] = by_edge.get(edge["kind"], 0) + 1
    reasons: Dict[str, int] = {}
    for item in unresolved:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    timers = sum(1 for u in units.values() if u["type"] == "timer")
    stats: Dict[str, Any] = {
        "enabled": True,
        "flag": FLAG_ENV,
        "edges": {k: by_edge.get(k, 0) for k in EDGE_KINDS},
        "scripts": {"total": len(scripts), "sh": sh_count, "py": py_count},
        "systemd": {"units": len(units), "timers": timers,
                    "services": sum(1 for u in units.values() if u["type"] == "service"),
                    **{k: v for k, v in sysd.items()}},
        "crontab": cron_stats,
        "schedule_nodes": timers + cron_stats["files"],
        "nginx": ng_stats,
        "compose": cp_stats,
        "external_refs": external,
        "unresolved_refs": len(unresolved),
        "unresolved_by_reason": dict(sorted(reasons.items())),
        "unresolved_sample": unresolved[:UNRESOLVED_SAMPLE_LIMIT],
        "errors": errors[:UNRESOLVED_SAMPLE_LIMIT],
    }
    return {"edges": edges.edges, "stats": stats, "script_files": scripts}
