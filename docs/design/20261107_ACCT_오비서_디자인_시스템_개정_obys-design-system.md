---
document_key: obys-design-system
kind: design
status: draft
change_summary: 재검수 지적 반영 - 근태 판정 절 분리, dialog·inline 색 조사표 추가, WCAG 등급 구분, 롤백 절차 보강
---
# 오비서 디자인 시스템 (obys-design-system)

이 문서는 승인되지 않은 초안이다. 이전 판본(1.0.6)의 자동 재검수 지적 4건을 반영한 새 판본이다. ① 근태 '기본 출근' 판정 절을 이 문서에서 빼 별도 문서로 옮겼다(`docs/design/20261107_ACCT_오비서_근태_기본_출근_규칙_obys-attendance-default-present.md`, document_key `obys-attendance-default-present`). ② 6절에 dialog·inline 색 조사 결과를 표로 넣었다. ③ 7절 터치 영역 근거 등급을 구분했다. ④ 9절 롤백에 배포 경로와 충돌 대응을 적었다. 색 값은 이전 판본과 같다(#a85a12·#127352).

- 근거 코드: aads-server 5c6937d7(2026-11-07 재조사; 색·dialog 조사는 이 SHA 기준, 3절 행 번호는 ce79b3a0 확인값이며 구현 착수 시 다시 대조한다), `app/static/apps/obys/admin.html`, `app/static/apps/obys/modules/contract-editor-v41.css`.
- rev5 대비 정정: rev5 는 contract-editor-v41.css 의 `var(--orange)`·`var(--green)` 글자 지정을 0건이라고 적었다. 실제로는 1건(11행 `.cv41-steps li.done{color:var(--green)}`)이 있다. 3절에서 고쳐 적었다.

## 1. 목표
- 직원·근태·급여 화면의 **상태 글자**를 실제로 쓰이는 배경 위에서 WCAG 2.x 일반 텍스트 4.5:1 이상으로 맞춘다.
- 이미 코드에 있는 글자·간격·입력 높이·폼 배치 규칙을 한 곳에 기록해, 화면마다 따로 정하지 않게 한다.

## 2. 범위와 비목표
| 구분 | 내용 |
|---|---|
| 이번 변경 범위 | ① 3절 대상 화면의 상태 글자색(color) ② 그 색을 화면 밖 dialog 에도 적용하기 위한 최소 동작 변경: dialog 를 열 때 `people-dialog` 클래스를 붙이고 닫을 때 떼는 JS 토글(6절 3항) |
| 기록만 하는 범위 | 7절 글자·간격·입력·배치 규칙, 8절 상태·오류 안내. 이미 admin.html 에 구현돼 있으며 이번 변경에서 바꾸지 않는다 |
| 신규 코드 규칙 | 4.3절 보조색 규칙. 기존 코드는 바꾸지 않고, 이번 구현과 이후 새로 쓰는 코드에만 적용한다 |
| 비목표 | 근태·급여 판정 로직 변경(별도 문서 `obys-attendance-default-present` 가 소유하며 이 문서의 구현으로 바꾸지 않는다), 다른 메뉴의 전역 변수·컴포넌트, 배경색·테두리색, 다크 테마, 로고·브랜드 색, 차트 색, 가져오기 미리보기, 계약 편집 단계 표시(3절 범위 밖 요소) |

## 3. 대상 화면과 실제 구조 (ce79b3a0 확인)
- 대상 라우트: `employees`, `attendance`, `payroll`.
- 범위 표시: admin.html 467행 `renderPageV41()` 이 `main#page`(88행)의 className 을 `"page" + (위 세 라우트면 " people-screen")` 로 매번 다시 설정한다.
  - 라우트에 들어가면 `.people-screen` 클래스가 붙고, 나가면 빠진다. 별도 래퍼가 필요 없다.
  - `#people-screen`(id)은 존재하지 않으므로 쓰지 않는다.
- 이미 이 클래스로 범위를 건 규칙: admin.html 35~54행(글자·입력 높이·선택 상태·달력·계약 원문), 70~71행(1050px·600px 폼 열 수).
- 현재 상태 글자 정의: 21행 `.metric span{color:var(--green)}`, `.warn span{color:var(--orange)}`. 24행 `.badge{color:#167454;background:#eaf7f1}`, `.badge.warn{color:#a95611;background:#fff3e8}`. 25행 `.notice{color:#8a5a10;background:#fff8e8}`. 26행 `.error-text{color:var(--red)}`.
- 범위 밖 요소
  - 공용 dialog(`#detailDialog` 91행, `#registerDialog` 92행 등)는 `#page` 밖에 있어 `.people-screen` 규칙이 닿지 않는다. 그래서 6절 3항으로 처리한다.
  - 계약 편집 `modules/contract-editor-v41.css` 11행 `.cv41-steps li.done{color:var(--green)}` 는 계약 편집 단계 표시의 완료 글자다. 이 파일은 별도 컴포넌트 스타일이라 이번 변경에 넣지 않는다. 배경 조합과 대비는 10절 7항에서 측정만 하고, 미달이면 별도 작업으로 낸다.
- 기존 변수(15행 `:root`): `--ink #172033`, `--muted #667085`, `--bg #f4f6f9`, `--line #e2e8f0`, `--blue #2457d6`, `--blue2 #eef4ff`, `--green #16845b`, `--orange #c56a16`, `--red #c93645`.

## 4. 글자·배경 조합과 대비 (WCAG 2.x 상대 휘도)
### 4.1 변경 후 실제 DOM 에 남는 조합 — 판정 대상
10절 수용 기준 2 는 **이 표의 행만** 검사한다. 모든 행이 4.5:1 이상이어야 한다.
| 요소(정의 위치) | 글자 | 배경 | 대비 | 비고 |
|---|---|---|---|---|
| `.badge` (24행) | #167454 | #eaf7f1 | 5.21 | 변경 안 함 |
| `.badge.warn` (24행) | #a95611 | #fff3e8 | 4.78 | 변경 안 함 |
| `.notice` (25행) | #8a5a10 | #fff8e8 | 5.59 | 변경 안 함 |
| `.error-text` (26행, `--red`) | #c93645 | #ffffff / #f4f6f9 | 5.13 / 4.73 | 변경 안 함 |
| `.warn span` 교체 후 (`--orange-text`) | #a85a12 | #ffffff / #f4f6f9 / #fff3e8 | 5.08 / 4.69 / 4.65 | 이번 변경 |
| `.metric span` 교체 후 (`--green-text`) | #127352 | #ffffff / #f4f6f9 / #eaf7f1 | 5.83 / 5.39 / 5.30 | 이번 변경 |
| 보조 `--muted` | #667085 | #ffffff / #f4f6f9 / #eef4ff | 4.97 / 4.59 / 4.51 | 변경 안 함, 4.3절 참고 |
| 본문 `--ink` | #172033 | #eef4ff | 14.73 | 변경 안 함 |

### 4.2 교체 전 조합 — 비교용, 판정 제외
대상 화면에서 교체 전에 쓰이던 값이다. 교체 이유를 남기려는 기록이며 **수용 기준의 통과 판정에 넣지 않는다.** 구현 후 대상 화면 DOM 에 이 조합이 글자색으로 남아 있으면 수용 기준 1 의 실패다.
| 요소 | 글자 | 배경 | 대비 | 교체 후 |
|---|---|---|---|---|
| `.warn span` (21행) | #c56a16 | #ffffff / #f4f6f9 | 3.86 / 3.56 | #a85a12 |
| `.metric span` (21행) | #16845b | #ffffff / #f4f6f9 | 4.68 / 4.32 | #127352 |

- 하드코딩된 배지·안내 색은 이미 기준을 넘으므로 바꾸지 않는다. 바꿀 것은 대상 화면에서 `var(--orange)`·`var(--green)` 을 **글자색**으로 쓰는 곳뿐이다.
- 기존 #c56a16·#16845b 는 범위 밖 메뉴의 글자와, 대상 화면의 장식(테두리·아이콘 채움)에 남는다.

### 4.3 보조색 규칙 — 신규 코드 규칙 (기존 코드 변경 없음)
- 기존 코드: `--muted` 가 `#eef4ff`(`--blue2`) 위에 쓰인 곳은 4.51 로 기준을 넘는다. 그래서 이번 변경에서 바꾸지 않는다.
- 신규 규칙: 여유가 0.01 뿐이므로, 이번 구현과 이후 새로 쓰는 코드에서 `--blue2` 배경 위 보조 문구는 `--ink` 를 쓴다. `--muted` 는 흰색·`#f4f6f9` 위에서만 새로 쓴다.
- 확인 방법: 10절 수용 기준 8.

## 5. 대안 비교 (색 변경에만 해당)
| 안 | 내용 | 장점 | 리스크 | 판정 |
|---|---|---|---|---|
| A | `:root` 에 글자 전용 변수 `--orange-text`·`--green-text` 를 새로 선언하고, 대상 화면 선택자에서만 `color` 를 그 변수로 바꿈 | 새 이름이라 다른 메뉴가 쓰지 않음(ce79b3a0 기준 참조 0건), dialog 에도 같은 변수를 쓸 수 있음, 되돌리기 쉬움 | 변수 2개 증가 | **권장** |
| B | 전역 `--orange`·`--green` 값을 바로 교체 | 변수 수 그대로 | 다른 메뉴의 배지·차트·`.metric`·계약 편집 단계 표시 색이 함께 바뀜 | 비권장 |
| C | 색은 두고 글자를 키워 대형 텍스트 기준(3:1)으로 맞춤 | 색 변경 없음 | 13~14px 본문·배지에는 적용 불가 | 비권장 |

7·8절은 이미 구현된 값의 기록이므로 대안 비교 대상이 아니다.

## 6. 적용 구조 (구현 시)
1. 15행 `:root` 에 `--orange-text:#a85a12;--green-text:#127352;` 두 개를 추가한다. 기존 변수 값은 바꾸지 않는다.
2. `.people-screen .warn span{color:var(--orange-text)}`, `.people-screen .metric span{color:var(--green-text)}` 를 추가한다. 기존 21행 전역 규칙은 그대로 둔다.
3. dialog 클래스 토글(JS, 2절 범위 ②): 대상 라우트에서 dialog 를 열 때 `people-dialog` 클래스를 붙이고, dialog `close` 이벤트에서 뗀다. 같은 두 규칙을 `.people-dialog` 에도 건다. 대상 dialog 는 아래 6.1 표로 확정한다.
4. 대상 라우트의 JS 가 inline `style="color:var(--orange|--green)"` 를 쓰면 같은 새 변수로 바꾼다. 교체 대상은 아래 6.2 표로 확정한다.
5. 이 문서가 승인되기 전에는 코드를 바꾸지 않는다. 구현은 별도 러너 작업으로 낸다. 구현 착수 전 선행 조건: 6.1·6.2 의 grep 을 현재 origin/main 에서 다시 돌려 표와 다르면 표를 먼저 고친다.

### 6.1 showModal() 호출 위치 조사 (5c6937d7, `grep -n "showModal" admin.html modules/*.js`)
| 위치 | dialog | 열리는 화면 | 판정 |
|---|---|---|---|
| admin.html 500행 | `#sourceDataDialog` (원본 보기, 동적 생성) | 근태 등 원본 확인 | 확인 후 대상 여부 결정: 대상 라우트(employees·attendance·payroll)에서 열리면 대상 |
| admin.html 539행 `openEmployeeSeparationV41` | 퇴사·계약해지·복직 dialog (동적 생성) | employees | 대상 |
| admin.html 559행 `openDetailV41` | `#detailDialog` | 여러 라우트 공용 | 대상 라우트에서 열릴 때만 `people-dialog` 부여 |
| admin.html 560행 `openRegisterV41` | `#registerDialog` | 여러 라우트 공용 | 대상 라우트에서 열릴 때만 부여 |
| admin.html 591행 `openHelpV41` | `#detailDialog` 재사용 | 공용 도움말 | 대상 라우트에서 열릴 때만 부여 |
| modules/payroll-admin.js 112행 | 지급 입력 dialog (동적 생성) | payroll | 대상 |
| modules/contract-editor-v41.js 996·1704·1804행 | 계약 편집 dialog·sheet·문서 dialog | employees 안의 계약 편집 | 계약 편집 단계 표시는 범위 밖(2절). 이 dialog 안에 `.warn span`·`.metric span` 이 있는지는 구현 착수 전 확인하고, 없으면 제외 |
- 공용 dialog(`#detailDialog`·`#registerDialog`)는 같은 요소를 여러 라우트가 쓰므로 열 때마다 클래스를 새로 계산한다(대상 라우트면 붙이고 아니면 뗀다). 수용 기준 5의 검사 대상은 위 표의 "대상" 행 전부와 공용 dialog 3개다.

### 6.2 inline 색 사용처 조사 (5c6937d7, `grep -n 'color:var(--\(orange\|green\))'`)
| 위치 | 내용 | 판정 |
|---|---|---|
| admin.html 565·566행 | 가져오기·등록 미리보기 안 `style="color:var(--green)"` 3건 | 비목표(가져오기 미리보기, 2절). 교체하지 않는다 |
| modules/*.js | 0건 | 해당 없음 |
- 결과: 대상 라우트의 inline `--orange`·`--green` 글자색은 0건이므로 6절 4항의 교체 작업은 현재 없다. 구현 착수 전 재조사에서 새로 생겼으면 그때 이 표에 추가한다.
5. 이 문서가 승인되기 전에는 코드를 바꾸지 않는다. 구현은 별도 러너 작업으로 낸다.

## 7. 글자·간격·입력·배치 규칙 (기존 코드 기록, 이번 변경 아님)
| 규칙 | 값 | 현재 코드 근거 |
|---|---|---|
| 본문 글자 | 14px, 줄간격 1.5 | admin.html 35행 `.people-screen` |
| 입력·버튼·탭 | 최소 높이 44px, 글자 13px | 36행 |
| 표·배지·보조 문구 | 13px | 45행 |
| 체크박스·선택 라벨 | 24px, 라벨 선택 영역 44px | 43·44행 |
| 폼 열 수 | 기본 4열, 1050px 이하 2열, 600px 이하 1열 | 37·70·71행 |
| 간격 | 4·6·8·12·16·24px, 카드 안쪽 16px | 37·41·46행 등 기존 클래스 값 |
| 표 | 별도 가로 스크롤 영역(`.table-wrap`) | 기존 마크업 |

44px 은 WCAG 2.5.5 Target Size (Enhanced, **AAA 권장** 44×44 CSS px)를 따른 내부 기준이다. 이 문서의 2.x 일반 기준 목표(AA)에서 요구하는 최소값은 2.5.8 Target Size (Minimum)의 24×24 CSS px 이며, 44px 은 이를 넘는 더 엄격한 값이다. 두 등급을 섞어 통과 판정하지 않는다: 수용 기준 4는 44px(내부 기준)로 검사하고, 24px 미만이면 AA 위반으로 따로 표시한다. 13px 은 기존 코드 값이다. 이 규칙을 바꾸려면 별도 판본·별도 구현으로 한다.

## 8. 상태·오류 안내 (기존 동작 기록, 이번 변경 아님)
- 빈 상태에는 원인과 다음 행동을 함께 쓴다. 예: '승인된 직원이 없습니다. 직원 현황·초대·가입승인에서 승인하세요.'
- 저장 실패처럼 즉시 알려야 하는 오류는 `role="alert"` 로 표시한다. 조회 결과·진행 상태 같은 정보는 `role="status"` 로 표시한다.
- 저장에 실패하면 입력값을 그대로 유지하고 재시도 버튼을 둔다.
- 계약 원문은 줄바꿈을 보존(54행 `.contract-basis`)하고, 해석할 수 없으면 '직접 등록'을 안내한다.
- 기록과 실제 동작이 다르면 별도 작업으로 낸다.

## 9. 리스크와 롤백
| 리스크 | 대응 |
|---|---|
| 새 변수가 대상 밖에서 쓰임 | ce79b3a0 기준 `--orange-text`·`--green-text` 참조 0건. 구현 리뷰에서 `git grep -e "--orange-text" -e "--green-text"` 결과가 6절 위치뿐인지 확인 |
| dialog 클래스를 떼지 않아 다른 메뉴 dialog 에 남음 | `close` 이벤트에서 제거. 수용 기준 5로 확인 |
| 색이 어두워져 상태 구분이 약해짐 | 색만으로 상태를 전달하지 않는다(문구 병기) |
| 계약 편집 단계 표시가 미달로 남음 | 범위 밖으로 명시. 수용 기준 7 로 측정만 하고, 미달이면 별도 작업 |

롤백: 이번 변경은 커밋 1개로 낸다(admin.html 변수 2개·선택자 규칙·dialog 클래스 토글, `modules/payroll-admin.js` 지급 입력 dialog 토글, 필요하면 `contract-editor-v41.js` 토글·inline 색 교체). 되돌릴 때는 아래 순서를 따른다.
1. 구현 커밋 SHA 를 해당 러너 작업 결과(`runner-…` 커밋 제목)에서 확인한다.
2. 이후 커밋이 쌓이지 않았으면 `git revert <SHA>` 한다.
3. 같은 파일을 건드린 커밋이 이후에 쌓여 revert 가 충돌하면 자동 해결하지 않는다. 수동 되돌림은 **참조하는 쪽부터 지우고 정의를 마지막에** 지운다(변수를 먼저 지우면 남은 참조가 무효 값이 된다). 대상 파일은 6.1절 표의 모든 파일이다: `admin.html`, `modules/payroll-admin.js`, 그리고 구현 때 `people-dialog` 토글을 넣은 `modules/contract-editor-v41.js`(해당 시).
   - (a) 동적 dialog 생성 코드의 `people-dialog` 클래스 부여·제거: admin.html 의 `openEmployeeSeparationV41`·`openDetailV41`·`openRegisterV41`·`openHelpV41`·`#sourceDataDialog` 생성부, `modules/payroll-admin.js` 의 지급 입력 dialog 생성부(`document.createElement("dialog")` 근처), 해당 시 `modules/contract-editor-v41.js` 의 dialog 생성부.
   - (b) 공용 dialog 의 `close` 이벤트 리스너 중 `people-dialog` 를 떼는 코드.
   - (c) CSS 의 `.people-dialog …` 규칙과 `.people-screen .warn span`·`.people-screen .metric span` 추가분.
   - (d) inline 색을 바꿨다면 원래 `var(--orange|--green)` 으로 복원.
   - (e) 마지막으로 `:root` 의 `--orange-text`·`--green-text` 두 변수 삭제.
   - 그 밖의 줄(이후 커밋의 변경)은 건드리지 않는다. 끝나면 `git grep -e "--orange-text" -e "--green-text" -e "people-dialog" -- app/static/apps/obys` 가 0건이고 아래 확인 셋이 통과해야 한다. 한 건이라도 남으면 롤백 미완료다.
4. 배포는 처음 변경을 내보낸 것과 같은 경로로 한다. ACCT 배포 요청 경로는 러너가 commit·push 후 서버 `/root/aads/AGENTS.md` 의 블루/그린 릴리스 계약과 `aads-server/AGENTS.md`(API 릴리스, `bash /root/aads/aads-server/deploy.sh bluegreen`)에 따라 수행한다. 정적 파일(`app/static/apps/obys/`)만 바뀐 경우의 배포 방식도 이 두 문서가 정본이며, 사람이 `docker compose up -d` 나 `supervisorctl restart` 를 직접 치지 않는다(CLAUDE.md R-DOCKER). 롤백도 새 러너 작업으로 낸다.
7·8절은 바꾸지 않으므로 롤백 대상이 아니다.

롤백 후에는 다음 셋을 확인한다.
- 대상 화면 1곳과 범위 밖 메뉴 1곳의 `.warn span` 계산 색이 #c56a16 으로 돌아왔는지
- 입력 높이·폼 열 수가 변경 전과 같은지
- 콘솔 오류가 0건인지

## 10. 수용 기준
1. 대상 세 라우트의 `.warn span`·`.metric span` 계산 색이 각각 #a85a12·#127352 다. 4.2절의 교체 전 글자색(#c56a16·#16845b)이 대상 화면 DOM 의 상태 글자에 남아 있지 않다. 브라우저 `getComputedStyle` 값을 결과 JSON 에 기록한다.
2. **변경 후 대상 화면 DOM 에 실제로 남는 글자·배경 조합**(4.1절 표의 행)이 모두 4.5:1 이상이다. 4.2절의 교체 전 행은 비교용이며 이 판정에서 제외한다. 계산 스크립트 출력(요소·글자색·배경색·대비)을 첨부한다.
3. 범위 밖 메뉴 2곳(예: 매출, 가져오기 미리보기)과 공용 dialog 1개(대상 라우트가 아닌 곳에서 연 것)의 `.warn span`·`.metric span` 계산 색이 구현 전과 같다(구현 전후 `getComputedStyle` 비교).
4. 폭 1280·1051·1050·601·600·390px 에서 다음이 맞다. 각 폭의 DOM 계산값으로 기록한다.
   - 폼 열 수가 4·4·2·2·1·1이다.
   - 입력·버튼 높이가 44px 이상이다.
   - 이것은 회귀 확인이며 값이 바뀌면 실패다.
5. 6.1절 표의 "대상" 행 dialog 전부와 공용 dialog 3개(`#detailDialog`·`#registerDialog`·`#sourceDataDialog`)에 대해, 대상 라우트에서 연 것은 `people-dialog` 클래스를 가진다. 닫은 뒤, 그리고 다른 메뉴에서 같은 dialog 를 열었을 때는 클래스가 없다.
6. 저장 실패를 모의(네트워크 차단)하면 `role="alert"` 안내가 뜨고, 입력값이 유지되며, 재시도 후 저장에 성공한다. 기존 동작 확인이며, 실패하면 별도 작업으로 등록한다.
7. 계약 편집 `.cv41-steps li.done` 의 계산 글자색·배경색·대비를 측정해 결과에 적는다. 측정만 하는 항목이며 이번 변경의 통과 판정에 넣지 않는다. 4.5:1 미만이면 별도 작업으로 등록한다.
8. 4.3절 신규 규칙: 구현 커밋 diff 에서 `--blue2`/`#eef4ff` 배경 요소에 새로 추가된 `color:var(--muted)` 가 0건이다(`git diff` grep 결과 첨부). 기존 코드의 `--muted`/`#eef4ff` 조합은 수용 기준 2 의 4.51 행으로 검사한다.
