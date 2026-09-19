# OVIS Smart Browser 구현 보고서

- 문서 키: `report:smart-browser-release`
- 문서 버전: `1.0.0`
- 목표: `169e5328-244e-444d-95c8-d20377192671`
- 대상 마일스톤: M7~M11
- 기준일: 2026-09-19 KST

## 구현 범위

| 단계 | 구현 계약 | 코드 정본 |
|---|---|---|
| M7 | Site Profile, Page Template, Site Skill, Semantic Memory, Live Observation을 tenant/site/version으로 격리 | `app/services/site_knowledge.py`, `app/api/site_knowledge.py` |
| M8 | 최초 ARIA 부분구조 학습은 candidate만 생성하고 재방문은 reuse/rediscover/human gateway로 판정 | `app/services/smart_browser_learning.py` |
| M9 | active Site Skill만 Exact → Qwen3 vector → 제한된 LLM allowlist 순으로 선택하고 실행 전 Channel Router를 재검증 | `app/services/smart_browser_learning.py`, `app/services/ohvis_harness.py` |
| M10 | 가격·재고 등 변동 사실은 Live Fact에 TTL·출처·증거를 저장하고 표시 직전 재검증 | `app/services/live_fact_gate.py`, `app/api/site_knowledge.py` |
| M11 | 읽기 전용 데모 사이트의 최초 구조 학습, 동적 값 변경 재방문, 페이지 명령 차단, 핵심 앵커 변경 Human Gateway 전환 | Playwright E2E 및 본 보고서 증거 |

## 보안 경계

- DOM·ARIA·페이지 텍스트는 `ObservationEnvelope`의 `UNTRUSTED_PAGE_DATA`로만 유입됩니다.
- 페이지 관측은 실행 권한·도구·origin·tenant·approval scope를 변경할 수 없습니다.
- 원시 HTML/DOM, 비밀번호·쿠키·OTP·토큰·주민등록번호·카드번호는 사이트 지식에 저장하지 않습니다.
- 스킬은 active 버전, 서버 등록 함수, JSON Schema, tenant/site scope, Human Gateway 정책을 모두 통과해야 실행됩니다.
- 신규 Page Template과 Skill은 즉시 active가 되지 않고 G6 candidate → shadow → active 승격 게이트를 거칩니다.

## 검증 근거

- 정적검사: 신규/변경 Python 파일 Ruff 통과.
- 단위·회귀: 사이트 지식, 학습, Channel Router, ARIA, Live Fact, Skill Registry, Golden Promotion 관련 테스트 통과.
- DB: 격리 PostgreSQL에서 G3 → G5 → G6 → M7~M11 순서 적용, 정상 origin 정규화, knowledge version 증가, candidate → shadow → active, 감사 이벤트를 트랜잭션 롤백 방식으로 검증.
- 브라우저: `https://books.toscrape.com/` HTTP 200, 상품 20개. 가격을 동적으로 바꿔도 구조 재사용(`similarity=1.0`), 페이지 명령문 차단, 필수 브랜드 앵커 제거 시 `human_gateway` 전환을 확인.
- 브라우저 캡처: `/tmp/smartbrowser-books-e2e-final.png` (릴리스 검증 호스트 로컬 증거).

## 운영 계약

DB 마이그레이션은 additive이며 `DROP`/`TRUNCATE`가 없습니다. API 릴리스는 clean SHA 단일 이미지 빌드, candidate 직접 health, 짧은 nginx lock, routed health 실패 시 rollback, 동일 digest standby, 5분 P0/P1 감시를 모두 통과해야 최종 완료입니다.
