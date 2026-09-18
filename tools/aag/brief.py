#!/usr/bin/env python3
"""AAG 착수 브리프 — 워커가 코드를 파기 전에 받는 아키텍처 사실 요약.

scan_aads.py 가 만든 그래프에서 **이번 지시서와 연결된 부분만** 잘라
마크다운으로 뱉는다. 러너(scripts/pipeline-runner.sh)가 워커 프롬프트
맨 앞 가까이에 붙인다.

왜 만들었나. 워커 프롬프트의 "[STEP 0 기존 구현 조사]" 는 "기존 함수·엔드포인트·
DB 접점을 먼저 확인하라" 고 **지시만 하고 지도를 주지 않았다**. 그래서 워커는
매번 grep 으로 처음부터 팠고, `app/api/chat.py` 와 `app/routers/chat.py` 처럼
같은 네임스페이스를 둘이 소유한 곳에서는 **죽은 쪽을 고치는** 일이 반복됐다.
정적분석은 이미 그 사실을 알고 있었는데 워커만 몰랐다.

설계 원칙 하나: **브리프는 본 작업을 절대 막지 않는다.** 그래프가 없거나,
깨졌거나, 매칭이 0건이거나, 시간이 넘으면 전부 `exit 0` + 빈(또는 부분) 출력이다.
브리프는 보조 정보이지 게이트가 아니다 — 여기서 실패해서 러너 작업이 죽으면
"도움 주려다 파이프라인을 세운" 것이 된다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))

# 타이머 최신본이 1순위, 저장소 커밋본이 2순위다. 타이머(2시간)가 도는 서버에서는
# 커밋본보다 항상 새롭다.
TIMER_GRAPH = Path("/var/log/aads-pipeline/aag-struct/aads-graph.json")

# 프로젝트별 그래프 탐색 경로 — CWD 기준(러너가 각 프로젝트 워크디렉터리에서 호출한다).
# `*` 를 포함한 항목은 glob 으로 풀어 최신(mtime) 1개를 쓴다. 여기 없는 프로젝트는
# DEFAULT_GRAPH_CANDIDATES 로 폴백한다. 표는 여기 하나로만 모은다 — 흩지 마라.
GRAPH_GLOB_PATTERN = "reports/aag/*-graph.json"
PROJECT_GRAPH_CANDIDATES: dict[str, list[str]] = {
    "AADS": [str(TIMER_GRAPH), "reports/aag/aads-graph.json"],
    "ACCT": ["reports/aag/acct-graph.json"],
    "NTV2": ["reports/aag/ntv2-graph.json", GRAPH_GLOB_PATTERN],
}
DEFAULT_GRAPH_CANDIDATES: list[str] = [GRAPH_GLOB_PATTERN]

DEFAULT_MAX_BYTES = 6000
TIME_BUDGET_SEC = 15.0

# 섹션 우선순위 — 예산이 모자라면 큰 숫자부터 버린다.
# ⚠결함이 1순위인 이유: "여기 이미 부채가 걸려 있다" 가 브리프에서 유일하게
# 워커의 행동을 바꾸는 정보다. 마운트 경로는 그 다음(어느 파일이 살아 있는가).
PRI_FINDINGS = 1
PRI_MOUNTS = 2
PRI_CALLERS = 3
PRI_TABLES = 4
PRI_UNRESOLVED = 5

FOOTER = (
    "위 목록은 정적분석 실측이다. 여기에 없는 경로를 만들기 전에 왜 없는지 먼저 확인하라.\n"
    "DUP_MODULE/DOUBLE_MOUNT 로 표시된 파일은 **둘 중 죽은 쪽을 고치고 있지 않은지** 먼저 확인하라."
)

TRUNCATED_MARK = "(브리프 일부 생략)"

# 중첩 반복을 쓰지 않는다 (R-BG #3). `(?:[\w/]+/)+` 형태는 대용량 입력에서
# 백트래킹이 폭발한다 — 2026-09-14 에 10시간 CPU 를 태운 정규식이 그 형태였다.
RE_FILEPATH = re.compile(r"[A-Za-z0-9_./\-]*/[A-Za-z0-9_.\-]+\.(?:py|tsx|ts|sh|js|jsx|sql)\b")
RE_ROUTE = re.compile(r"/api/[A-Za-z0-9_\-/{}.]*[A-Za-z0-9_\-}]")
RE_QUOTED = re.compile(r"[`\"']([A-Za-z0-9_./\-]{3,80})[`\"']")
RE_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

MAX_SEEDS = 20
MAX_LINES_PER_SECTION = 40
HUB_DEGREE = 20
# 한 섹션이 예산을 전부 먹지 못하게 한다. 우선순위(⚠결함 먼저)는 그대로 지키되,
# 결함이 길다는 이유로 "어느 파일을 건드리는가" 가 통째로 잘려 나가면 브리프의
# 본래 목적이 사라진다.
SECTION_BUDGET_RATIO = 0.55
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class Deadline:
    """시간 상한 (R-BG #1). 넘으면 부분 결과라도 내보낸다."""

    def __init__(self, budget: float) -> None:
        self._end = time.monotonic() + budget
        self.hit = False

    def expired(self) -> bool:
        if self.hit:
            return True
        if time.monotonic() >= self._end:
            self.hit = True
        return self.hit


# ── 그래프 로딩 ────────────────────────────────────────────────────────


def _resolve_glob_candidate(pattern: str) -> Path | None:
    try:
        matches = [p for p in Path.cwd().glob(pattern) if p.is_file()]
    except OSError:
        return None
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def resolve_graph_path(explicit: str | None, project: str) -> tuple[Path | None, str]:
    """쓸 그래프 파일을 고른다. 없으면 (None, 사유)."""
    if explicit:
        p = Path(explicit)
        # 명시 지정은 그것만 본다 — 지정한 파일이 없는데 조용히 다른 그래프로
        # 폴백하면 "무엇을 근거로 만든 브리프인가" 를 알 수 없게 된다.
        if p.is_file():
            return p, ""
        return None, f"지정한 그래프 없음: {p}"

    for cand in PROJECT_GRAPH_CANDIDATES.get(project, DEFAULT_GRAPH_CANDIDATES):
        if "*" in cand:
            found = _resolve_glob_candidate(cand)
            if found is not None:
                return found, ""
            continue
        p = Path(cand)
        try:
            resolved = p if p.is_absolute() else Path.cwd() / p
            if resolved.is_file():
                return resolved, ""
        except OSError:
            continue
    return None, f"그래프 파일 없음 (project={project})"


def load_graph(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:  # JSONDecodeError 는 ValueError 다
        return None, f"그래프 읽기 실패 ({path}): {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return None, f"그래프 형식이 예상과 다르다 ({path})"
    return data, ""


# ── 지시서 → 키워드 → 그래프 노드 ──────────────────────────────────────


def extract_tokens(text: str) -> dict[str, list[str]]:
    """지시서에서 후보 키워드를 뽑는다. 채택 여부는 그래프가 정한다."""
    paths: list[str] = []
    routes: list[str] = []
    symbols: list[str] = []

    # 줄 단위로 훑는다 — 대용량 입력에 re.S 를 걸지 않기 위해서다 (R-BG #3).
    for line in text.splitlines():
        if len(line) > 4000:
            line = line[:4000]
        paths.extend(RE_FILEPATH.findall(line))
        routes.extend(RE_ROUTE.findall(line))
        for quoted in RE_QUOTED.findall(line):
            symbols.append(quoted)
            if RE_FILEPATH.fullmatch(quoted):
                paths.append(quoted)
        symbols.extend(RE_SNAKE.findall(line))

    return {
        "paths": _dedup(paths),
        "routes": _dedup(routes),
        "symbols": _dedup(symbols),
    }


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def match_nodes(tokens: dict[str, list[str]], nodes: dict[str, dict], deadline: Deadline) -> list[str]:
    """그래프에 **실제로 존재하는 것만** 채택한다."""
    node_ids = list(nodes)
    seeds: list[str] = []

    def add(node_id: str) -> None:
        if node_id in nodes and node_id not in seeds and len(seeds) < MAX_SEEDS:
            seeds.append(node_id)

    for token in tokens["paths"]:
        if deadline.expired():
            break
        if token in nodes:
            add(token)
            continue
        # 저장소 상대경로(app/api/chat.py)와 절대경로(프런트) 가 섞여 있다.
        suffix = "/" + token.lstrip("/")
        hits = [nid for nid in node_ids if nid.endswith(suffix)]
        if not hits:
            base = "/" + token.rsplit("/", 1)[-1]
            hits = [nid for nid in node_ids if nid.endswith(base)]
        for nid in hits[:4]:
            add(nid)

    # 라우트는 가장 긴(구체적인) 네임스페이스 하나만 고른다.
    ns_paths = {nodes[nid].get("path", ""): nid for nid in node_ids if nodes[nid].get("kind") == "namespace"}
    for route in tokens["routes"]:
        if deadline.expired():
            break
        best = ""
        for ns_path in ns_paths:
            if ns_path and route.startswith(ns_path.rstrip("/")) and len(ns_path) > len(best):
                best = ns_path
        if best:
            add(ns_paths[best])

    for sym in tokens["symbols"]:
        if deadline.expired():
            break
        if f"table:{sym}" in nodes:
            add(f"table:{sym}")

    return seeds


def hub_nodes(nodes: dict[str, dict], edges: list[dict], seeds: list[str]) -> set[str]:
    """모두와 연결된 노드는 관련 판정에서 뺀다 — 전부를 "관련" 으로 만들기 때문이다.

    실측 두 건. `app/main.py` 는 84개 라우터를 mount 하고, `table:pipeline_jobs` 는
    23개 모듈이 쓴다. 이 둘을 경유해 1홉을 펴면 관련 결함 3건이 13건이 된다 —
    브리프가 길어지는 정도가 아니라 **진짜 관련분이 묻힌다**.
    씨앗 자신은 예외다(그 테이블을 고치러 온 작업이면 그 테이블 결함은 관련이다).
    """
    degree: dict[str, int] = {}
    for e in edges:
        for key in ("from", "to"):
            nid = e.get(key)
            if nid:
                degree[nid] = degree.get(nid, 0) + 1
    seed_set = set(seeds)
    return {
        nid
        for nid, n in nodes.items()
        if nid not in seed_set and (n.get("kind") == "entrypoint" or degree.get(nid, 0) >= HUB_DEGREE)
    }


def expand(seeds: list[str], edges: list[dict], hops: int, deadline: Deadline) -> set[str]:
    frontier = set(seeds)
    known = set(seeds)
    for _ in range(max(0, hops)):
        if deadline.expired():
            break
        nxt: set[str] = set()
        for e in edges:
            src, dst = e.get("from"), e.get("to")
            if src in frontier and dst and dst not in known:
                nxt.add(dst)
            elif dst in frontier and src and src not in known:
                nxt.add(src)
        if not nxt:
            break
        known |= nxt
        frontier = nxt
    return known


# ── 섹션 조립 ──────────────────────────────────────────────────────────


def build_sections(
    graph: dict[str, Any],
    seeds: list[str],
    related: set[str],
    deadline: Deadline,
) -> list[tuple[int, str, list[str]]]:
    nodes = {n["id"]: n for n in graph.get("nodes", []) if isinstance(n, dict) and n.get("id")}
    edges = [e for e in graph.get("edges", []) if isinstance(e, dict)]
    seed_set = set(seeds)

    mounts_by_target: dict[str, list[dict]] = {}
    owns_by_module: dict[str, list[str]] = {}
    calls_by_ns: dict[str, list[dict]] = {}
    calls_by_source: dict[str, list[dict]] = {}
    queries_by_module: dict[str, list[str]] = {}
    users_by_table: dict[str, list[str]] = {}

    for e in edges:
        kind = e.get("kind")
        src, dst = e.get("from"), e.get("to")
        if kind == "mounts" and dst:
            mounts_by_target.setdefault(dst, []).append(e)
        elif kind == "owns" and src and dst:
            owns_by_module.setdefault(src, []).append(dst)
        elif kind == "calls" and dst:
            calls_by_ns.setdefault(dst, []).append(e)
            if src:
                calls_by_source.setdefault(src, []).append(e)
        elif kind == "queries" and src and dst:
            queries_by_module.setdefault(src, []).append(dst)
            users_by_table.setdefault(dst, []).append(src)

    seed_ns = {ns for m in seeds for ns in owns_by_module.get(m, [])}
    seed_ns |= {s for s in seeds if nodes.get(s, {}).get("kind") == "namespace"}
    seed_tables = {t for m in seeds for t in queries_by_module.get(m, [])}
    seed_tables |= {s for s in seeds if s.startswith("table:")}

    sections: list[tuple[int, str, list[str]]] = []

    # ── 건드릴 파일과 실제 마운트 경로 ──
    lines: list[str] = []
    for sid in seeds:
        node = nodes.get(sid, {})
        kind = node.get("kind")
        if kind == "namespace":
            owners = [m for m, nss in owns_by_module.items() if sid in nss]
            lines.append(f"- 네임스페이스 `{node.get('path', sid)}` — 소유: " + (", ".join(f"`{o}`" for o in owners) or "없음(ORPHAN)"))
            continue
        if kind == "table":
            continue
        if kind == "frontend":
            lines.append(f"- `{sid}` — 프런트 파일 (백엔드 라우트 아님)")
            continue
        mounted = mounts_by_target.get(sid, [])
        owned = owns_by_module.get(sid, [])
        if mounted:
            for m in mounted[:3]:
                ns_txt = ", ".join(nodes.get(n, {}).get("path", n) for n in owned) or "(네임스페이스 미확정)"
                routes = node.get("routes")
                routes_txt = f", 라우트 {routes}개" if isinstance(routes, int) else ""
                lines.append(
                    f"- `{sid}` — `{m.get('from')}`:{m.get('lineno')} 에서 prefix `{m.get('prefix', '')}` 로 mount"
                    f" → 실제 경로 `{ns_txt}`{routes_txt}"
                )
        elif kind == "router_module":
            lines.append(f"- `{sid}` — APIRouter 는 있는데 **어디에도 mount 되지 않았다**(죽은 라우터 의심)")
        else:
            lines.append(f"- `{sid}` — 라우터가 아닌 모듈 (mount 없음)")
    if lines:
        sections.append((PRI_MOUNTS, "## 건드릴 파일과 실제 마운트 경로", lines))

    # ── 이 모듈을 부르는 곳 ──
    lines = []
    for ns in sorted(seed_ns):
        for e in calls_by_ns.get(ns, [])[:8]:
            lines.append(
                f"- `{e.get('from')}`:{e.get('lineno')} → {e.get('method', '')} `{e.get('path', '')}`"
                f" (네임스페이스 `{nodes.get(ns, {}).get('path', ns)}`)"
            )
    for sid in seeds:
        if nodes.get(sid, {}).get("kind") != "frontend":
            continue
        for e in calls_by_source.get(sid, [])[:8]:
            owners = [m for m, nss in owns_by_module.items() if e.get("to") in nss]
            owner_txt = ", ".join(f"`{o}`" for o in owners) or "(소유 모듈 미확정)"
            lines.append(f"- `{sid}`:{e.get('lineno')} 가 호출 → {e.get('method', '')} `{e.get('path', '')}` — 처리: {owner_txt}")
    for sid in seeds:
        for m in mounts_by_target.get(sid, [])[:2]:
            lines.append(f"- `{m.get('from')}`:{m.get('lineno')} 가 `{sid}` 를 include_router 한다")
    if lines:
        sections.append((PRI_CALLERS, "## 이 모듈을 부르는 곳", _dedup(lines)))

    # ── 이 모듈이 쓰는 테이블 ──
    lines = []
    for sid in seeds:
        for t in sorted(set(queries_by_module.get(sid, [])))[:15]:
            tnode = nodes.get(t, {})
            defined = tnode.get("defined")
            mark = "" if defined else " ⚠CREATE TABLE 정의를 코드에서 못 찾음"
            lines.append(f"- `{sid}` → `{t[len('table:'):] if t.startswith('table:') else t}`{mark}")
    for sid in seeds:
        if not sid.startswith("table:"):
            continue
        users = sorted(set(users_by_table.get(sid, [])))
        lines.append(f"- `{sid[len('table:'):]}` 를 쓰는 모듈 {len(users)}개: " + ", ".join(f"`{u}`" for u in users[:6]))
    if lines:
        sections.append((PRI_TABLES, "## 이 모듈이 쓰는 테이블", _dedup(lines)))

    # ── ⚠ 이 범위에 이미 걸린 결함 ──
    lines = []
    for f in relevant_findings(graph.get("findings", []), seed_set, related, seed_ns, seed_tables, nodes, deadline):
        sev = f.get("severity", "?")
        detail = str(f.get("detail", "")).replace("\n", " ")
        if len(detail) > 220:
            detail = detail[:217] + "…"
        lines.append(f"- [{sev}] {f.get('rule')} `{f.get('key', '')}` — {detail}")
    if lines:
        sections.append((PRI_FINDINGS, "## ⚠ 이 범위에 이미 걸린 결함", lines))

    # ── 판정 불가 ──
    lines = []
    for u in graph.get("unresolved", []):
        if not isinstance(u, dict):
            continue
        mod = u.get("module", "")
        if mod not in seed_set and mod not in related:
            continue
        lines.append(f"- {u.get('kind')} `{mod}`:{u.get('lineno')} — {u.get('detail', '')} (여기는 그래프가 모른다)")
        if len(lines) >= MAX_LINES_PER_SECTION:
            break
    if lines:
        sections.append((PRI_UNRESOLVED, "## 판정 불가(UNRESOLVED)", lines))

    return sections


def relevant_findings(
    findings: list,
    seeds: set[str],
    related: set[str],
    seed_ns: set[str],
    seed_tables: set[str],
    nodes: dict[str, dict],
    deadline: Deadline,
) -> list[dict]:
    ns_paths = {nodes.get(n, {}).get("path", n) for n in seed_ns}
    table_names = {t[len("table:"):] for t in seed_tables if t.startswith("table:")}
    seed_basenames = {s.rsplit("/", 1)[-1] for s in seeds}

    picked: list[tuple[int, int, dict]] = []
    for f in findings:
        if not isinstance(f, dict) or deadline.expired():
            continue
        refs: set[str] = set()
        # entrypoint 는 일부러 안 본다. app/main.py 는 84개 라우터를 전부 mount 하는
        # 허브라서, 이것을 근거로 관련을 판정하면 DOUBLE_MOUNT 9건이 전부 딸려온다
        # (2026-09-18 실측: 관련 결함 2건이 13건으로 불어났다).
        for key in ("file", "module"):
            if f.get(key):
                refs.add(str(f[key]))
        for key in ("modules", "owners", "users"):
            val = f.get(key)
            if isinstance(val, list):
                refs |= {str(v) for v in val}

        direct = bool(refs & seeds)
        hit = direct or bool(refs & related)
        if not hit and f.get("namespace") in ns_paths:
            hit = direct = True
        if not hit and f.get("table") in table_names:
            hit = True
        if not hit and f.get("key") in seed_basenames:
            hit = direct = True
        if not hit and f.get("path"):
            path = str(f["path"])
            if any(nsp and path.startswith(nsp.rstrip("/")) for nsp in ns_paths if nsp):
                hit = True
        if not hit:
            continue
        picked.append((SEVERITY_ORDER.get(str(f.get("severity")), 9), 0 if direct else 1, f))

    picked.sort(key=lambda x: (x[1], x[0]))
    return [f for _, _, f in picked[:MAX_LINES_PER_SECTION]]


# ── 렌더링 ─────────────────────────────────────────────────────────────


def render(
    graph: dict[str, Any],
    sections: list[tuple[int, str, list[str]]],
    max_bytes: int,
    partial: bool,
    project: str,
) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    header = (
        f"[AAG 착수 브리프 — {project}]\n"
        f"생성 {now} · 그래프 {graph.get('generated_at', '?')} · "
        f"노드 {len(graph.get('nodes', []))} / 엣지 {len(graph.get('edges', []))} 중 관련분만\n"
    )
    fixed = _blen(header) + _blen("\n" + FOOTER + "\n") + _blen("\n" + TRUNCATED_MARK + "\n")
    budget = max(0, max_bytes - fixed)

    kept: dict[int, list[str]] = {}
    truncated = partial
    for pri, title, lines in sorted(sections, key=lambda s: s[0]):
        cost = _blen("\n" + title + "\n")
        if cost > budget:
            truncated = True
            continue
        budget -= cost
        section_cap = budget if len(kept) + 1 >= len(sections) else int(budget * SECTION_BUDGET_RATIO)
        spent = 0
        taken: list[str] = []
        for line in lines[:MAX_LINES_PER_SECTION]:
            lcost = _blen(line + "\n")
            if lcost > budget or spent + lcost > section_cap:
                truncated = True
                break
            budget -= lcost
            spent += lcost
            taken.append(line)
        if len(taken) < len(lines):
            truncated = True
        if taken:
            kept[pri] = taken
        else:
            budget += cost  # 제목만 남기지 않는다

    out = [header]
    for pri, title, _ in sorted(sections, key=lambda s: s[0]):
        if pri in kept:
            out.append("\n" + title + "\n" + "\n".join(kept[pri]) + "\n")
    # 화면 순서: ⚠결함을 먼저 읽히게 우선순위 순서 그대로 둔다.
    if truncated:
        out.append("\n" + TRUNCATED_MARK + "\n")
    out.append("\n" + FOOTER + "\n")
    return "".join(out)


def _blen(s: str) -> int:
    return len(s.encode("utf-8"))


# ── main ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AAG 착수 브리프 생성기")
    ap.add_argument("--instruction-file", required=True)
    ap.add_argument("--graph", default=None)
    ap.add_argument("--project", default="AADS")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--hops", type=int, default=1)
    args = ap.parse_args(argv)

    deadline = Deadline(TIME_BUDGET_SEC)
    hops = max(1, min(2, args.hops))

    try:
        instruction = Path(args.instruction_file).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[aag-brief] 지시서를 읽을 수 없다: {exc}", file=sys.stderr)
        return 0
    if not instruction.strip():
        print("[aag-brief] 지시서가 비었다", file=sys.stderr)
        return 0

    graph_path, why = resolve_graph_path(args.graph, args.project)
    if graph_path is None:
        print(f"[aag-brief] {why}", file=sys.stderr)
        return 0

    graph, why = load_graph(graph_path)
    if graph is None:
        print(f"[aag-brief] {why}", file=sys.stderr)
        return 0

    nodes = {n["id"]: n for n in graph.get("nodes", []) if isinstance(n, dict) and n.get("id")}
    edges = [e for e in graph.get("edges", []) if isinstance(e, dict)]

    tokens = extract_tokens(instruction)
    seeds = match_nodes(tokens, nodes, deadline)
    if not seeds:
        print("[aag-brief] 지시서 키워드가 그래프의 어떤 노드와도 일치하지 않는다", file=sys.stderr)
        return 0

    related = expand(seeds, edges, hops, deadline) - hub_nodes(nodes, edges, seeds)
    sections = build_sections(graph, seeds, related, deadline)
    if not sections:
        print("[aag-brief] 관련 사실 0건", file=sys.stderr)
        return 0

    text = render(graph, sections, max(500, args.max_bytes), partial=deadline.hit, project=args.project)
    sys.stdout.write(text)
    if deadline.hit:
        print("[aag-brief] 시간 상한(15초) 초과 — 부분 결과만 출력했다", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:  # 브리프 실패가 본 작업을 막아서는 안 된다
        print(f"[aag-brief] 예외로 중단: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        sys.exit(0)
