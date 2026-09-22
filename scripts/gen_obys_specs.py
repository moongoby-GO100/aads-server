"""오비서 V4 기획 정본화 — roadmap.md + 슬라이스별 spec/plan/tasks 생성.

CEO 승인 다음단계 1건: docs/specs/obys-v4/ 아래에 Spec Kit 'Spec of Specs' 패턴으로
슬라이스(=사이드바 하위메뉴가 가리키는 실제 상세화면) 목록을 ID·의도·범위경계·의존·상태
5열로 고정하고, 슬라이스마다 spec.md(무엇/왜)·plan.md(어떻게)·tasks.md(작업목록)를 만든다.

소스: app/static/apps/obys/mockup-v3.html의 submenuItems 실제 데이터(33개 고유 상세화면).
CEO는 "35개"라 했으나 mockup 실측 결과 고유 상세화면은 33개다 — 반올림 표현으로 보고
실측값을 그대로 쓴다(R-CRITICAL: 실측 우선, 추측 금지).
"""
import os

MENUS = {
    "home": ("통합 홈", [["오늘 할 일", "task"], ["현금흐름", "account-detail"], ["매출·정산", "sales-detail"], ["세무 일정", "calendar"]]),
    "tasks": ("할 일·알림", [["긴급", "task"], ["오늘", "review"], ["예정", "calendar"], ["완료", "approval-detail"]]),
    "reports": ("경영 리포트", [["손익", "review"], ["현금흐름", "account-detail"], ["채널·지점", "sales-detail"], ["보고서 생성", "report"]]),
    "documents": ("경영자료·보관", [["경영보고서", "document-vault"], ["세무문서", "tax-return-preview"], ["계약·인사", "employee-docs"], ["증빙 원본보관", "receipt-upload"]]),
    "sales": ("매출·정산", [["매출 현황", "sales-detail"], ["거래 내역", "order-detail"], ["정산 대사", "review"], ["매출자료 등록", "sales-channel-connect"]]),
    "accounts": ("입금·계좌", [["계좌 현황", "account-detail"], ["입금 예정", "order-detail"], ["미매칭", "match"], ["입출금자료 등록", "bank-connect"]]),
    "employees": ("직원·급여", [["직원 현황", "employee-detail"], ["근태", "signature-progress"], ["급여", "approve"], ["계약·증빙", "employee-docs"]]),
    "inventory": ("재고·발주", [["재고 현황", "menu-detail"], ["발주 추천", "order-detail"], ["매입처 현황", "supplier-connect"], ["매입자료 등록", "receipt-upload"]]),
    "tax": ("세무·회계", [["증빙 대장", "vat-ledger"], ["증빙 등록·누락", "evidence-gap"], ["전표관리", "withholding-ledger"], ["신고·납부", "tax-return-preview"]]),
    "approvals": ("통합 승인함", [["승인 대기", "approval-detail"], ["완료", "audit-detail"], ["반려", "review"], ["정책", "tax-agent-approval"]]),
    "alerts": ("알림센터", [["중요", "notification"], ["업무", "task"], ["시스템", "integration-audit"], ["알림 설정", "credential-vault"]]),
    "business": ("사업자·지점", [["사업자", "business-detail"], ["지점", "business-ocr"], ["전환 이력", "audit-detail"], ["신규 등록", "business-switch"]]),
    "integrations": ("연동 관리", [["판매 자동연동", "sales-channel-connect"], ["은행 자동연동", "bank-connect"], ["매입 자동연동", "supplier-connect"], ["세무 자동연동", "tax-connect"], ["권한·오류", "integration-audit"]]),
    "margins": ("원가·마진", [["메뉴 원가", "menu-detail"], ["원가 변동", "review"], ["마진", "margin-detail"], ["시뮬레이션", "order-detail"]]),
    "audit": ("권한·감사로그", [["계정 권한", "audit-detail"], ["변경 이력", "credential-vault"], ["로그인·연동", "integration-audit"], ["정책", "business-switch"]]),
}

# PRD-OBYS-MOCKUP-V3-DETAIL-PAGES.md 6절 "메뉴 소유권과 중복 정리" 표 — 정본 소유 메뉴.
CANONICAL_OWNER = {
    "sales": "매출관리 — 매출·주문·카드 승인/취소·정산 대사",
    "accounts": "계좌·입출금 — 잔액·입출금·입금예정·미매칭",
    "inventory": "재고·매입 — 매입처·발주·입고·매입자료 등록",
    "tax": "세무·회계 — 증빙대장·증빙등록·전표관리·신고납부",
    "integrations": "외부연동 — 인증·수집주기·권한·오류 복구",
    "documents": "문서보관함 — 확정 문서와 원본의 보관·권한·이력",
}

# detail slug -> [(view_slug, label), ...]
detail_refs: dict[str, list[tuple[str, str]]] = {}
for view_slug, (view_label, items) in MENUS.items():
    for label, detail in items:
        detail_refs.setdefault(detail, []).append((view_slug, label))

rows = []
for detail in sorted(detail_refs):
    refs = detail_refs[detail]
    ref_views = sorted({v for v, _ in refs})
    canonical_views = [v for v in ref_views if v in CANONICAL_OWNER]
    if len(canonical_views) == 1:
        owner = canonical_views[0]
        status = "owner_resolved"
    elif len(ref_views) == 1:
        owner = ref_views[0]
        status = "single_menu"
    elif len(canonical_views) > 1:
        owner = "|".join(canonical_views)
        status = "conflict_multi_canonical"
    else:
        owner = "|".join(ref_views)
        status = "conflict_needs_review"
    rows.append({
        "id": f"obys-v4-{detail}",
        "detail": detail,
        "labels": sorted({label for _, label in refs}),
        "ref_views": ref_views,
        "owner": owner,
        "status": status,
    })

os.makedirs("docs/specs/obys-v4", exist_ok=True)

STATUS_KR = {
    "owner_resolved": "정본 확정(PRD 6절 소유 메뉴와 일치)",
    "single_menu": "단일 메뉴 참조(충돌 없음)",
    "conflict_multi_canonical": "충돌 — 2개 이상 정본 메뉴가 동일 화면 참조, 소유권 재결정 필요",
    "conflict_needs_review": "미정 — 정본 소유 메뉴 없이 여러 메뉴가 참조, 점검 필요",
}

lines = []
lines.append("# 오비서 V4 기획 로드맵 — Spec of Specs\n")
lines.append("")
lines.append("PRD 원본: `docs/PRD-OBYS-MOCKUP-V3-DETAIL-PAGES.md`. 소스 실측: "
              "`app/static/apps/obys/mockup-v3.html`의 `submenuItems`(14개 상위 메뉴 · "
              f"{sum(len(v[1]) for v in MENUS.values())}개 하위 항목 · 고유 상세화면 {len(rows)}개).")
lines.append("")
lines.append("CEO 지시는 \"35개 메뉴\"였으나 실측 결과 고유 상세화면은 "
             f"**{len(rows)}개**다. 추측으로 2개를 채우지 않고 실측값을 그대로 쓴다"
             "(R-CRITICAL). 하위 항목 총량(61개) 기준으로는 35에 더 가깝게 보일 수 있어 "
             "차이를 여기 명시한다.")
lines.append("")
lines.append("각 슬라이스는 `docs/specs/obys-v4/<slug>/{spec,plan,tasks}.md` 3산출물로 고정된다. "
              "추가지시는 이 3파일을 갱신하는 형태로만 반영하고, 대화에만 흩어져 남기지 않는다.")
lines.append("")
lines.append("## 슬라이스 목록")
lines.append("")
lines.append("| ID | 의도(레이블) | 범위경계(소유) | 의존(참조 메뉴) | 상태 |")
lines.append("|---|---|---|---|---|")
def describe_owner(owner: str) -> str:
    if owner in CANONICAL_OWNER:
        return CANONICAL_OWNER[owner]
    if owner in MENUS:
        return MENUS[owner][0]
    return " / ".join(MENUS[v][0] for v in owner.split("|") if v in MENUS)


for r in rows:
    intent = " / ".join(r["labels"])
    owner_desc = describe_owner(r["owner"])
    deps = ", ".join(MENUS[v][0] for v in r["ref_views"])
    lines.append(f"| `{r['id']}` | {intent} | {owner_desc} | {deps} | {STATUS_KR[r['status']]} |")

lines.append("")
lines.append("## 충돌/점검 필요 슬라이스")
lines.append("")
conflicts = [r for r in rows if r["status"].startswith("conflict")]
if conflicts:
    for r in conflicts:
        deps = ", ".join(MENUS[v][0] for v in r["ref_views"])
        lines.append(f"- `{r['id']}`: {deps} 가 동시 참조 — {STATUS_KR[r['status']]}")
else:
    lines.append("- 없음")
lines.append("")
lines.append("## 다음 게이트")
lines.append("")
lines.append("1. analyze: 슬라이스 간 범위경계 재확인(특히 위 충돌 목록)")
lines.append("2. converge: CEO 승인 후 상태를 owner_resolved로 고정, 이후 구현 착수")
lines.append("")

with open("docs/specs/obys-v4/roadmap.md", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

SPEC_TMPL = """# spec: {id}

## 의도(무엇/왜)
"{intent}" 화면. 오비서 사이드바에서 {deps} 메뉴가 이 화면을 참조한다.

## 범위경계
- 소유: {owner_desc}
- 상태: {status_kr}
{conflict_note}
## 사용자 흐름
1. 사이드바에서 하위 메뉴 클릭 → 이 상세화면 진입
2. 목록/현황 조회 → 항목 선택 → 상세 확인
3. (해당 시) 등록·수정·승인 액션 수행

## 데이터 계약
- 입력: PRD 7절(자동연동 실패 시 자료 등록) 대상 화면이면 업로드 세션·컬럼 매핑·검증 결과·임시저장·승인 API 단계 분리
- 출력: 목록/현황 조회 결과, 상세 항목, 액션 결과

## 완료 기준
- 화면이 소유 메뉴 1곳에서만 원본으로 제공되고 다른 메뉴는 링크로만 연결
- 데이터 계약이 실제 API와 1:1 대응(시안 단계에서도 계약 명시)
"""

PLAN_TMPL = """# plan: {id}

## 기술 계약(어떻게)
- 프론트: `app/static/apps/obys/mockup-v3.html` 시안 기준, 실제 구현 시 별도 라우트/컴포넌트로 분리
- 백엔드: 소유 메뉴({owner_desc})의 서비스 계층에서 단일 진실 소스 제공, 참조 메뉴는 링크만
- 중복 방지: 동일 데이터를 참조 메뉴가 재조회하지 않고 소유 메뉴 API를 호출

## 단계
1. 데이터 모델/쿼리 확정(소유 메뉴 기준)
2. API 계약 확정(요청/응답 스키마)
3. 프론트 화면 연결(소유 메뉴 원본 + 참조 메뉴 링크)
4. 참조 무결성 테스트(같은 화면이 두 번 구현되지 않았는지)

## 리스크
{conflict_note_plan}
"""

TASKS_TMPL = """# tasks: {id}

- [ ] 데이터 모델/쿼리 확정
- [ ] API 계약 문서화(요청/응답 스키마)
- [ ] 소유 메뉴({owner_desc}) 화면 구현
- [ ] 참조 메뉴({deps})는 링크만 연결, 화면 복제 금지
- [ ] 완료 기준 검증(spec.md 참조)
{conflict_task}
"""

for r in rows:
    slug_dir = f"docs/specs/obys-v4/{r['detail']}"
    os.makedirs(slug_dir, exist_ok=True)
    intent = " / ".join(r["labels"])
    owner_desc = describe_owner(r["owner"])
    deps = ", ".join(MENUS[v][0] for v in r["ref_views"])
    status_kr = STATUS_KR[r["status"]]
    if r["status"].startswith("conflict"):
        conflict_note = f"- ⚠️ 충돌: {deps} 가 동시 참조한다. 정본 소유 메뉴를 CEO/PM이 확정해야 한다.\n"
        conflict_note_plan = f"- {deps} 가 동시 참조하는 충돌 슬라이스. 소유권 확정 전 구현 착수 금지."
        conflict_task = "- [ ] ⚠️ 소유권 충돌 해결(CEO/PM 결정 필요) — 확정 전 구현 착수 금지"
    else:
        conflict_note = ""
        conflict_note_plan = "- 없음(단일 소유 확인됨)"
        conflict_task = ""

    with open(f"{slug_dir}/spec.md", "w", encoding="utf-8") as f:
        f.write(SPEC_TMPL.format(id=r["id"], intent=intent, deps=deps, owner_desc=owner_desc,
                                  status_kr=status_kr, conflict_note=conflict_note))
    with open(f"{slug_dir}/plan.md", "w", encoding="utf-8") as f:
        f.write(PLAN_TMPL.format(id=r["id"], owner_desc=owner_desc, conflict_note_plan=conflict_note_plan))
    with open(f"{slug_dir}/tasks.md", "w", encoding="utf-8") as f:
        f.write(TASKS_TMPL.format(id=r["id"], owner_desc=owner_desc, deps=deps, conflict_task=conflict_task))

print(f"SLICES={len(rows)}")
print(f"CONFLICTS={len(conflicts)}")
print("FILES_WRITTEN", 1 + len(rows) * 3)
