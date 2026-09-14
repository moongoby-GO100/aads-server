# 공유 교훈 INDEX — **이관됨 (2026-09-14)**

> 이 색인은 **더 이상 갱신되지 않는다.** 아래 8건은 오류 사전
> (`ohvis_wiki_error_book`)으로 옮겼고, 새 교훈은 거기에 쌓인다.
>
>     scripts/error_book.py match <오류파일|->   # 조사 시작할 때
>     scripts/error_book.py list                 # 등록된 항목
>
> **왜 옮겼나.** 이 파일은 2026-03-06 이후 반년간 한 줄도 늘지 않았는데,
> 그 사이 `.claude/rules/flow-rules.md` 는 계속 "작업 전 INDEX.md 확인" 을
> 지시하고 있었다. 죽은 색인을 가리키는 규칙은 규칙 자체를 무시하게 만든다.
> 오류 사전은 DB 한 벌이라 모든 서버·세션이 같은 것을 보고, 실패 지점에서
> 자동으로 조회된다.
>
> | 옛 번호 | 새 키 |
> |---|---|
> | L-001 | `infra.watchdog_service_name_mismatch` |
> | L-002 | `infra.disk_full_write_cascade` |
> | L-003 | `infra.docker_image_accumulation` |
> | L-004 | `api.oauth_token_expiry_no_prerefresh` |
> | L-005 | `api.saas_no_webhook_needs_ack_retry` |
> | L-006 | `deploy.no_post_deploy_monitoring` |
> | L-007 | `data.error_log_no_hash_dedup` |
> | L-008 | `patterns.ack_retry_for_external_messages` |
>
> 원문은 아래에 그대로 둔다 — 이관본에 없는 맥락이 있을 수 있다.

---

## (원문 보존)
## infra (서버·디스크·Docker·네트워크)
- L-001: Watchdog 서비스명 불일치 오탐 폭주 [AADS-117] → infra/L-001_watchdog-false-positive.md
- L-002: 디스크 100% 도달 → PostgreSQL write 실패 연쇄 [AADS] → infra/L-002_disk-full-cascade.md
- L-003: Docker image 누적 → 주간 prune 필요 [AADS] → infra/L-003_docker-prune-schedule.md

## api (외부 API·토큰·웹훅·타임아웃)
- L-004: API 토큰 만료 전 자동갱신 필수 [KIS 9건] → api/L-004_token-refresh-pattern.md
- L-005: 외부 SaaS 웹훅 미지원 시 ACK+재전송 [GenSpark] → api/L-005_genspark-no-webhook.md

## deploy (배포·검증·롤백)
- L-006: 배포 후 5분 모니터링 의무 [AADS T-038 903건] → deploy/L-006_verify-before-next-task.md

## data (DB·마이그레이션·로깅)
- L-007: 에러 로그 해시 기반 중복 방지 [Watchdog] → data/L-007_error-hash-dedup.md

## patterns (재사용 코드 패턴)
- L-008: ACK+Retry 패턴 (외부 메시지 확인) [Bridge.py] → patterns/L-008_ack-retry-pattern.md
