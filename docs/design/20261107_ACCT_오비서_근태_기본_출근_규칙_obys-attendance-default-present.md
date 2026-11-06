---
document_key: obys-attendance-default-present
kind: design
status: draft
change_summary: 근무표만 있고 출근 기록이 없는 날을 '출근(기본)'으로 판정하고 급여 초안에 포함하는 규칙의 영향 범위·결근 수정·롤백·수용 기준
---
# 오비서 근태 기본 출근 규칙 (obys-attendance-default-present)

이 문서는 승인되지 않은 초안이다. 디자인 시스템 문서(`obys-design-system`)에 섞여 있던 '구현 중 변경' 절을 분리한 것이다. 급여 금액에 영향을 주는 판정 규칙이므로 디자인(색·dialog)과 별도로 승인한다. 이미 구현·반영된 코드(runner-6b44b93a 197fa662, runner-f765cd5f 5c6937d7)를 사후 기록하며, 승인 결과에 따라 보완 작업을 낸다.

## 1. 기준 문서와 정합성
- 근태 화면 기준: `docs/prd/20261028_AADS_오비서_근태_현황_목업_검토.md`(document_key `obys-attendance-mockup-review`). 이 문서는 "지각·초과근무·주휴·자동 승인 등 미정 정책은 결정하지 않는다"고 했으므로, '출근(기본)'은 그 정책 범위 밖이 아닌지 **승인 시 확인할 사항**이다. 승인 전에는 해당 PRD 의 "출퇴근 처리 흐름"에 기본 출근 규칙과 결근 수정 방식을 반영하는 새 판본이 필요하다.
- 직원·급여 소유 spec(승인된 직원·급여 spec)과의 정합성은 아직 확인되지 않았다. 승인 요청 시 해당 spec 의 document_key 와 판본을 이 절에 적고, 급여 초안 산식과 충돌이 없음을 확인한다. 확인 전에는 "정합성 미확인"으로 둔다.

## 2. 규칙
- 근무표(kind=work)가 있고 시작시각이 지났는데 실제 출퇴근 기록이 없으면 `출근(기본)` 으로 판정한다(코드: `app/services/obys_attendance_default.py`, 출처 표시 `schedule_default`).
- 결근은 대표(owner/admin)가 직접 바꾼 날만이다: `status=rejected` 이고 `memo='결근(대표 확인)'`.
- 이전 동작: 같은 경우 오늘은 '미출근', 지난 날은 '확인필요'로 표시했고 급여 초안에는 포함하지 않았다.
- 바뀌지 않는 것: 지각·조퇴 판정, 실제 타각 기록, 시급·일급 등 금액 산식.

## 3. 영향 범위
| 대상 | 변경 | 근거 |
|---|---|---|
| 관리자 근태 현황(오늘 요약·직원별·개인 달력) | 근무표만 있는 날이 `출근(기본)` 으로 보인다 | admin.html `absenceActionV41` 등 |
| 급여 초안 | 기본 출근일이 근무일수에 포함된다. 응답에 `default_present_days`·`default_present_dates` 로 별도 표시하고, 실제 타각 근무시간(`real_worked`)과 구분한다 | `app/services/yeoljeong_finance_service.py` `_payroll_default_present_rows` |
| 일급·월급 등 금액 | 포함된 일수만큼 늘 수 있다. 시급제처럼 타각 시간이 필요한 산식에 기본 출근일이 어떻게 반영되는지는 승인 전에 산식별로 표로 확인한다 | 확인 필요 |
| 제외 | 마지막 근무일 이후, 아직 시작 전인 날(`default_present_applies`) | 서비스 코드 |

## 4. 결근 수정 방식
- 관리자(owner/admin)에게만 버튼이 보인다. 근무표만 있는 날 `결근으로 수정`(mark-absent), 이미 결근 처리된 날 `출근으로 복원`(restore-present), 기록이 없고 기본 출근이 아닌 날 `출근으로 수정`(mark-present).
- 결근 처리는 위 `status=rejected`·`memo` 기록으로 남고 복원하면 되돌아간다. 기록 변경은 대표 확인 이력이 된다.
- 한 날에 기본 출근과 실제 기록이 섞이면 실제 기록이 우선한다(테스트: `test_obys_absence_mixed_day_node.py`).

## 5. 위험과 롤백
| 위험 | 대응 |
|---|---|
| 실제로 안 나온 직원이 급여 초안에 포함됨 | 급여 초안에서 기본 출근일을 `default_present_dates` 로 별도 표시하고, 지급 확정 전 대표 확인 단계에서 결근 수정이 가능하다 |
| 이미 확정·지급된 급여 | 기본 출근이 소급되어 이미 낸 급여가 바뀌는지는 승인 전 확인 대상(미확인). 통과 조건은 6절 8항 |

롤백: 반영 커밋(197fa662, 5c6937d7 및 이후 후속)을 `git revert` 하고 같은 러너 배포 경로(`/root/aads/AGENTS.md` 블루/그린 계약, `aads-server/AGENTS.md`)로 내보낸다. 충돌 시에는 `obys_attendance_default.py` 호출부(`_payroll_default_present_rows` 호출과 admin.html `default_present` 분기)만 수동으로 제거하고 다른 줄은 건드리지 않는다. 롤백 후 근무표만 있는 날이 다시 '미출근/확인필요'이며 급여 초안 `default_present_days` 가 0인지 확인한다. 이미 저장된 `결근(대표 확인)` 기록은 삭제하지 않는다.

## 6. 수용 기준
1. 근무표만 있고 시작시각이 지난 날 → `출근(기본)`, 급여 초안 `default_present_days` 에 포함.
2. `결근(대표 확인)` 기록이 있는 날 → 결근, 급여 초안에서 제외. 복원하면 다시 기본 출근.
3. 시작 전 날·마지막 근무일 이후 날은 포함되지 않는다.
4. 실제 타각 기록이 있는 날은 기록이 우선하고 지각·조퇴 판정이 변경 전과 같다.
5. 기본 출근일 때문에 달라지는 급여 금액을 산식별(월급·일급·시급 등)로 구현 전후 비교해 표로 남긴다.
6. owner/admin 이 아닌 역할에는 결근 수정 버튼이 보이지 않는다.
7. 단위 테스트 통과: `bash scripts/run_unit_tests.sh tests/unit/test_obys_attendance_default_present.py tests/unit/test_obys_absence_lifecycle.py tests/unit/test_obys_absence_server_judgment.py`.
8. **확정·지급 급여 소급 불변**: 규칙 반영 전에 확정(confirmed) 또는 지급(paid) 상태가 된 급여 명세는 반영 후에도 금액·근무일수·상태가 한 건도 바뀌지 않는다. 검증: 반영 전 확정·지급 명세 전체의 (명세 id, 근무일수, 지급액, 상태)를 읽기 전용으로 덤프하고, 반영 후 같은 조회 결과와 비교해 차이 0건을 결과에 첨부한다. 차이가 1건이라도 있으면 통과 실패이며 롤백 대상이다(5절 롤백 후에도 같은 비교로 원상 복귀를 확인). 아직 확정 전인 초안만 `default_present_days` 로 바뀔 수 있다. 확정 명세가 하나도 없으면 "대상 0건"으로 적고 이 항목을 확인 불가로 보고한다(통과로 적지 않는다).
9. **초안 재계산 경계**: 확정 전 초안은 재계산 시 기본 출근일이 반영되고, 확정 후에는 재계산되지 않음을 같은 기간 명세 하나로 확인한다(초안 → 확정 → 규칙 토글 전후 재조회).
