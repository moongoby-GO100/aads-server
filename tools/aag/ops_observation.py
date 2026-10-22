"""AAG S2 — 운영 서버 관측 (live crontab · 활성 systemd timer). 읽기 전용.

저장소 그래프(ops_assets.py)는 "커밋된 것"이고, 이 모듈은 "지금 서버에서 도는 것"이다.
둘을 한 그래프에 섞지 않는다 — 섞으면 저장소에 없는 운영 상태가 커밋된 사실처럼 보인다.
관측은 별도 문서(JSON) → 별도 테이블(aag_ops_observations)로 간다.

이 모듈은 명령을 **정의**하고 출력 텍스트를 **파싱**한다. 실행은 `collect()` 가 `run` 콜백으로만
하며, 기본 콜백은 허용 목록(COLLECT_COMMANDS)에 있는 argv 가 아니면 거부한다.

원격 Python 3.8 에서도 도는 문법만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from tools.aag import ops_assets as _oa
except ModuleNotFoundError:  # direct execution
    import ops_assets as _oa

SCHEMA_VERSION = "aag.ops_observation.v1"

# 읽기 전용 수집 명령. 키는 observation.command_status 의 이름이다.
# 쓰기·재시작·네트워크가 없다. 여기에 없는 argv 는 실행하지 않는다.
COLLECT_COMMANDS: Dict[str, List[str]] = {
    "crontab": ["crontab", "-l"],
    "timers": ["systemctl", "list-timers", "--all", "--no-pager"],
}
COMMAND_TIMEOUT_SEC = 20

_NO_CRONTAB_RE = re.compile(r"no crontab for", re.IGNORECASE)
_TIMER_FOOTER_RE = re.compile(r"^\d+\s+timers?\s+listed", re.IGNORECASE)


# ── 파싱 ───────────────────────────────────────────────────────────


def parse_crontab_output(text: str) -> Dict[str, Any]:
    """`crontab -l` 출력 → {entries, env, commented_entries}. 사용자 필드 없는 형식."""
    parsed = _oa.parse_crontab(text, user_field=False)
    return {
        "entries": [{"lineno": e["lineno"], "schedule": e["schedule"], "command": e["command"]}
                    for e in parsed["entries"]],
        "env": parsed["env"],
        "commented_entries": parsed["commented_entries"],
    }


def parse_list_timers(text: str) -> List[Dict[str, Any]]:
    """`systemctl list-timers --all --no-pager` 텍스트 → [{unit, activates, next, last, raw}]

    열은 NEXT LEFT LAST PASSED UNIT ACTIVATES 인데 날짜에 공백이 있어 열 너비로 자를 수 없다.
    줄 끝의 두 토큰(`*.timer` `*.service`)만 신뢰하고, 앞부분은 원문으로 보관한다.
    머리글·바닥글·빈 줄은 건너뛴다.
    """
    out: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _TIMER_FOOTER_RE.match(line) or line.startswith("NEXT") \
                or line.startswith("Pass --all"):
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[-2].endswith(".timer"):
            continue
        activates = parts[-1]
        out.append({
            "unit": parts[-2],
            "activates": activates,
            "when": " ".join(parts[:-2])[:200],
            "raw": line[:300],
        })
    return out


# ── 관측 문서 ──────────────────────────────────────────────────────


def _digest(body: Dict[str, Any]) -> str:
    """수집 시각·호스트를 뺀 내용만 해시한다 — 내용 동일성만 나타내며 관측 이벤트의 식별자는 아니다."""
    payload = json.dumps(
        {"crontab": body["crontab"], "timers": [t["unit"] + ">" + t["activates"] for t in body["timers"]],
         "command_status": {k: v["ok"] for k, v in sorted(body["command_status"].items())}},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_observation(project: str, host: str, collected_at: str,
                      outputs: Dict[str, Optional[str]],
                      errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """수집 출력 → 관측 문서.

    outputs[name] 이 None 이면 그 명령은 실행하지 못한 것이다(관측 못 함 ≠ 비어 있음).
    `crontab -l` 의 "no crontab for root" 는 실패가 아니라 **빈 crontab 관측**이다.
    """
    errors = errors or {}
    status: Dict[str, Dict[str, Any]] = {}
    for name in COLLECT_COMMANDS:
        text = outputs.get(name)
        ok = text is not None and name not in errors
        status[name] = {"ok": bool(ok), "argv": COLLECT_COMMANDS[name]}
        if name in errors:
            status[name]["error"] = errors[name][:300]
    cron_text = outputs.get("crontab") or ""
    if _NO_CRONTAB_RE.search(cron_text):
        cron_text = ""
    crontab = parse_crontab_output(cron_text) if status["crontab"]["ok"] else \
        {"entries": [], "env": {}, "commented_entries": 0}
    timers = parse_list_timers(outputs.get("timers") or "") if status["timers"]["ok"] else []
    doc: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "project": project,
        "host": host,
        "collected_at": collected_at,
        "command_status": status,
        "crontab": crontab,
        "timers": timers,
    }
    doc["content_digest"] = _digest(doc)
    return doc


def validate_observation(doc: Any) -> List[str]:
    """적재 전 형식 검사. 문제 목록(빈 목록이면 통과)."""
    problems: List[str] = []
    if not isinstance(doc, dict):
        return ["observation 은 객체여야 한다"]
    if doc.get("schema") != SCHEMA_VERSION:
        problems.append("schema 불일치: %r" % doc.get("schema"))
    for key in ("project", "host", "collected_at", "content_digest"):
        if not isinstance(doc.get(key), str) or not doc.get(key):
            problems.append("%s 누락" % key)
    if not isinstance(doc.get("crontab"), dict) or not isinstance(doc["crontab"].get("entries"), list):
        problems.append("crontab.entries 누락")
    if not isinstance(doc.get("timers"), list):
        problems.append("timers 누락")
    status = doc.get("command_status")
    if not isinstance(status, dict) or not all(
        isinstance(status.get(n), dict) and "ok" in status[n] for n in COLLECT_COMMANDS
    ):
        problems.append("command_status 누락")
    if not problems and _digest(doc) != doc["content_digest"]:
        problems.append("content_digest 가 내용과 맞지 않는다")
    return problems


# ── 저장소 그래프와의 대조 (읽기 전용 비교, 그래프에 합치지 않는다) ────────


def compare_with_repo(doc: Dict[str, Any], graph: Dict[str, Any]) -> Dict[str, Any]:
    """관측 timer 를 저장소의 systemd timer 노드와 이름으로 대조한다.

    - observed_not_in_repo: 서버에서 도는데 저장소에 유닛 파일이 없는 timer
    - repo_not_observed: 저장소에 있는데 서버에서 활성이 아닌 timer(timers 를 관측했을 때만)
    관측하지 못한 명령은 비교하지 않는다(없음으로 단정하지 않는다).
    """
    repo_timers: Dict[str, List[str]] = {}
    for node in graph.get("nodes") or []:
        if node.get("kind") == "systemd_unit" and node.get("unit_type") == "timer":
            repo_timers.setdefault(node.get("unit") or posixpath.basename(node["id"]), []).append(node["id"])
    result: Dict[str, Any] = {"timers_compared": False, "observed_not_in_repo": [],
                              "repo_not_observed": []}
    if not (doc.get("command_status", {}).get("timers") or {}).get("ok"):
        return result
    observed = {t["unit"] for t in doc.get("timers") or []}
    result["timers_compared"] = True
    result["observed_not_in_repo"] = sorted(observed - set(repo_timers))
    result["repo_not_observed"] = sorted(
        path for unit, paths in repo_timers.items() if unit not in observed for path in paths
    )
    return result


def resolve_cron_refs(doc: Dict[str, Any], script_files: Iterable[str],
                      py_files: Iterable[str], prefix_map: Sequence[str]) -> List[Dict[str, Any]]:
    """관측 crontab 항목이 가리키는 저장소 스크립트. 그래프 간선이 아니라 대조용 목록이다."""
    files = set(script_files) | set(py_files)
    paths = _oa.RepoPaths(files, prefix_map)
    resolver = _oa.Resolver(paths, _oa.PyIndex(sorted(set(py_files)), [""]))
    out: List[Dict[str, Any]] = []
    for entry in (doc.get("crontab") or {}).get("entries") or []:
        found = _oa.Findings()
        resolver.shell_text(entry["command"], [""], entry.get("lineno", 0), "crontab", found,
                            env=(doc.get("crontab") or {}).get("env") or {})
        out.append({"schedule": entry["schedule"], "command": entry["command"][:200],
                    "repo_files": sorted({f for f, _l, _v in found.files})})
    return out


# ── 적재 SQL ───────────────────────────────────────────────────────


def _dollar_quote(text: str) -> str:
    """텍스트에 나오지 않는 달러 인용 태그로 감싼다(SQL 주입 방지)."""
    n = 0
    while True:
        tag = "$aagops%d$" % n
        if tag not in text:
            return tag + text + tag
        n += 1


def _sql_text(value: str) -> str:
    return _dollar_quote(value)


def insert_statement(doc: Dict[str, Any]) -> str:
    """관측 문서 → 멱등 INSERT 한 문장. 검증을 통과한 문서만 받는다."""
    problems = validate_observation(doc)
    if problems:
        raise ValueError("관측 문서가 올바르지 않다: " + "; ".join(problems))
    status = {k: v["ok"] for k, v in doc["command_status"].items()}
    body = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    return (
        "INSERT INTO aag_ops_observations\n"
        "    (project, host, collected_at, schema_version, content_digest,\n"
        "     crontab_entries, active_timers, command_status, observation)\n"
        "VALUES (%s, %s, %s::timestamptz, %s, %s, %d, %d, %s::jsonb, %s::jsonb)\n"
        "ON CONFLICT ON CONSTRAINT aag_ops_observations_event_uniq DO NOTHING;\n" % (
            _sql_text(doc["project"]), _sql_text(doc["host"]), _sql_text(doc["collected_at"]),
            _sql_text(doc["schema"]), _sql_text(doc["content_digest"]),
            len(doc["crontab"]["entries"]), len(doc["timers"]),
            _sql_text(json.dumps(status, sort_keys=True)), _sql_text(body),
        )
    )


# ── 수집 실행 (호출될 때만) ────────────────────────────────────────


RunFn = Callable[[List[str], int], Tuple[int, str, str]]


def _default_run(argv: List[str], timeout: int) -> Tuple[int, str, str]:
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def collect(run: Optional[RunFn] = None) -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    """허용된 읽기 전용 명령만 실행해 (출력, 오류) 를 돌려준다. 실패는 오류로 남기고 계속한다."""
    runner = run or _default_run
    allowed = {tuple(v) for v in COLLECT_COMMANDS.values()}
    outputs: Dict[str, Optional[str]] = {}
    errors: Dict[str, str] = {}
    for name, argv in COLLECT_COMMANDS.items():
        if tuple(argv) not in allowed:  # pragma: no cover — 방어
            raise RuntimeError("허용되지 않은 명령: %r" % (argv,))
        try:
            code, out, err = runner(list(argv), COMMAND_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as exc:
            outputs[name] = None
            errors[name] = "%s: %s" % (type(exc).__name__, exc)
            continue
        text = out if code == 0 else (out + err)
        if code != 0 and not (name == "crontab" and _NO_CRONTAB_RE.search(text)):
            outputs[name] = None
            errors[name] = "exit %d: %s" % (code, (err or out).strip()[:200])
            continue
        outputs[name] = text
    return outputs, errors


def set_of_units(doc: Dict[str, Any]) -> Set[str]:
    return {t["unit"] for t in doc.get("timers") or []}
