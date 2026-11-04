"""원천세 소득 구분별 규칙 (오비서 세금신고 P-11).

세율·기준은 이 파일의 상수에만 둔다. 공식 출처(국세청/법제처)를 확인하지 못한
구분은 verified=False 로 두고, 계산을 시도하면 WithholdingUnverified 를 낸다
(추정값으로 계산하지 않는다). 출처 목록과 확인 일자는 문서
docs/reports/20261022_AADS_오비서_세금신고_접수구분_원천세_회계권한_전표DB_결과.md 에 있다.

H2(근태 기반 급여 초안)는 4대보험·원천세를 계산하지 않는다. 급여명세의
tax_withholding 을 이 모듈로 채우는 연결 지점은 compute_withholding(income_kind, base)
하나이며, 호출하는 쪽은 소득 구분을 명시해야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.services import yeoljeong_wage_tax_table as wage_tax_table

BUSINESS_INCOME = "business_income"
WAGE_INCOME = "wage_income"
DAILY_WAGE_INCOME = "daily_wage_income"

# 국세청 원천징수 세율표: 원천징수 대상 사업소득 3%
#   https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=2291&cntntsId=7703
# 국세청 사업소득 원천징수방법(근거: 소득세법 제129조, 소득세법 시행령 제184조)
#   https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=6622&cntntsId=7902
BUSINESS_INCOME_TAX_RATE = Decimal("0.03")
# 지방세법 제103조의13: 원천징수하는 소득세의 100분의 10 을 개인지방소득세로 특별징수
#   https://www.law.go.kr/LSW//lsLawLinkInfo.do?lsJoLnkSeq=1000226093&lsId=001649&chrClsCd=010202&print=print
LOCAL_INCOME_TAX_RATIO = Decimal("0.10")

# 홈택스 근로소득 간이세액표(CEO 첨부 엑셀 2종, 데이터는 app/data/obys_wage_tax_table/)
WAGE_TABLE_2024_SOURCE = "홈택스 근로소득 간이세액표_2024.03.01.xlsx (적용 2024-03-01)"
WAGE_TABLE_2026_SOURCE = "홈택스 근로소득 간이세액표_2026.03.01.xlsx (적용 2026-03-01)"
WAGE_TABLE_ARTICLE = "소득세법 시행령 제189조 별표2"

NTS_RATE_PAGE = "https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=2291&cntntsId=7703"
NTS_BUSINESS_INCOME_PAGE = "https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=6622&cntntsId=7902"
LAW_LOCAL_TAX_103_13 = (
    "https://www.law.go.kr/LSW//lsLawLinkInfo.do?lsJoLnkSeq=1000226093&lsId=001649&chrClsCd=010202&print=print"
)
LAW_INCOME_TAX_129 = (
    "https://www.law.go.kr/LSW//lsLawLinkInfo.do?chrClsCd=010202&lsJoLnkSeq=1000821176&lsId=001565&print=print"
)


class WithholdingUnverified(Exception):
    """공식 출처로 확인되지 않은 소득 구분 — 계산하지 않는다."""

    def __init__(self, income_kind: str, reason: str):
        super().__init__(reason)
        self.income_kind = income_kind
        self.reason = reason


class WithholdingKindUnknown(ValueError):
    """알 수 없는 소득 구분."""


@dataclass(frozen=True)
class WithholdingRule:
    income_kind: str
    label: str
    verified: bool
    sources: tuple[str, ...]
    articles: tuple[str, ...]
    income_tax_rate: Decimal | None = None
    local_tax_ratio: Decimal | None = None
    unverified_reason: str = ""


WITHHOLDING_RULES: dict[str, WithholdingRule] = {
    BUSINESS_INCOME: WithholdingRule(
        income_kind=BUSINESS_INCOME,
        label="원천징수 대상 사업소득(인적용역 등) 3.3%",
        verified=True,
        sources=(NTS_RATE_PAGE, NTS_BUSINESS_INCOME_PAGE, LAW_INCOME_TAX_129, LAW_LOCAL_TAX_103_13),
        articles=("소득세법 제129조", "소득세법 시행령 제184조", "지방세법 제103조의13"),
        income_tax_rate=BUSINESS_INCOME_TAX_RATE,
        local_tax_ratio=LOCAL_INCOME_TAX_RATIO,
    ),
    WAGE_INCOME: WithholdingRule(
        income_kind=WAGE_INCOME,
        label="근로소득(상용) 간이세액표",
        verified=True,
        sources=(WAGE_TABLE_2024_SOURCE, WAGE_TABLE_2026_SOURCE, WAGE_TABLE_ARTICLE),
        articles=(WAGE_TABLE_ARTICLE, "소득세법 시행령 제194조", "지방세법 제103조의13"),
        local_tax_ratio=LOCAL_INCOME_TAX_RATIO,
    ),
    DAILY_WAGE_INCOME: WithholdingRule(
        income_kind=DAILY_WAGE_INCOME,
        label="일용근로소득",
        verified=False,
        sources=(NTS_RATE_PAGE,),
        articles=("소득세법 제129조 제1항 제2호",),
        unverified_reason=(
            "세율 6% 는 국세청 세율표에 있으나 일 공제액·세액공제 비율의 공식 조문을 확인하지 못했다 — 검증 필요"
        ),
    ),
}


def get_rule(income_kind: str | None) -> WithholdingRule:
    kind = str(income_kind or "").strip()
    rule = WITHHOLDING_RULES.get(kind)
    if rule is None:
        raise WithholdingKindUnknown(f"알 수 없는 소득 구분: {kind or '(없음)'}")
    return rule


def _compute_wage_income(
    rule: WithholdingRule,
    base_amount: Any,
    payroll_month: Any,
    dependents: int | None,
    children_8_20: int,
    withholding_ratio: Any,
) -> dict[str, Any]:
    base = Decimal(str(base_amount or 0))
    if base < 0:
        base = Decimal(0)
    common = {
        "income_kind": rule.income_kind,
        "label": rule.label,
        "base_amount": int(base),
        "articles": list(rule.articles),
        "sources": list(rule.sources),
    }
    if payroll_month in (None, ""):
        # 판(적용일)을 모르면 자녀 공제액과 세액표를 고를 수 없다 — 추정하지 않는다.
        return {
            **common,
            "status": "verification_required",
            "needs_input": True,
            "reason": "공제대상가족 수 미등록" if dependents in (None, 0) else "급여월 미지정",
            "income_tax": None,
            "local_income_tax": None,
            "total": None,
        }
    looked = wage_tax_table.lookup_monthly_wage_tax(
        payroll_month, base, dependents, children_8_20, withholding_ratio
    )
    if looked["income_tax"] is None:
        return {
            **common,
            "status": "verification_required",
            "needs_input": looked["needs_input"],
            "reason": looked["reason"],
            "table_version": looked["table_version"],
            "income_tax": None,
            "local_income_tax": None,
            "total": None,
        }
    return {
        **common,
        "income_tax": looked["income_tax"],
        "local_income_tax": looked["local_income_tax"],
        "total": looked["income_tax"] + looked["local_income_tax"],
        "needs_input": False,
        "table_version": looked["table_version"],
        "bracket": looked["bracket"],
        "basis_note": looked["basis_note"],
        "local_tax_ratio": str(rule.local_tax_ratio),
        "rounding_verified": False,
    }


def compute_withholding(
    income_kind: str | None,
    base_amount: Any,
    *,
    payroll_month: Any = None,
    dependents: int | None = None,
    children_8_20: int = 0,
    withholding_ratio: Any = 100,
) -> dict[str, Any]:
    """소득 구분별 원천세를 계산한다. 검증되지 않은 구분은 WithholdingUnverified.

    단수처리(원 미만 절사)는 공식 조문을 확인하지 못해 결과에 rounding_verified=False 로 표시한다.
    근로소득(WAGE_INCOME)은 base_amount 를 비과세·학자금 제외 월급여로 보고 간이세액표를 조회한다.
    payroll_month·dependents 가 없으면 세액 없이 needs_input=True 결과를 돌려준다(예외 아님).
    """
    rule = get_rule(income_kind)
    if rule.income_kind == WAGE_INCOME:
        return _compute_wage_income(
            rule, base_amount, payroll_month, dependents, children_8_20, withholding_ratio
        )
    if not rule.verified or rule.income_tax_rate is None or rule.local_tax_ratio is None:
        raise WithholdingUnverified(rule.income_kind, rule.unverified_reason or "검증 필요")
    base = Decimal(str(base_amount or 0))
    if base < 0:
        base = Decimal(0)
    income_tax = (base * rule.income_tax_rate).quantize(Decimal(1), rounding=ROUND_DOWN)
    local_tax = (income_tax * rule.local_tax_ratio).quantize(Decimal(1), rounding=ROUND_DOWN)
    return {
        "income_kind": rule.income_kind,
        "label": rule.label,
        "base_amount": int(base),
        "income_tax": int(income_tax),
        "local_income_tax": int(local_tax),
        "total": int(income_tax + local_tax),
        "income_tax_rate": str(rule.income_tax_rate),
        "local_tax_ratio": str(rule.local_tax_ratio),
        "articles": list(rule.articles),
        "sources": list(rule.sources),
        "rounding_verified": False,
    }
