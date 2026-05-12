"""
Final response contract for chat-driven code/workspace changes.

Prompt instructions are useful, but they are not a hard guarantee. This module
checks the workspace change ledger immediately before an assistant response is
saved and appends a short correction when the response omits or overstates the
commit/push/deploy state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


_PENDING_STATUSES = ("dirty", "committed")
_UNDEPLOYED_STATUSES = ("dirty", "committed", "pushed")

_WORK_DONE_MARKERS = (
    "수정",
    "패치",
    "조치",
    "적용",
    "반영",
    "배포",
    "커밋",
    "푸시",
    "문서",
    "handover",
    "commit",
    "push",
    "deploy",
)

_DISCLOSURE_MARKERS = (
    "커밋/푸시",
    "커밋·푸시",
    "커밋은 아직",
    "푸시는 아직",
    "커밋 전",
    "푸시 전",
    "미커밋",
    "미푸시",
    "pending",
    "작업트리",
    "git status",
)

_CODE_INTENTS = {
    "code_modify",
    "deploy",
    "git_operation",
    "pipeline_runner",
    "cto_code_analysis",
    "cto_verify",
    "runner_response",
}

_COMMIT_DONE_RE = re.compile(
    r"(?:커밋|commit).{0,18}(?:완료|성공|했습니다|했음|됨|done|pushed)",
    re.IGNORECASE | re.DOTALL,
)
_PUSH_DONE_RE = re.compile(
    r"(?:푸시|push).{0,18}(?:완료|성공|했습니다|했음|됨|done)",
    re.IGNORECASE | re.DOTALL,
)
_DEPLOY_DONE_RE = re.compile(
    r"(?:배포|deploy).{0,18}(?:완료|성공|했습니다|했음|됨|done)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class CompletionContractResult:
    response_text: str
    adjusted: bool = False
    violation_types: list[str] = field(default_factory=list)
    pending_count: int = 0
    pending_files: list[str] = field(default_factory=list)
    note: str = ""

    def quality_details(self) -> dict[str, Any]:
        return {
            "completion_contract_adjusted": self.adjusted,
            "completion_contract_violations": self.violation_types,
            "pending_change_count": self.pending_count,
            "pending_files": self.pending_files,
        }


def _normalize_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        status = str(row.get("status") or "").strip()
        if status not in _UNDEPLOYED_STATUSES:
            continue
        normalized.append(
            {
                "project": str(row.get("project") or "").strip(),
                "repo": str(row.get("repo") or "").strip(),
                "file_path": str(row.get("file_path") or "").strip(),
                "status": status,
                "commit_sha": str(row.get("commit_sha") or "").strip(),
            }
        )
    return normalized


def _has_disclosure(response_text: str) -> bool:
    lowered = (response_text or "").lower()
    return any(marker.lower() in lowered for marker in _DISCLOSURE_MARKERS)


def _looks_like_work_completion(response_text: str, user_msg: str, intent: str) -> bool:
    text = f"{user_msg}\n{response_text}".lower()
    if (intent or "") in _CODE_INTENTS:
        return True
    return any(marker.lower() in text for marker in _WORK_DONE_MARKERS)


def _format_status(status: str) -> str:
    if status == "dirty":
        return "미커밋"
    if status == "committed":
        return "커밋됨/미푸시"
    if status == "pushed":
        return "푸시됨/미배포"
    return status or "unknown"


def _build_note(rows: list[dict[str, Any]], violations: list[str]) -> str:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["project"], row["repo"]), []).append(row)

    lines = ["", "", "⚠️ 완료 상태 보정"]
    if violations:
        lines.append(f"- 보정 사유: {', '.join(violations)}")
    lines.append("- 현재 workspace ledger 기준으로 아직 최종 완료되지 않은 변경이 있습니다.")

    shown = 0
    for (project, repo), group_rows in sorted(grouped.items()):
        lines.append(f"- {project or '-'} / {repo or '-'}")
        for row in group_rows[:5]:
            shown += 1
            lines.append(f"  - `{row['file_path']}`: {_format_status(row['status'])}")
        if len(group_rows) > 5:
            lines.append(f"  - 외 {len(group_rows) - 5}건")
    if shown == 0:
        lines.append("- pending 파일 목록을 불러오지 못했습니다.")
    lines.append("- 따라서 최종 완료 보고에는 커밋/푸시/문서기록/배포 상태를 별도로 확인해야 합니다.")
    return "\n".join(lines)


def evaluate_completion_contract(
    *,
    response_text: str,
    user_msg: str,
    intent: str,
    changes: Iterable[dict[str, Any]],
) -> CompletionContractResult:
    rows = _normalize_rows(changes)
    pending_rows = [row for row in rows if row["status"] in _PENDING_STATUSES]
    undeployed_rows = rows

    if not rows:
        return CompletionContractResult(response_text=response_text)

    violations: list[str] = []
    if pending_rows and _COMMIT_DONE_RE.search(response_text or ""):
        violations.append("commit_report_conflicts_with_ledger")
    if pending_rows and _PUSH_DONE_RE.search(response_text or ""):
        violations.append("push_report_conflicts_with_ledger")
    if undeployed_rows and _DEPLOY_DONE_RE.search(response_text or ""):
        violations.append("deploy_report_conflicts_with_ledger")
    if pending_rows and not _has_disclosure(response_text) and _looks_like_work_completion(response_text, user_msg, intent):
        violations.append("missing_commit_push_disclosure")

    if not violations:
        return CompletionContractResult(
            response_text=response_text,
            pending_count=len(rows),
            pending_files=[row["file_path"] for row in rows if row["file_path"]],
        )

    note = _build_note(rows, violations)
    base = (response_text or "").rstrip()
    adjusted = f"{base}{note}" if base else note.lstrip()
    return CompletionContractResult(
        response_text=adjusted,
        adjusted=True,
        violation_types=violations,
        pending_count=len(rows),
        pending_files=[row["file_path"] for row in rows if row["file_path"]],
        note=note,
    )


async def enforce_completion_contract(
    *,
    response_text: str,
    user_msg: str,
    session_id: str,
    intent: str,
) -> CompletionContractResult:
    if not session_id:
        return CompletionContractResult(response_text=response_text)

    from app.services.workspace_change_tracker import list_changes

    changes = await list_changes(
        session_id=session_id,
        statuses=_UNDEPLOYED_STATUSES,
    )
    return evaluate_completion_contract(
        response_text=response_text,
        user_msg=user_msg,
        intent=intent,
        changes=changes,
    )
