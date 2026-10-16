"""봉인 필드에서 미리보기와 같은 v3/v4 조항을 생성한다 (DB/JS 런타임 불필요)."""
from __future__ import annotations

import math
import re
from typing import Any

CONTRACT_TITLES = {
    'part_time': '단시간/아르바이트 근로계약서', 'regular': '정규직 근로계약서',
    'manager': '매니저 근로계약서', 'fixed_term': '기간제 근로계약서',
    'daily': '일용 근로계약서', 'freelancer': '3.3% 프리랜서 용역계약서',
    'confidentiality': '보안/개인정보 서약서',
}


def value(contract: dict[str, Any], key: str) -> Any:
    """contractValue와 같은 null 병합 (camelCase 우선)."""
    camel = re.sub(r'_([a-z])', lambda m: m[1].upper(), key)
    found = contract.get(camel)
    return found if found is not None else contract.get(key) if contract.get(key) is not None else ''


def money(amount: Any) -> str:
    try:
        number = float(amount or 0)
    except (ValueError, TypeError):
        number = 0
    return f'{math.floor(number + 0.5) if math.isfinite(number) else 0:,}원'


def korean_date(date: Any) -> str:
    return re.sub(r'^(\d{4})-(\d{2})-(\d{2})$', r'\1년 \2월 \3일', str(date or ''))


def contract_clauses(contract: dict[str, Any]) -> list[tuple[str, str]]:
    """contractPreviewHtml 조항 순서·공백·버전 분기를 보존한다."""
    def v(key: str, fallback: str = '') -> Any:
        return value(contract, key) or fallback

    probation = v('probation_period')
    legacy_probation = v('probation_terms')
    if legacy_probation and probation in ('수습기간 없음.', '수습기간 없음. 수습을 정한 경우에도 입력 임금을 동일 적용한다.'):
        probation = ''  # generated editor defaults cannot negate a sealed agreement
    probation_text = '\n'.join(dict.fromkeys(str(x) for x in (probation, legacy_probation) if x))
    is_v3 = bool(re.search(r'-v3$', v('template_version')))
    kind = v('contract_type')
    employee, employer = v('employee_name'), v('employer_name')
    start, end = v('start_date'), v('end_date')
    part_note = f" 근로일별 근로시간: {v('daily_work_schedule', '미입력')}." if kind == 'part_time' else ''
    tax_label = {'four_insurance': '4대보험 가입 근로자', 'freelancer_33': '3.3% 프리랜서 원천징수'}.get(v('employment_tax_type'), v('employment_tax_label', '-'))
    wage_type = v('wage_type', 'hourly')
    wage_label = {'hourly': '시급', 'monthly': '월급', 'daily': '일급', 'case_fee': '건별 용역비'}.get(wage_type, wage_type)
    wage_line = f"{wage_label} {money(v('wage'))} / 지급일: {v('pay_date', '미입력')} / 지급방법: {v('pay_method', '직원 명의 계좌이체')}"
    overtime, leave = v('overtime_terms'), v('leave_terms')
    if not is_v3:
        five = v('workplace_size_category', 'under_5') in ('five_plus', 'over_5')
        size_terms = {
            'overtime_terms': (
                '상시 5인 미만 사업장: 근로기준법 제56조 가산 규정은 미적용하나 연장·야간·휴일의 실제 근로 임금은 지급하며 별도 유리한 약정을 준수한다.',
                '상시 5인 이상 사업장: 근로기준법 제56조에 따른 연장·야간·휴일근로 50% 이상 가산수당 등 법정 기준을 적용한다.',
                '연장·야간·휴일근로는 사전 승인 및 근태기록 기준으로 운영하고, 적용 대상 법정수당을 급여마감에 반영'),
            'leave_terms': (
                '상시 5인 미만 사업장: 근로기준법 제60조 연차유급휴가는 미적용하며 약정 휴가와 법정 적용 요건을 준수한다.',
                '상시 5인 이상 사업장: 근로기준법 제60조 연차유급휴가를 법정 요건에 따라 적용한다.',
                '연차유급휴가, 지각, 조퇴, 결근, 병가, 무단결근은 근태기록과 법정 기준에 따라 급여내역서에 반영'),
        }
        effective = []
        for key, text in [('overtime_terms', overtime), ('leave_terms', leave)]:
            standards = size_terms[key]
            wrong_prefix = '상시 5인 미만' if five else '상시 5인 이상'
            effective.append(standards[int(five)] if not text or text in standards or text.startswith(wrong_prefix) else text)
        overtime, leave = effective
    insurance = v('insurance_terms')
    if not insurance and not is_v3 and kind == 'daily':
        insurance = '고용·산재보험 일용근로 신고 및 근로내용 확인신고를 법정 기준에 따라 처리한다.'
    if kind == 'freelancer':
        clauses = [
            ('계약 목적', f'{employer or "위탁자"}는 {employee or "수급인"}에게 독립 용역을 의뢰하고 수급인은 계약 범위의 산출물을 제공한다.'),
            ('용역 기간 및 장소', f'{start or "용역 시작일 미입력"}부터 {end or "종료일 별도 합의"}까지 수행한다. 수행 장소·일정은 산출물 납품과 검수에 필요한 범위에서 협의하며 고정 출퇴근, 매장 상시 근무표, 교대조 편입으로 운영하지 않는다.'),
            ('업무범위, 산출물, 검수', v('freelancer_scope') or v('job_description', '업무 범위 미입력')),
            ('용역비 및 정산', f"{wage_line}. {v('freelancer_settlement_terms', '검수 완료 후 합의한 지급일에 지급하며, 사업소득세 3.3%를 원천징수한다.')}"),
            ('독립적 업무수행', '수급인은 독립 사업자로서 업무수행방법을 스스로 정한다. 고정 근무, 상시 지휘감독, 대체인력 제한, 매장 상시업무 편입 등 근로자성 징표가 생기면 계약 명칭과 무관하게 근로계약 전환을 검토한다.'),
            ('안전보건 및 협조', '위탁자는 용역 수행에 필요한 자료와 안전·보건상 유의사항을 안내하고, 수급인은 관련 법령과 사업장 안전·위생 기준을 준수한다.'),
            ('비밀유지 및 자료보호', v('confidentiality_terms', '매출자료, 고객정보, 레시피, 정산자료, 계정정보를 외부에 공개하지 않는다.')),
            ('계약 해지 및 기성 정산', v('termination_terms', '중대한 계약 위반, 검수 불합격 반복, 보안 위반 시 서면 통지 후 해지할 수 있으며 검수 완료 산출물은 별도 정산한다.')),
        ]
    elif kind == 'confidentiality':
        clauses = [
            ('서약 목적', f'{employee or "서약자"}는 근무 또는 용역 수행 중 알게 된 영업비밀과 개인정보를 보호한다.'),
            ('비밀정보 범위', '고객정보, 직원정보, 매출·정산자료, 원가·매입자료, 레시피, 영업 매뉴얼, 계정정보, 시스템 접근정보를 포함한다.'),
            ('금지 행위', '무단 열람, 반출, 촬영, 복사, 제3자 제공, 개인 저장매체 보관, 퇴사 후 사용을 금지한다.'),
            ('반환 및 삭제', '퇴사·계약 종료 또는 사용자 요청 시 보유 자료를 즉시 반환하고 개인 기기·계정의 사본을 삭제한다.'),
            ('책임', v('confidentiality_terms', '위반 시 회사 손해, 고객 피해, 법령 위반에 대한 책임을 부담할 수 있다.')),
        ]
    else:
        manager_note = ''
        if kind == 'manager':
            manager_note = '매니저는 포스 마감, 현금·정산, 위생·근태 점검, 직원 교육, 배달앱 운영계정 관리 책임을 포함한다.'
            if not is_v3:
                manager_note += ' 매니저도 근로기준법상 근로자이며 직함만으로 연장·야간근로수당 적용에서 제외되지 않는다.'
        if is_v3:
            period = f'{start or "입사일 미입력"}부터 {end or "기간의 정함 없음"}까지로 한다. ' + ('기간의 정함이 없는 근로계약으로 한다.' if kind == 'regular' else '')
        elif end:
            period = f'{start}부터 {end}까지로 한다.'
            if kind == 'fixed_term':
                period += ' 갱신은 만료 전 당사자가 서면 합의한다. 기간제법 제4조에 따라 2년 초과 사용 시 무기계약 전환 여부를 준수한다.'
            elif kind == 'daily':
                period += ' 근로일 단위로 계약하며 계속근로 여부는 실제 근무관계로 판단한다.'
        else:
            period = f'{korean_date(start)}부터 기간의 정함이 없는 근로계약으로 한다.'
        clauses = [
            ('근로계약 기간', period),
            ('근무장소 및 업무', f"{v('workplace') or v('branch', '근무장소 미입력')}에서 {v('job_description', '업무내용 미입력')} 업무를 수행한다. {manager_note}"),
            ('소정근로시간 및 휴게', f"{v('work_time', '근무시간 미입력')} / 휴게 {v('rest_time', '미입력')} / 주 소정근로시간 {v('weekly_hours', '미입력')}. {part_note}"),
            ('근무일, 휴일, 주휴', f"근무일은 {v('work_days', '미입력')}, 휴일은 {v('holidays', '미입력')}로 한다.{part_note if is_v3 else ''}"),
            ('임금, 계산방법, 지급방법', f"{wage_line}. 임금 구성: {v('wage_composition', '기본급, 법정수당, 공제항목 미입력')}. 최저임금 및 임금명세서 교부 기준을 준수한다."),
            ('연장·야간·휴일근로', overtime or '연장·야간·휴일근로는 사전 승인 기준으로 운영하고 법정 가산수당을 급여 지급 시 반영한다.'),
            ('휴가, 결근, 근태', leave or '연차유급휴가, 지각, 조퇴, 결근, 병가 처리는 근태기록과 법정 기준에 따라 급여내역서에 반영한다.'),
            ('4대보험 및 세무', insurance or f'{tax_label} 기준으로 취득 신고, 원천세, 급여명세서 교부를 처리한다.'),
        ]
        if not is_v3:
            clauses.append(('퇴직급여', '사업장 규모와 무관하게 계속근로 1년 이상이고 4주 평균 주 15시간 이상인 근로자에게 퇴직급여보장법에 따른 퇴직급여를 지급한다. 주 15시간 미만 초단시간근로자는 주휴·연차·퇴직급여 적용 대상에서 제외되며 실제 계속근로와 소정근로시간으로 판단한다.'))
        clauses.extend([
            ('수습 및 평가', probation_text or '수습기간 없음.'),
            ('식대, 유니폼, 보건증', v('meal_uniform_terms', '식사 제공, 유니폼 지급·반납, 보건증 제출·갱신, 위생교육 이수 기준을 준수한다.')),
            ('퇴직 및 계약 해지', v('termination_terms', '퇴직 시 사전 통보, 인수인계, 무단결근·중대한 위반 시 계약 해지 기준을 적용한다.')),
            ('개인정보, 보안, 위생', v('confidentiality_terms', '고객정보, 매출자료, 레시피, 배달앱·포스 계정정보, 위생수칙을 준수하고 무단 반출·공유를 금지한다.')),
        ])
    if kind in ('freelancer', 'confidentiality') and probation_text and (legacy_probation or probation not in ('수습기간 없음.', '수습기간 없음. 수습을 정한 경우에도 입력 임금을 동일 적용한다.')):
        clauses.append(('기존 수습 약정', probation_text))
    if v('foreign_worker'):
        clauses.append(('외국인 근로자 체류자격', f"체류자격 {v('visa_status', '미입력')}, 외국인등록번호 {v('foreign_registration_no_masked', '미입력')}. 근로자는 체류자격·체류기간이 바뀌면 사용자에게 즉시 알린다."))
    if v('minor_guardian_name'):
        protection = '연령증명서와 친권자/후견인 동의서를 사업장에 비치한다.' if is_v3 else '근로기준법 제66조에 따른 가족관계증명서와 친권자/후견인 동의서를 비치한다. 제69조에 따라 1일 7시간·주 35시간을 원칙으로 하며, 제70조에 따른 야간·휴일근로 제한을 준수한다.'
        clauses.append(('연소근로자 보호', f"친권자/후견인 {v('minor_guardian_name')}, 연락처 {v('minor_guardian_phone', '미입력')}. {protection}"))
    delivery = {
        'freelancer': ('계약서 제공 및 전자서명', '본 용역계약서는 전자서명 요청 링크로 수급인에게 제공하며, 전자서명 완료본을 전자문서로 보관하고 당사자에게 교부한다.'),
        'confidentiality': ('서약서 제공 및 전자서명', '본 서약서는 전자서명 요청 링크로 서약자에게 제공하며, 전자서명 완료본을 전자문서로 보관하고 당사자에게 교부한다.'),
    }
    clauses.append(delivery.get(kind, ('계약서 교부 및 전자서명', '사업주는 근로계약 체결 시 본 계약서를 근로자에게 교부하며, 전자서명 완료본은 전자문서로 보관한다.')))
    # previewText trims the whole body, not individual interpolated values.
    return [(title, body.strip() or '미입력') for title, body in clauses]
