"""Task policy compiler — 실행 전 "기존 구현 보존" 정책을 체크리스트로 컴파일한다.

이 모듈은 새 규칙을 만들지 않는다. 이미 운영 중인 세 곳의 게이트를 하나의
dict/체크리스트로 모아서 목표·러너·에이전트가 같은 문구로 자기감사하게 한다.

재사용 원본:
- `app/services/pipeline_runner_service.py` `_VERIFICATION_CHECKLIST_TEMPLATE`
  → STEP 0 기존 구현 조사 문구, 검증 체크리스트 문구 (문구 동일, 동기화는
    `tests/unit/test_task_policy_compiler.py`가 강제한다)
- `app/services/code_reviewer.py` `_precheck_preservation_gate`
  → 보존 하드 게이트(삭제 심볼·삭제 라인 비율·범위 밖 파일) 규칙
- `app/services/ohvis_harness.py` `RISK_POLICIES`
  → risk_tier별 allow/approve/respond/reject 결정
"""
from __future__ import annotations

from typing import Any, Optional

from app.services.ohvis_harness import RISK_POLICIES

# ─── STEP 0: 기존 구현 조사 (러너 지시서와 동일 문구) ─────────────────────────
PRESERVATION_SURVEY_STEPS: tuple[str, ...] = (
    "대상 파일의 기존 함수/클래스/엔드포인트/스케줄러/DB 접점 목록을 먼저 확인",
    "각 항목을 [유지 | 수정 | 신규 | 삭제(사유 필수)]로 분류",
    "기존 구현을 새 구현으로 통째 대체하지 않고 필요한 개선분만 반영",
    "삭제가 필요하면 삭제 대상, 호출처 영향, 롤백 방법을 RESULT에 명시",
    "지시서에 명시되지 않은 파일을 변경해야 하면 변경 전 사유를 RESULT에 명시",
)

PRESERVATION_CLASSES: tuple[str, ...] = ("유지", "수정", "신규", "삭제(사유 필수)")

# ─── 통째 대체 금지 (code_reviewer 보존 하드 게이트와 동일 판정 기준) ────────
NO_BROAD_REPLACEMENT_RULES: tuple[str, ...] = (
    "기존 함수/클래스/API 라우터 삭제가 있으면 preservation 0.2 이하, FLAG로 판정",
    "삭제 라인이 추가 라인의 50%를 초과하면 preservation 0.3 이하, REQUEST_CHANGES 이상으로 차단",
    "지시서에 명시된 파일 경로 밖 변경이 있으면 scope_compliance 0.3 이하로 판정",
    "기능이 동작해 보여도 기존 구현 조사·분류표와 삭제 사유가 없으면 승인 금지",
)

# ─── 대상 검증 (러너 검증 체크리스트와 동일 문구) ────────────────────────────
TARGET_VALIDATION_STEPS: tuple[str, ...] = (
    "구현 목표: (무엇을 구현했는지 1줄 요약)",
    "검증 방법: (curl 명령 또는 URL 또는 UI 셀렉터)",
    "완료 기준: (어떤 응답/결과가 나와야 완료인지)",
    "실패 기준: (이런 결과면 실패로 간주)",
    "서비스 재시작 확인: docker ps → container running",
    "에러 로그 0건: docker logs --since 60s | grep -i error",
)

# ─── 증거 요구사항 ───────────────────────────────────────────────────────────
BASE_EVIDENCE_REQUIREMENTS: tuple[str, ...] = (
    "변경한 Python 파일마다 python3 -m py_compile 통과 로그",
    "신규/변경 테스트의 pytest 실행 결과(통과 개수 포함)",
    "git diff --check 결과",
    "DB 접점이 있으면 before/after 카운트",
    "ohvis_harness_traces에 실행 근거 trace 기록",
)

_RISK_EVIDENCE: dict[str, tuple[str, ...]] = {
    "read": ("읽기 전용임을 보여주는 쿼리/명령 원문",),
    "write": ("변경 전 원본 값 또는 백업 경로", "롤백에 필요한 직전 커밋 SHA"),
    "deploy": (
        "릴리스 SHA당 단일 이미지 빌드와 슬롯 동일 digest 증거",
        "candidate health → cutover → routed health 순서 로그",
        "5분 P0/P1 모니터링 무에러 기록",
    ),
    "auth": (
        "captcha/otp 우회 시도 없음 증거",
        "동일 work_key 재개 여부",
        "자격증명이 로그/커밋에 남지 않았다는 확인",
    ),
    "financial": (
        "read-only 조회 선행 결과",
        "주문/이체 게이트 승인 기록",
    ),
    "destructive": (
        "CEO 승인 원문",
        "삭제 대상 백업 위치와 복구 검증 결과",
    ),
}

# ─── 롤백 노트 ───────────────────────────────────────────────────────────────
BASE_ROLLBACK_NOTES: tuple[str, ...] = (
    "변경 파일 목록과 직전 커밋 SHA를 RESULT에 남겨 git revert 가능하게 유지",
    "삭제/대체가 있으면 원본 코드 블록과 복구 절차를 명시",
    "DB 변경은 idempotent하게 작성하고 역방향 쿼리를 함께 기록",
)

_RISK_ROLLBACK: dict[str, tuple[str, ...]] = {
    "deploy": (
        "routed health 실패 시 nginx 라우팅을 이전 슬롯으로 즉시 되돌린다",
        "이전 활성 슬롯은 스트림 드레인 전까지 재빌드/재시작하지 않는다",
    ),
    "auth": ("실패 시 세션/자격증명 캐시를 초기화하고 수동 재인증 경로를 남긴다",),
    "financial": ("주문 실패 시 후속 주문을 중단하고 잔고/체결 상태를 재조회한다",),
    "destructive": ("사전 백업에서 복구하는 절차를 실행 전에 검증한다",),
}

# ─── intent / 제목 → risk_tier 추론 ──────────────────────────────────────────
_INTENT_RISK: dict[str, str] = {
    "read": "read",
    "audit": "read",
    "health": "read",
    "analysis": "read",
    "task_query": "read",
    "docs": "write",
    "write": "write",
    "code": "write",
    "recovery": "write",
    "pipeline": "write",
    "ops": "write",
    "deploy": "deploy",
    "release": "deploy",
    "auth": "auth",
    "browser_collection": "auth",
    "pc_agent": "auth",
    "finance": "financial",
    "trading": "financial",
    "order": "financial",
    "destructive": "destructive",
}

_TITLE_RISK_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("destructive", ("drop table", "truncate", "force push", "전체 삭제", "초기화")),
    ("financial", ("주문", "체결", "이체", "결제", "매매", "order", "payment")),
    ("auth", ("로그인", "인증", "captcha", "otp", "자격증명", "credential", "login")),
    ("deploy", ("배포", "릴리스", "릴리즈", "deploy", "release", "cutover", "blue/green", "bluegreen")),
    ("write", ("수정", "개선", "구현", "보강", "리팩터", "patch", "fix", "implement")),
)

# 낮음 → 높음. 추론 결과 중 가장 높은 등급을 채택한다.
_RISK_ORDER: tuple[str, ...] = ("read", "write", "deploy", "auth", "financial", "destructive")


def _risk_rank(tier: str) -> int:
    try:
        return _RISK_ORDER.index(tier)
    except ValueError:
        return 0


def normalize_risk_tier(risk_tier: Optional[str]) -> Optional[str]:
    """알려진 risk_tier면 소문자로 정규화, 아니면 None."""
    if not risk_tier:
        return None
    normalized = str(risk_tier).strip().lower()
    return normalized if normalized in RISK_POLICIES else None


def infer_risk_tier(
    intent: Optional[str] = None,
    goal_title: Optional[str] = None,
) -> tuple[str, str]:
    """intent/제목에서 risk_tier를 추론한다. (tier, source)를 돌려준다."""
    tier = "read"
    source = "default"

    intent_key = str(intent or "").strip().lower()
    intent_tier = _INTENT_RISK.get(intent_key)
    if intent_tier and _risk_rank(intent_tier) > _risk_rank(tier):
        tier, source = intent_tier, "intent"

    haystack = str(goal_title or "").lower()
    if haystack:
        for candidate, terms in _TITLE_RISK_TERMS:
            if any(term in haystack for term in terms) and _risk_rank(candidate) > _risk_rank(tier):
                tier, source = candidate, "goal_title"
    return tier, source


def compile_task_policy(
    project: Optional[str] = None,
    intent: Optional[str] = None,
    risk_tier: Optional[str] = None,
    goal_title: Optional[str] = None,
) -> dict[str, Any]:
    """project/intent/risk_tier/goal_title로 보존 정책 체크리스트를 만든다.

    반환 dict는 그대로 프롬프트에 주입하거나 harness trace metadata로 저장할 수
    있도록 JSON 직렬화 가능한 값만 담는다.
    """
    explicit = normalize_risk_tier(risk_tier)
    if explicit:
        tier, tier_source = explicit, "explicit"
    else:
        tier, tier_source = infer_risk_tier(intent, goal_title)

    policy = RISK_POLICIES.get(tier, RISK_POLICIES["read"])
    evidence = list(BASE_EVIDENCE_REQUIREMENTS) + list(_RISK_EVIDENCE.get(tier, ()))
    rollback = list(BASE_ROLLBACK_NOTES) + list(_RISK_ROLLBACK.get(tier, ()))

    checklist: list[dict[str, str]] = []
    for section, items in (
        ("existing_implementation_survey", PRESERVATION_SURVEY_STEPS),
        ("no_broad_replacement", NO_BROAD_REPLACEMENT_RULES),
        ("target_validation", TARGET_VALIDATION_STEPS),
        ("evidence_requirements", evidence),
        ("rollback_notes", rollback),
    ):
        for index, requirement in enumerate(items, start=1):
            checklist.append({
                "id": f"{section}.{index}",
                "section": section,
                "requirement": requirement,
            })

    return {
        "project": (project or "").upper() or None,
        "intent": intent or None,
        "goal_title": goal_title or None,
        "risk_tier": tier,
        "risk_tier_source": tier_source,
        "risk_policy": dict(policy),
        "decision": policy.get("decision", "allow"),
        "approval_required": bool(policy.get("approval_required", False)),
        "existing_implementation_survey": list(PRESERVATION_SURVEY_STEPS),
        "preservation_classification": list(PRESERVATION_CLASSES),
        "no_broad_replacement": list(NO_BROAD_REPLACEMENT_RULES),
        "target_validation": list(TARGET_VALIDATION_STEPS),
        "evidence_requirements": evidence,
        "rollback_notes": rollback,
        "checklist": checklist,
        "deletion_allowed": False,
        "summary": policy_summary(tier, tier_source, project, goal_title, len(checklist)),
    }


def policy_summary(
    risk_tier: str,
    risk_tier_source: str,
    project: Optional[str],
    goal_title: Optional[str],
    checklist_size: int,
) -> str:
    """trace input_summary로 쓰기 좋은 1줄 요약."""
    scope = (project or "-") + ("/" + goal_title if goal_title else "")
    return (
        f"policy risk={risk_tier}({risk_tier_source}) scope={scope[:120]} "
        f"checks={checklist_size} deletion=not_allowed"
    )


def render_policy_markdown(policy: dict[str, Any]) -> str:
    """컴파일된 정책을 지시서/프롬프트에 붙일 마크다운으로 렌더링한다."""
    lines: list[str] = [
        "## 기존 구현 보존 정책",
        f"- project: {policy.get('project') or '-'}",
        f"- intent: {policy.get('intent') or '-'}",
        f"- risk_tier: {policy.get('risk_tier')} ({policy.get('risk_tier_source')}) "
        f"→ decision={policy.get('decision')}, approval_required={policy.get('approval_required')}",
        f"- 분류 라벨: [{' | '.join(policy.get('preservation_classification', []))}]",
        "",
        "### STEP 0 기존 구현 조사 (코드 수정 전 필수)",
    ]
    for step in policy.get("existing_implementation_survey", []):
        lines.append(f"- [ ] {step}")

    for title, key in (
        ("### 통째 대체 금지 하드 게이트", "no_broad_replacement"),
        ("### 대상 검증", "target_validation"),
        ("### 증거 요구사항", "evidence_requirements"),
        ("### 롤백 노트", "rollback_notes"),
    ):
        lines.append("")
        lines.append(title)
        for item in policy.get(key, []):
            lines.append(f"- [ ] {item}")

    lines.append("")
    lines.append("삭제는 기본 금지다. 불가피하면 삭제 대상·호출처 영향·롤백 방법을 RESULT에 명시한다.")
    return "\n".join(lines)
