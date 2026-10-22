"""AAG v2 스냅샷(nodes/edges JSON)에서 네임스페이스 단위 mermaid 텍스트를 만드는 순수 함수.

tools/aag/scan_aads.py render_mermaid() 의 규칙(네임스페이스 묶음, 상위 N개, 중복 마운트 강조)을
Scan 객체 없이 저장된 그래프만으로 재현한다. DB·네트워크 접근 없음.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

DEFAULT_MAX_NAMESPACES = 40
MAX_NAMESPACES_LIMIT = 80
MAX_CHARS = 60_000


def mermaid_id(raw: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9]", "_", raw)


def _label(raw: str) -> str:
    return raw.replace('"', "'")


def _edges_of(edges: Iterable[Mapping[str, Any]], kind: str) -> list[Mapping[str, Any]]:
    return [
        e for e in edges
        if isinstance(e, Mapping) and e.get("kind") == kind
        and isinstance(e.get("from"), str) and isinstance(e.get("to"), str)
    ]


def render_snapshot_mermaid(
    nodes: list[Any],
    edges: list[Any],
    findings: list[Any] | None = None,
    *,
    max_namespaces: int = DEFAULT_MAX_NAMESPACES,
    max_chars: int = MAX_CHARS,
) -> dict[str, Any]:
    """Return {mermaid, shown_namespaces, total_namespaces, truncated}.

    같은 입력이면 같은 텍스트가 나오도록 모든 순회를 정렬한다.
    """
    limit = max(1, min(int(max_namespaces), MAX_NAMESPACES_LIMIT))

    ns_owners: dict[str, set[str]] = {}
    for e in _edges_of(edges, "owns"):
        if e["to"].startswith("ns:"):
            ns_owners.setdefault(e["to"][3:], set()).add(e["from"])
    ordered = sorted(ns_owners.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    shown = ordered[:limit]

    dup_ns = {
        f["namespace"] for f in (findings or [])
        if isinstance(f, Mapping) and f.get("rule") == "DOUBLE_MOUNT"
        and isinstance(f.get("namespace"), str)
    }
    entrypoints = sorted(
        n["id"] for n in nodes
        if isinstance(n, Mapping) and n.get("kind") == "entrypoint" and isinstance(n.get("id"), str)
    )

    lines = ["graph LR", "  classDef dbl fill:#ffd7d7,stroke:#c00,stroke-width:2px;"]
    for entry in entrypoints:
        lines.append(f'  {mermaid_id(entry)}["{_label(entry)}"]')

    declared_owners: set[str] = set()
    for ns, owners in shown:
        nid = mermaid_id("ns" + ns)
        marker = " ⚠2+" if ns in dup_ns else ""
        lines.append(f'  {nid}(["{_label(ns)}{marker}"])')
        for owner in sorted(owners):
            oid = mermaid_id(owner)
            if owner not in declared_owners:
                declared_owners.add(owner)
                lines.append(f'  {oid}["{_label(owner)}"]')
            lines.append(f"  {oid} --> {nid}")
        if ns in dup_ns:
            lines.append(f"  class {nid} dbl;")

    shown_modules = {m for _ns, owners in shown for m in owners}
    mounts = sorted({(e["from"], e["to"]) for e in _edges_of(edges, "mounts")})
    for src, dst in mounts:
        if dst in shown_modules:
            lines.append(f"  {mermaid_id(src)} --> {mermaid_id(dst)}")

    shown_ns = {ns for ns, _ in shown}
    fe_calls: dict[tuple[str, str], int] = {}
    for e in _edges_of(edges, "calls"):
        if e["to"].startswith("ns:") and e["to"][3:] in shown_ns:
            key = (e["from"], e["to"][3:])
            fe_calls[key] = fe_calls.get(key, 0) + 1
    declared_fe: set[str] = set()
    for (fe, ns), n in sorted(fe_calls.items()):
        if fe not in declared_fe:
            declared_fe.add(fe)
            lines.append(f'  {mermaid_id(fe)}[/"{_label(fe)}"/]')
        lines.append(f"  {mermaid_id(fe)} -. {n}건 .-> {mermaid_id('ns' + ns)}")

    if len(ordered) > limit:
        lines.append(f"  %% 네임스페이스 {len(ordered)}개 중 상위 {limit}개만 표시")

    text = "\n".join(lines)
    truncated = len(text) > max_chars
    if truncated:
        note = f"\n%% 출력 {max_chars}자 상한으로 잘림"
        cut = text[: max(0, max_chars - len(note))]
        cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
        text = cut + note

    return {
        "mermaid": text,
        "shown_namespaces": len(shown),
        "total_namespaces": len(ordered),
        "truncated": truncated,
    }
