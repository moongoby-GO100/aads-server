# 교육자료 추가 — 바이브코딩 개발 용어 대사전

- 작성: 2026-09-14 KST, CEO 지시 "바이브코딩에 있어서 중요한 개발 용어도 모두 상세하게 정리해서 교육자료로 만들어줘"
- 프로젝트: AADS (서버68)
- 상태: 파일 생성·포털 등록·공개 URL 검증 완료 / git 커밋·푸시 미실행

> 주의: `handover_write`(DB 정본, R-HANDOVER-DB)가 이번 세션에서 `missing_tenant_id`로 실패했습니다.
> 이 파일은 **임시 보조 기록**이며 DB 정본을 대체하지 않습니다. 세션 컨텍스트 복구 후 `handover_write`로 재기록이 필요합니다.

## 변경 파일

| 파일 | 구분 | 크기 |
|---|---|---|
| `app/static/reports/20260914_dev_terminology_vibecoding_education.html` | 신규 | 153,640 bytes / 1,465 lines |
| `app/static/reports/index.html` | 수정(1행 추가) | 19,425 bytes |

## 문서 구성

21개 장, `class="term"` 표제어 380개, 외부 1차 출처 19건.

1. 왜 용어인가 — 용어가 지시 품질을 결정한다
2. 개발 전체 지도 — 요청 하나가 지나가는 길
3. 코드 구조 용어 (변수 → 프레임워크)
4. Git·버전관리 용어
5. API·통신 용어 (HTTP 상태코드 16종 포함)
6. 데이터베이스 용어
7. 인프라·컨테이너·배포 용어
8. 테스트·품질 용어
9. 성능·관측성 용어
10. 신뢰성 용어 — 멱등성·원자성·정합성
11. 보안 용어
12. 아키텍처 용어
13. 프론트엔드·UI 용어
14. AI·LLM 용어
15. 바이브코딩 고유 용어
16. 기획·프로세스 용어
17. 에러·디버깅·장애 용어
18. 헷갈리는 용어 짝 30선
19. CEO 지시문 템플릿 6종
20. AADS 실제 적용 용어 지도
21. 참고자료 및 다음 단계

## AADS 적용 근거 (2026-09-14 실측)

파일 존재 확인:
`deploy.sh`, `scripts/reload-api.sh`, `scripts/error_book.py`, `scripts/run_unit_tests.sh`,
`app/core/anthropic_client.py`, `/root/aads/AGENTS.md`, `/root/aads/aads-dashboard/deploy.sh`,
`.git/hooks/pre-commit` (15,653 bytes)

`git worktree list` 실행 — `/root/aads/.worktrees/` 하위 격리 워크트리 다수 운영 확인.

인용 규칙: R-DOCKER, R-KEY, R-AUTH, R-COMMIT, R-QUALITY, R-BG, R-ERRBOOK, R-RELEASE, R-HANDOVER-DB.

## 검증 결과

| 항목 | 명령·도구 | 결과 |
|---|---|---|
| 태그 구조 | `grep -o` 카운트 | div 115/115, table 40/40, tr 518/518 ✅ |
| 앵커 | ch1~ch21 존재 확인 | 누락 0건, 중복 id 0건 ✅ |
| 컨테이너 동기화 | `docker exec aads-server stat` | 153,640 bytes 일치 ✅ |
| 공개 URL | `curl -w %{http_code}` | 200 / 154,875 bytes ✅ |
| 본문 무결성 | `diff` (로컬 vs 공개 수신본) | 차이 2곳 모두 Cloudflare 봇탐지 주입, 본문 동일 ✅ |
| 포털 인덱스 | `curl` | 200 ✅ |
| 브라우저 렌더 | Playwright `browser_navigate` | 제목 "바이브코딩 개발 용어 대사전 — AADS Academy" 확인 ✅ |
| 전체 화면 캡처 | `browser_screenshot` | ❌ 실패 — Browser Bridge 세션 about:blank, 전역 브리지 `localhost:9333` 응답 없음 |

## 공개 URL

- 교육자료: https://fb.newtalk.kr/static/reports/20260914_dev_terminology_vibecoding_education.html
- 포털: https://fb.newtalk.kr/static/reports/index.html

## 미완료 / 다음 행동

1. git 커밋·푸시 미실행 — 승인 시 위 2개 파일만 선별 커밋
2. 전체 화면 스크린샷 미확보 — Browser Bridge 복구 후 재시도
3. `handover_write` DB 정본 미기록 — 세션 컨텍스트 복구 후 재기록
