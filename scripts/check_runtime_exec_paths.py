#!/usr/bin/env python3
"""실행 경로 점검기 (읽기 전용) — 운영 폴더에서 코드를 실행하는 cron/systemd 항목을 찾는다.

운영 폴더(/root/aads/aads-server)를 실행 경로에서 빼는 기획 B안 M2 의 점검 도구.
`crontab -l` 과 /etc/systemd/system/*.service(+ *.service.d/*.conf drop-in 반영)를 읽어
각 항목을 분류한다.

  release     릴리스 사본 / runner-live / runtime 경로에서 실행
  ops_folder  운영 폴더에서 코드를 실행 (WorkingDirectory·cd 대상·실행 파일)
  other       그 외 (운영 폴더를 인자·상태 파일로만 참조하는 경우 포함)

ops_folder 항목의 실행 파일은 릴리스 사본의 같은 경로 파일과 바이트 비교한다
(same / differs / missing_in_release) — 사본으로 옮겨도 되는지 판단하는 근거.
파일 하나가 같다고 그 파일이 import 하는 모듈까지 같다는 뜻은 아니다.

이 도구는 어떤 파일도 쓰거나 바꾸지 않는다.

종료코드: ops_folder 0건이면 0, 있으면 1(경보용), 인자 오류 2.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

DEFAULT_OPS_ROOT = "/root/aads/aads-server"
DEFAULT_RELEASE_ROOT = "/root/aads/runtime/aads-server-release"
DEFAULT_SYSTEMD_DIR = "/etc/systemd/system"
EXTRA_RELEASE_PREFIXES = ("/root/aads/runtime", "/root/aads/runner-live")

SHELLS = {"bash", "sh", "dash", "zsh"}
PYTHON_VERSIONED = re.compile(r"^python[\d.]+$")
SHELL_NOISE = {"{", "}", "then", "do", "else", "fi", "done", "!"}
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
COMPARE_RANK = {"differs": 4, "missing_in_release": 3, "ops_file_missing": 2, "not_a_file": 2, "same": 1}
MAX_DEPTH = 4


@dataclass(frozen=True)
class OptSpec:
    """명령 하나의 옵션 문법. 값을 받는 옵션을 표로 두고, 그 값을 건너뛴 뒤 실제 대상을 찾는다.

    short_val   값을 받는 짧은 옵션 글자(묶음 안에서도 인식, 값이 붙어 있으면 그 토큰 하나만 소비)
    long_val    값을 받는 긴 옵션 이름(--name value 형태. --name=value 는 항상 토큰 하나)
    positional  옵션 뒤, 실제 명령 앞에 오는 필수 위치 인자 수(timeout 의 DURATION, flock 의 lockfile 등)
    stop_on     이 옵션을 만나면 나머지는 전부 그 옵션의 대상/인자(python -m·-c, node -e)
    chdir       값이 작업 디렉터리를 바꾸는 옵션(sudo -D, env -C)
    cmd_keys    값이 셸 명령 문자열인 옵션(flock -c, env -S, su -c)
    no_cmd      이 옵션이 있으면 명령을 실행하지 않는 모드(chrt -p, command -v)
    need_flag   이 옵션이 있어야만 나머지가 명령이다(runuser -u USER -- cmd)
    """
    short_val: str = ""
    long_val: tuple = ()
    positional: int = 0
    stop_on: str = ""
    chdir: tuple = ()
    cmd_keys: tuple = ()
    no_cmd: str = ""
    need_flag: str = ""
    plus_ok: bool = False


WRAPPERS: dict[str, OptSpec] = {
    "nohup": OptSpec(),
    "setsid": OptSpec(),
    "exec": OptSpec(short_val="a"),
    "command": OptSpec(no_cmd="vV"),
    "time": OptSpec(short_val="fo", long_val=("format", "output")),
    "nice": OptSpec(short_val="n", long_val=("adjustment",)),
    "ionice": OptSpec(short_val="cnpPu", long_val=("class", "classdata", "pid", "pgid", "uid")),
    "timeout": OptSpec(short_val="ks", long_val=("kill-after", "signal"), positional=1),
    "flock": OptSpec(short_val="wEc", long_val=("timeout", "wait", "conflict-exit-code", "command"),
                     positional=1, cmd_keys=("c", "command")),
    "sudo": OptSpec(short_val="uUgCDpRrtT", long_val=("user", "other-user", "group", "close-from", "chdir",
                                                       "prompt", "chroot", "role", "type", "command-timeout"),
                    chdir=("D", "chdir")),
    "env": OptSpec(short_val="uCS", long_val=("unset", "chdir", "split-string", "block-signal",
                                              "default-signal", "ignore-signal"),
                   chdir=("C", "chdir"), cmd_keys=("S", "split-string")),
    "stdbuf": OptSpec(short_val="ioe", long_val=("input", "output", "error")),
    "chrt": OptSpec(short_val="TPD", long_val=("sched-runtime", "sched-period", "sched-deadline"),
                    positional=1, no_cmd="p"),
    "taskset": OptSpec(positional=1, no_cmd="p"),
    "xargs": OptSpec(short_val="nPIdLsEaJ", long_val=("max-args", "max-procs", "replace", "delimiter",
                                                      "max-lines", "max-chars", "eof", "arg-file")),
    "runuser": OptSpec(short_val="ugGsc", long_val=("user", "group", "supp-group", "shell", "command"),
                       cmd_keys=("c", "command"), need_flag="u"),
    "su": OptSpec(short_val="gGsc", long_val=("group", "supp-group", "shell", "command"),
                  cmd_keys=("c", "command"), need_flag="\0"),
}

# 인터프리터: 값 있는 옵션을 건너뛰고 첫 비옵션 인자를 실행 파일로 본다.
# python -m/-c, node -e/-p 는 파일이 아니라 cwd 기준 모듈·코드 실행이다.
SHELL_SPEC = OptSpec(short_val="oO", long_val=("rcfile", "init-file"), plus_ok=True)
PYTHON_SPEC = OptSpec(short_val="WXQcm", long_val=("check-hash-based-pycs",), stop_on="cm")
NODE_SPEC = OptSpec(short_val="repC", long_val=("require", "eval", "print", "import", "loader",
                                                 "experimental-loader", "input-type", "conditions"),
                    stop_on="ep")
PERL_SPEC = OptSpec(short_val="eEI", stop_on="eE")
RUBY_SPEC = OptSpec(short_val="eIrCE", stop_on="e")


def _interpreter_spec(base: str) -> OptSpec | None:
    if base in SHELLS:
        return SHELL_SPEC
    if base in ("python", "python3") or PYTHON_VERSIONED.match(base):
        return PYTHON_SPEC
    return {"node": NODE_SPEC, "perl": PERL_SPEC, "ruby": RUBY_SPEC}.get(base)


@dataclass
class Config:
    ops_root: str = DEFAULT_OPS_ROOT
    release_root: str = DEFAULT_RELEASE_ROOT
    release_prefixes: tuple = ()

    def __post_init__(self) -> None:
        self.ops_root = posixpath.normpath(self.ops_root)
        self.release_root = posixpath.normpath(self.release_root)
        if not self.release_prefixes:
            self.release_prefixes = (self.release_root, *EXTRA_RELEASE_PREFIXES)
        self.ops_re = re.compile(re.escape(self.ops_root) + r"(?![\w.\-])")


@dataclass
class Ref:
    role: str  # cwd | exec | arg
    path: str
    kind: str  # ops | release | other
    compare: str = ""
    release_path: str = ""

    def as_dict(self) -> dict:
        d = {"role": self.role, "path": self.path, "kind": self.kind}
        if self.compare:
            d["compare"] = self.compare
            d["release_path"] = self.release_path
        return d


@dataclass
class Item:
    source: str  # cron | systemd
    name: str
    command: str
    workdir: str = ""
    refs: list = field(default_factory=list)
    cls: str = "other"
    compare: str = "n/a"

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "name": self.name,
            "class": self.cls,
            "compare": self.compare,
            "workdir": self.workdir,
            "command": self.command,
            "refs": [r.as_dict() for r in self.refs],
        }


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def path_kind(path: str, cfg: Config) -> str:
    p = posixpath.normpath(path)
    if _under(p, cfg.ops_root):
        return "ops"
    if any(_under(p, pre) for pre in cfg.release_prefixes):
        return "release"
    return "other"


def _lex(text: str) -> list[str]:
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = "#"
        return list(lex)
    except ValueError:
        return text.split()


def _is_punct(tok: str) -> bool:
    return bool(tok) and all(c in "();<>|&" for c in tok)


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """리다이렉션과 그 대상 단어를 버리고, 연결자(&& || ; | & 괄호)에서 명령 단위로 자른다."""
    segments: list[list[str]] = [[]]
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if _is_punct(tok):
            if "<" in tok or ">" in tok:
                skip_next = True
            else:
                segments.append([])
            continue
        segments[-1].append(tok)
    return [s for s in segments if s]


def _abs(path: str, cwd: str) -> str:
    """절대경로로 풀어 정규화한다. 변수·~ 확장이 필요하거나 cwd 를 모르는 상대경로는 빈 문자열."""
    if not path or any(c in path for c in "$`~*?"):
        return ""
    if path.startswith("/"):
        return posixpath.normpath(path)
    if cwd:
        return posixpath.normpath(posixpath.join(cwd, path))
    return ""


def _parse_args(words: list[str], spec: OptSpec, start: int = 1) -> tuple[int, dict]:
    """words[start:] 의 옵션을 spec 에 따라 건너뛰고 (첫 피연산자 위치, 옵션 값 맵)을 돌려준다.

    피연산자가 없거나 stop_on 옵션을 만나면 위치는 len(words) 다.
    """
    flags: dict[str, str] = {}
    n = len(words)
    i = start
    pos = 0
    while i < n:
        w = words[i]
        if w == "--":
            i += 1
            while pos < spec.positional and i < n:
                pos += 1
                i += 1
            return i, flags
        if w.startswith("--"):
            name, eq, val = w[2:].partition("=")
            if eq:
                flags[name] = val
                i += 1
            elif name in spec.long_val:
                flags[name] = words[i + 1] if i + 1 < n else ""
                i += 2
            else:
                flags[name] = ""
                i += 1
            continue
        if len(w) > 1 and (w[0] == "-" or (spec.plus_ok and w[0] == "+")):
            take_next = False
            stop = False
            for j in range(1, len(w)):
                ch = w[j]
                if ch in spec.short_val:
                    rest = w[j + 1:]
                    if rest:
                        flags[ch] = rest
                    else:
                        flags[ch] = words[i + 1] if i + 1 < n else ""
                        take_next = i + 1 < n
                    stop = ch in spec.stop_on
                    break
                flags.setdefault(ch, "")
            i += 2 if take_next else 1
            if stop:
                return n, flags
            continue
        if pos < spec.positional:
            pos += 1
            i += 1
            continue
        return i, flags
    return n, flags


def _scan_data_words(words: list[str], cfg: Config, refs: list[Ref]) -> None:
    """실행 대상이 아닌 단어(인자·환경변수 값)에서 운영/릴리스 경로 참조를 모은다."""
    for w in words:
        m = cfg.ops_re.search(w)
        if m:
            refs.append(Ref("arg", w[m.start():].rstrip("\"'"), "ops"))
            continue
        for pre in cfg.release_prefixes:
            idx = w.find(pre)
            if idx >= 0:
                refs.append(Ref("arg", w[idx:].rstrip("\"'"), "release"))
                break


def _note_exec(word: str, cwd: str, cfg: Config, refs: list[Ref]) -> None:
    """경로 구분자가 있는 실행 파일(래퍼·인터프리터 자신 포함)을 exec 참조로 남긴다."""
    if "/" not in word:
        return
    resolved = _abs(word, cwd)
    if resolved:
        refs.append(Ref("exec", resolved, path_kind(resolved, cfg)))


def _scan_words(words: list[str], cwd: str, cfg: Config, refs: list[Ref], depth: int) -> str:
    data: list[str] = []

    # 래퍼(nice/ionice/timeout/flock/sudo/env/...)를 옵션 표대로 건너뛰고 실제 명령을 찾는다.
    for _ in range(MAX_DEPTH * 3):
        k = 0
        while k < len(words) and (words[k] in SHELL_NOISE or ENV_ASSIGN.match(words[k])):
            k += 1
        data.extend(words[:k])
        words = words[k:]
        if not words:
            _scan_data_words(data, cfg, refs)
            return cwd
        spec = WRAPPERS.get(posixpath.basename(words[0]))
        if spec is None:
            break
        idx, flags = _parse_args(words, spec)
        cmd_strings = [flags[k] for k in spec.cmd_keys if flags.get(k)]
        _note_exec(words[0], cwd, cfg, refs)
        data.extend(w for w in words[1:idx] if w not in cmd_strings)
        for key in spec.chdir:
            target = _abs(flags.get(key, ""), cwd)
            if target:
                cwd = target
                refs.append(Ref("cwd", target, path_kind(target, cfg)))
        for key in spec.cmd_keys:
            if flags.get(key):
                _scan_command(flags[key], cwd, cfg, refs, depth + 1)
                _scan_data_words(data, cfg, refs)
                return cwd
        if any(f in flags for f in spec.no_cmd) or (spec.need_flag and spec.need_flag not in flags):
            _scan_data_words(data, cfg, refs)
            return cwd
        words = words[idx:]
    else:
        _scan_data_words(data + words, cfg, refs)
        return cwd

    cmd = words[0]
    base = posixpath.basename(cmd)
    exec_idx: set[int] = set()

    if cmd == "cd":
        operand = next((w for w in words[1:] if not (w.startswith("-") and len(w) > 1) or w == "--"), "")
        operand = "" if operand == "--" else operand
        target = _abs(operand, cwd)
        if target:
            cwd = target
            refs.append(Ref("cwd", target, path_kind(target, cfg)))
        else:
            cwd = ""  # cd $X, cd ~ 등 알 수 없는 곳 — 이후 상대경로는 판정하지 않는다
        _scan_data_words(data, cfg, refs)
        return cwd

    ispec = _interpreter_spec(base)
    if ispec is SHELL_SPEC:
        idx, flags = _parse_args(words, ispec)
        if "c" in flags:
            _note_exec(cmd, cwd, cfg, refs)
            if idx < len(words):
                _scan_command(words[idx], cwd, cfg, refs, depth + 1)
                data.extend(words[idx + 1:])
            _scan_data_words(data + words[1:idx], cfg, refs)
            return cwd  # bash -c 는 자식 프로세스라 바깥 cwd 는 바뀌지 않는다
        if "/" in cmd:
            exec_idx.add(0)
        if idx < len(words) and words[idx] != "-":
            exec_idx.add(idx)
    elif ispec is not None:
        idx, _flags = _parse_args(words, ispec)
        if "/" in cmd:
            exec_idx.add(0)
        if idx < len(words) and words[idx] != "-":
            exec_idx.add(idx)
    elif base in ("source", ".") and len(words) > 1:
        exec_idx.add(1)
    elif "/" in cmd:
        exec_idx.add(0)

    rest: list[str] = []
    for j, w in enumerate(words):
        if j in exec_idx:
            resolved = _abs(w, cwd)
            if resolved:
                refs.append(Ref("exec", resolved, path_kind(resolved, cfg)))
        else:
            rest.append(w)
    _scan_data_words(data + rest, cfg, refs)
    return cwd


def _scan_command(text: str, cwd: str, cfg: Config, refs: list[Ref], depth: int = 0) -> str:
    """명령 문자열을 훑어 cwd/exec/arg 참조를 refs 에 모은다. 마지막 cwd 를 돌려준다."""
    if depth > MAX_DEPTH:
        return cwd
    for seg in _split_segments(_lex(text)):
        cwd = _scan_words(seg, cwd, cfg, refs, depth)
    return cwd


def compare_file(ops_path: str, cfg: Config) -> tuple[str, str]:
    """운영 폴더 파일과 릴리스 사본의 같은 상대 경로 파일을 바이트 비교한다."""
    rel = posixpath.relpath(ops_path, cfg.ops_root)
    rel_path = posixpath.join(cfg.release_root, rel)
    if not os.path.exists(ops_path):
        return "ops_file_missing", rel_path
    if not os.path.isfile(ops_path):
        return "not_a_file", rel_path
    if not os.path.isfile(rel_path):
        return "missing_in_release", rel_path
    try:
        same = filecmp.cmp(ops_path, rel_path, shallow=False)
    except OSError:
        return "missing_in_release", rel_path
    return ("same" if same else "differs"), rel_path


def classify(item: Item, cfg: Config) -> Item:
    refs: list[Ref] = []
    if item.workdir:
        wd = posixpath.normpath(item.workdir)
        refs.append(Ref("cwd", wd, path_kind(wd, cfg)))
    _scan_command(item.command, item.workdir, cfg, refs)

    # 중복 제거(순서 유지)
    seen: set = set()
    uniq: list[Ref] = []
    for r in refs:
        key = (r.role, r.path)
        if key not in seen:
            seen.add(key)
            uniq.append(r)

    for r in uniq:
        if r.role == "exec" and r.kind == "ops":
            r.compare, r.release_path = compare_file(r.path, cfg)

    driving = [r for r in uniq if r.role in ("cwd", "exec")]
    if any(r.kind == "ops" for r in driving):
        item.cls = "ops_folder"
    elif any(r.kind == "release" for r in uniq):
        item.cls = "release"
    else:
        item.cls = "other"

    cmps = [r.compare for r in uniq if r.compare]
    item.compare = max(cmps, key=lambda c: COMPARE_RANK.get(c, 0)) if cmps else "n/a"
    item.refs = uniq
    return item


# ── 입력 파서 ──────────────────────────────────────────────────────────

_CRON_LINE = re.compile(r"^(@\S+|\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.+)$")


def parse_crontab(text: str) -> list[Item]:
    items: list[Item] = []
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _CRON_LINE.match(line)
        if not m or not re.match(r"[0-9*@?]", line):
            continue  # NAME=value 환경 줄 등
        items.append(Item("cron", f"L{no}", m.group(2).strip()))
    return items


def _unit_lines(text: str) -> list[str]:
    joined: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if buf:
            line = buf + " " + line.lstrip()
            buf = ""
        if line.endswith("\\"):
            buf = line[:-1].rstrip()
            continue
        joined.append(line)
    if buf:
        joined.append(buf)
    return joined


def _apply_unit_file(text: str, exec_starts: list[str], workdir: list[str]) -> None:
    section = ""
    for line in _unit_lines(text):
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            continue
        if section != "Service" or "=" not in s:
            continue
        key, _, value = s.partition("=")
        key, value = key.strip(), value.strip()
        if key == "ExecStart":
            if value == "":
                exec_starts.clear()  # 빈 값은 목록 초기화
            else:
                exec_starts.append(value.lstrip("@-:+!"))
        elif key == "WorkingDirectory":
            workdir[:] = [value.lstrip("-")] if value else []


def parse_systemd_dir(directory: str) -> list[Item]:
    items: list[Item] = []
    base = Path(directory)
    if not base.is_dir():
        return items
    for unit in sorted(base.glob("*.service")):
        exec_starts: list[str] = []
        workdir: list[str] = []
        try:
            _apply_unit_file(unit.read_text(errors="replace"), exec_starts, workdir)
            dropin_dir = base / (unit.name + ".d")
            if dropin_dir.is_dir():
                for conf in sorted(dropin_dir.glob("*.conf")):
                    _apply_unit_file(conf.read_text(errors="replace"), exec_starts, workdir)
        except OSError:
            continue
        if not exec_starts and not workdir:
            continue
        items.append(Item("systemd", unit.name, " ; ".join(exec_starts), workdir[0] if workdir else ""))
    return items


def read_crontab_command() -> tuple[str, str]:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", f"crontab -l 실행 실패: {e}"
    if r.returncode != 0:
        return "", f"crontab -l 종료코드 {r.returncode}: {r.stderr.strip()}"
    return r.stdout, ""


# ── 출력 ───────────────────────────────────────────────────────────────

def summarize(items: Iterable[Item]) -> dict:
    items = list(items)
    summary = {"total": len(items), "release": 0, "ops_folder": 0, "other": 0}
    cmp_counts = {"same": 0, "differs": 0, "missing_in_release": 0, "other": 0}
    for it in items:
        summary[it.cls] += 1
        if it.cls == "ops_folder":
            key = it.compare if it.compare in cmp_counts else "other"
            cmp_counts[key] += 1
    summary["ops_folder_compare"] = cmp_counts
    return summary


def render_table(items: list[Item], summary: dict, warnings: list[str]) -> str:
    rows = [("SRC", "NAME", "CLASS", "COMPARE", "COMMAND")]
    for it in items:
        cmd = it.command if len(it.command) <= 90 else it.command[:87] + "..."
        wd = f"[cwd={it.workdir}] " if it.workdir and it.source == "systemd" else ""
        rows.append((it.source, it.name, it.cls, it.compare, wd + cmd))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r[:4])) + "  " + r[4] for r in rows]
    cc = summary["ops_folder_compare"]
    lines.append("")
    lines.append(
        f"total={summary['total']} release={summary['release']} ops_folder={summary['ops_folder']} other={summary['other']}"
    )
    lines.append(
        f"ops_folder compare: same={cc['same']} differs={cc['differs']} "
        f"missing_in_release={cc['missing_in_release']} other={cc['other']}"
    )
    for w in warnings:
        lines.append(f"WARN {w}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="운영 폴더 실행 경로 점검기 (읽기 전용)")
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    ap.add_argument("--crontab-file", help="crontab -l 대신 읽을 파일")
    ap.add_argument("--systemd-dir", default=DEFAULT_SYSTEMD_DIR)
    ap.add_argument("--ops-root", default=DEFAULT_OPS_ROOT)
    ap.add_argument("--release-root", default=DEFAULT_RELEASE_ROOT)
    ap.add_argument("--only-ops", action="store_true", help="ops_folder 항목만 표시")
    args = ap.parse_args(argv)

    cfg = Config(ops_root=args.ops_root, release_root=args.release_root)
    warnings: list[str] = []

    if args.crontab_file:
        try:
            cron_text = Path(args.crontab_file).read_text(errors="replace")
        except OSError as e:
            print(f"crontab 파일을 읽을 수 없습니다: {e}", file=sys.stderr)
            return 2
    else:
        cron_text, warn = read_crontab_command()
        if warn:
            warnings.append(warn)

    items = parse_crontab(cron_text) + parse_systemd_dir(args.systemd_dir)
    if not Path(args.systemd_dir).is_dir():
        warnings.append(f"systemd 디렉터리 없음: {args.systemd_dir}")
    for it in items:
        classify(it, cfg)

    summary = summarize(items)
    shown = [i for i in items if i.cls == "ops_folder"] if args.only_ops else items
    if args.json:
        print(json.dumps(
            {"summary": summary, "warnings": warnings, "items": [i.as_dict() for i in shown]},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render_table(shown, summary, warnings))
    return 1 if summary["ops_folder"] else 0


if __name__ == "__main__":
    sys.exit(main())
