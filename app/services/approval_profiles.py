"""Project-specific display metadata for goal approval settings.

These helpers describe existing settings; they do not grant execution rights.
"""
from typing import Literal

ApprovalProfile = Literal["trading", "general"]


def profile_for_project(project: str | None) -> ApprovalProfile:
    return "trading" if (project or "").strip().upper() in {"GO100", "KIS"} else "general"


def profile_options(profile: str) -> dict[str, dict[str, str]]:
    """Return fresh checkbox metadata keyed by the unchanged policy fields.

    risk_tone is semantic (warning/danger), independent of dashboard colors.
    Unknown profiles use the general wording.
    """
    if profile == "trading":
        return {
            "auto_approve_high": {
                "label": "실매매 코드 수정 미리 승인",
                "description": "실매매 코드 수정을 이 목표 달성까지 미리 승인합니다. 허용 횟수를 넘으면 다시 묻습니다.",
                "risk_tone": "warning",
            },
            "auto_approve_critical": {
                "label": "주문·자금까지 미리 승인",
                "description": "주문·자금·서비스 재기동까지 미리 승인 (돈이 직접 걸립니다). 허용 횟수를 넘으면 다시 묻습니다.",
                "risk_tone": "danger",
            },
        }
    explanation = (
        "코드·도구 권한을 새로 주는 것이 아니라 다음 단계 제안 카드 승인을 생략하는 설정입니다. "
        "같은 프로젝트의 목표에 연결된 세션에서 허용 횟수가 남아 있을 때 적용되며, "
        "허용 횟수를 넘으면 다시 묻습니다."
    )
    return {
        "auto_approve_high": {
            "label": "다음 단계 자동 진행(고위험 제외)",
            "description": "critical을 제외한 다음 단계 제안에 적용됩니다. " + explanation,
            "risk_tone": "warning",
        },
        "auto_approve_critical": {
            "label": "고위험 단계까지 자동 진행",
            "description": "critical 단계 제안에 적용됩니다. 그 외 단계는 위의 다음 단계 자동 진행 설정을 따릅니다. " + explanation,
            "risk_tone": "danger",
        },
    }
