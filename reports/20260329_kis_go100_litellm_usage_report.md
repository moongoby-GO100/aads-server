# KIS·GO100 가설엔진·LiteLLM 사용량 점검 보고  
**일시:** 2026-03-29 12:06 KST

## 1. 요약

| 항목 | 결과 |
|------|------|
| KIS 웹앱 소스(`webapp`) 내 가설 엔진 | `HypothesisEngine` / `hypothesis` 코드 **미포함** (규칙 기반 시그널 위주) |
| GO100·KIS와의 관계 | 동일 제품 계열 표기·모듈 공유(주석·리포트에 GO100 다수); **가설 파이프라인은 별도 배포/경로**로 운영되는 것으로 추정 |
| Claude(Anthropic) 트래픽 | **LiteLLM 프록시 로그에 `211.188.51.113` → `POST /v1/messages` 등 확인** → 211 서버 클라이언트가 프록시 경유로 Anthropic Messages API 사용 중 (가설·CLI·기타 포함 가능) |
| 사용량 API | `DISABLE_SPEND_LOGS=true` + DB 미연결로 `/spend/logs` **미사용** |
| 추가한 점검 수단 | 호스트 스크립트 `scripts/litellm_docker_usage_report.py` (Docker 로그 파싱) |

## 2. 오늘 10:00 KST ~ 조사 시점 Claude·LiteLLM 사용량

### 2-1. 컨테이너 **재생성 이전** (로그 보존 시점, 약 12:01 KST 전후)

`docker logs aads-litellm --since 2026-03-29T01:00:00Z` (10:00 KST) 기준 **부분 문자열 카운트**:

- `POST /v1/messages`: **249**
- `POST /v1/chat/completions`: **144**
- 응답 줄에 ` 429 ` 포함: **248**
- `rate_limit` 포함: **483**
- 로그에 `anthropic` 언급: **1868** (스택·에러 포함)
- 로그에 `claude` 언급: **110**

**해석:** Anthropic Messages·채팅 호출이 다수이며, **429 / rate_limit 비율이 높아** 해당 구간에 **한도·레이트리밋 이슈**가 있었음.

### 2-2. 컨테이너 **재생성 이후** (12:04 KST 이후)

Docker는 **컨테이너 단위 로그**라 재생성 시 이전 버퍼가 비어, 동일 `--since`로도 **거의 집계되지 않음**.

`litellm_docker_usage_report.py` 실행 결과(12:05 KST):

- `/v1/messages` POST: **1**, HTTP **429**: **1**

→ **오전 10시~정오 구간의 정밀 누적은 2-1 스냅샷을 기준**으로 보고하고, 이후는 새 컨테이너 로그만 반영됨.

## 3. LiteLLM DB·Spend 로그 반영 시도

- `DATABASE_URL` + `DISABLE_SPEND_LOGS=false`로 전환 시 **AADS 공용 Postgres `public` 스키마에서 Prisma 마이그레이션 충돌** (`LiteLLM_DailyUserSpend` 등).
- **권장:** LiteLLM 전용 DB(또는 별도 스키마·인스턴스) + 공식 가이드대로 마이그레이션 후 `/spend/logs`·UI 사용.
- 현재는 **기존 안정 설정으로 복귀** (`DISABLE_SPEND_LOGS=true`, DB 미연결).

## 4. 추가된 점검 기능

```bash
python3 /root/aads/aads-server/scripts/litellm_docker_usage_report.py --since-kst '2026-03-29 10:00:00'
```

- Docker `aads-litellm` 로그만 사용 (DB 불필요).
- **주의:** 컨테이너 재생성 시 과거 구간 로그는 유실될 수 있음 → 장기 집계는 전용 DB 또는 중앙 로깅 권장.

## 5. 적용·배포

| 구분 | 상태 |
|------|------|
| 소스 | `scripts/litellm_docker_usage_report.py` 추가, compose는 **기존과 동일 정책 유지** |
| LiteLLM 컨테이너 | `docker compose up -d aads-litellm`로 기동 확인, readiness **healthy** (`db: Not connected` = 설정상 정상) |

## 6. 후속 체크

- [ ] LiteLLM **전용** Postgres 프로비저닝 후 spend 로그 재활성화.
- [ ] 211에서 가설 데몬·Claude Code가 동시에 프록시를 쓰는지 프로세스·환경변수로 구분.
- [ ] Anthropic 대시보드와 병행해 일일 토큰 상한 점검.
