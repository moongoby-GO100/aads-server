# AADS 인프라 이전: Server68 → Contabo Server5
## 전면 이전 준비 보고서

**작성일**: 2026-05-11 12:00 KST  
**작성자**: AADS PM/CTO AI  
**상태**: Phase 0 완료 — CEO 승인 게이트 대기

---

## 산출물 1: 현황 인벤토리

### 1-1. Docker 컨테이너 (server68 실측)

| 컨테이너 | 이미지 | 포트 | 상태 |
|----------|--------|------|------|
| aads-server (Blue) | aads-server-aads-server | 8100→8080 | ✅ Up 2일 (healthy) |
| aads-server-green | aads-server-aads-server-green | 8102→8080 | ✅ Up 2시간 (healthy) |
| aads-dashboard | aads-server-aads-dashboard | 3100→3100 | ✅ Up 3시간 (healthy) |
| aads-dashboard-green | aads-server-aads-dashboard-green | 3101→3100 | Exited (대기) |
| aads-postgres | pgvector/pgvector:pg15 | 5433→5432 | ✅ Up 6일 (healthy) |
| aads-litellm | ghcr.io/berriai/litellm:main-latest | 4000 | ✅ Up 2주 (healthy) |
| aads-searxng | searxng/searxng:latest | 8888 | ✅ Up 2주 (healthy) |
| aads-redis | redis:7-alpine | 6379 | ✅ Up 2주 (healthy) |
| aads-socket-proxy | tecnativa/docker-socket-proxy | 2375 | ✅ Up 2주 |

### 1-2. Nginx (server68: 호스트 설치 / server5: Docker 컨테이너)

- **server68**: CentOS 호스트 nginx, `/etc/nginx/conf.d/aads.conf` (HTTP + HTTPS)
- **server5**: Docker `aads-nginx` 컨테이너 (`--network host`), HTTP-only
- **SSL**: server68은 Let's Encrypt, server5는 Cloudflare SSL 종료 (HTTP 백엔드)
- **upstream**: Blue-Green (8100/8102 + 3100/3101)

### 1-3. DNS

| 도메인 | 현재 | 비고 |
|--------|------|------|
| aads.newtalk.kr | Cloudflare Proxy → server68 | A 레코드, Cloudflare 프록시 활성 |

### 1-4. Systemd 서비스 (server68)

| 서비스 | 스크립트 경로 | 용도 |
|--------|-------------|------|
| aads-pipeline-runner | /root/aads/aads-server/scripts/pipeline-runner.sh | DB 기반 Claude Code 실행기 |
| aads-telegram-bot | /root/aads/scripts/telegram_bot.py | 텔레그램 승인 봇 |
| aads-watchdog | /root/aads/scripts/watchdog_daemon.py | 서비스 감시 데몬 |
| aads-bridge | /root/aads/scripts/bridge.py | CEO 지시 감지 브리지 |
| aads-api (legacy) | /root/aads/aads-core/.venv/bin/uvicorn | aads-core 레거시 API (포트 8001) |
| aads-dashboard (legacy) | npm start (aads-core/dashboard) | 레거시 대시보드 (포트 3000) |

### 1-5. Legacy 호스트 서비스 (server68 전용)

| 서비스 | 상태 | 이전 필요 |
|--------|------|----------|
| PostgreSQL 9.6 | Running (since Mar 03) | ❌ server68 전용 레거시 |
| php-fpm | Running (since Feb 06) | ❌ server68 전용 레거시 |
| /opt/newtalk/ | 메모리 복원 스크립트 | ❌ server68 전용 |
| /var/www/ (trading, haru 등) | 레거시 웹사이트 | ❌ server68 전용 |

### 1-6. 외부 의존성

| 의존성 | 연결 방식 | 비고 |
|--------|----------|------|
| 서버211 (KIS/GO100) | SSH (id_ed25519) | Runner 원격 실행 |
| 서버114 (SF/NTV2) | SSH (id_rsa) | Runner 원격 실행 |
| GitHub | HTTPS | 코드 push/pull |
| Telegram | HTTPS API | 알림/승인 |
| Anthropic OAuth | ANTHROPIC_AUTH_TOKEN | Claude API |
| Cloudflare | DNS Proxy | SSL 종료 + CDN |
| DO Spaces | S3 API (nyc3) | 이미지 저장소 |

### 1-7. 동기화 현황

| 항목 | 주기 | 방식 | 상태 |
|------|------|------|------|
| 코드 (aads-server) | 5분 | rsync + 변경 감지 핫리로드 | ✅ 정상 |
| 코드 (aads-dashboard) | 5분 | rsync + blue-green 자동 배포 | ✅ 정상 |
| 문서 (aads-docs) | 5분 | rsync | ✅ 정상 |
| DB | 1시간 | pg_dump → atomic swap | ✅ 정상 (38건 lag) |

---

## 산출물 2: 갭 분석 (server68 ↔ server5)

### ✅ 동일/정상

| 항목 | server68 | server5 | 상태 |
|------|---------|---------|------|
| aads-server (Blue/Green) | ✅ healthy | ✅ healthy | 동일 |
| aads-dashboard | ✅ healthy | ✅ healthy | 동일 |
| aads-postgres (pgvector:pg15) | ✅ healthy | ✅ healthy | 동일 |
| aads-litellm | ✅ healthy | ✅ healthy | 동일 |
| aads-searxng | ✅ healthy | ✅ healthy | 동일 |
| aads-redis | ✅ healthy | ✅ healthy | 동일 |
| Node.js | v20.20.0 | v20.20.2 | ≈동일 |
| Claude CLI | 설치됨 | v2.1.138 | ✅ |
| Python | 3.6 (host) | 3.12.3 (host) | server5 우위 |
| 코드 동기화 | 원본 | 5분 rsync | ✅ |
| DB 동기화 | 원본 | 1시간 atomic swap | ✅ |

### ⚠️ 차이/미구성 (Phase 0에서 해결됨)

| 항목 | server68 | server5 | Phase 0 조치 |
|------|---------|---------|-------------|
| Systemd 서비스 | 6개 | 0개 → 4개 | ✅ 4개 복사 완료 |
| /root/aads/scripts/ | 존재 | 미동기화 → 동기화 | ✅ rsync 완료 |
| aads-runner.env | 존재 | 없음 → 복사 | ✅ 복사 완료 |
| Crontab | 활성 ~15개 | 없음 → 10개 | ✅ AADS 전용 설치 |
| SSH 키 (211→S5) | N/A | 미등록 → 등록 | ✅ 등록 완료 |
| SSH 키 (114→S5) | N/A | 미등록 → 등록 | ✅ 등록 완료 |

### ❌ 잔여 갭 (Phase 1~2에서 해결)

| 항목 | 내용 | 해결 단계 |
|------|------|----------|
| SSL 인증서 | server5 HTTP-only (Cloudflare 의존) | Phase 1: Cloudflare Full SSL 설정 확인 |
| Nginx HTTPS 블록 | server5에 없음 (Cloudflare 종료) | Phase 2: DNS 전환 시 Cloudflare 설정 |
| .env 시크릿 | rsync 제외 (보안) | Phase 2: CEO 수동 동기화 |
| 레거시 서비스 | pg9.6, php-fpm server68 전용 | server68 유지보수 모드로 잔류 |
| Genspark 스크립트 | server68 전용 | 이전 불필요 (AADS 독립) |
| 코드 동기화 역전 | 현재 68→S5 | Phase 2: 동기화 방향 전환 or 중단 |

---

## 산출물 3: 이전 체크리스트

### Phase 0: Pre-flight (✅ 완료)

- [x] server5 Docker 컨테이너 전체 healthy 확인 (7/7)
- [x] server5 API health-check 응답 확인
- [x] Systemd 서비스 4개 복사 + daemon-reload
- [x] /root/aads/scripts/ 동기화
- [x] aads-runner.env 복사
- [x] Node.js v20 + Claude CLI v2.1.138 확인
- [x] Crontab 10개 항목 설치
- [x] SSH 키 등록 (server211, server114 → server5)
- [x] SSH 접속 테스트 (211→S5: ✅)

### Phase 1: Dry-run (CEO 승인 후)

- [ ] `.env` 시크릿 동기화 (CEO 직접 또는 승인 후 SCP)
- [ ] Cloudflare SSL 모드 확인 (Full vs Flexible)
- [ ] server5에서 systemd 서비스 시작 테스트 (pipeline-runner 제외)
- [ ] server5에서 대시보드 접속 테스트 (IP 직접)
- [ ] DB 동기화 최종 실행 + 데이터 정합성 확인
- [ ] DNS TTL 단축 (300s → 60s) 사전 적용

### Phase 2: Cutover (CEO 승인 후)

- [ ] DB 최종 동기화 (pg_dump → restore)
- [ ] server68 동기화 cron 중단
- [ ] Cloudflare DNS A 레코드 변경: server68 IP → server5 IP
- [ ] server5 pipeline-runner 시작
- [ ] server5 전체 E2E 테스트
- [ ] server68 유지보수 모드 전환

### Phase 3: Post-cutover

- [ ] server5 모니터링 30분 (API, SSE, Dashboard)
- [ ] server211/114 → server5 Runner 테스트
- [ ] DNS 전파 확인 (dig aads.newtalk.kr)
- [ ] server68 Docker 서비스 중지 (postgres 제외)
- [ ] 롤백 매뉴얼 테스트

---

## 산출물 4: 리스크 평가

### 4-1. 다운타임 윈도우

| 시나리오 | 예상 다운타임 | 조건 |
|---------|-------------|------|
| Cloudflare DNS 전환 | **0~30초** | Cloudflare 프록시 모드, TTL 사전 단축 |
| DB 최종 동기화 | **2~5분** | pg_dump/restore (38K rows) |
| 전체 cutover | **5~10분** | DB sync + DNS + 검증 |

### 4-2. 데이터 일관성

- 현재 DB 동기화: 1시간 주기 atomic swap (38건 lag)
- Cutover 시: 최종 pg_dump 직후 DNS 전환 → 최대 1분 lag
- 위험: cutover 중 server68에 쓰인 채팅 메시지 유실 가능
- 대책: cutover 직전 server68 API 유지보수 모드 활성화 → 쓰기 차단

### 4-3. DNS TTL 단축 계획

1. **D-2일**: Cloudflare TTL 300s → 60s (사전 적용)
2. **D-Day**: DNS A 레코드 변경 → 60초 이내 전파
3. **D+1일**: TTL 복구 (60s → Auto)

### 4-4. 롤백 시나리오

| 시나리오 | 조건 | 조치 | 소요시간 |
|---------|------|------|---------|
| **즉시 롤백** | cutover 후 5분 이내 | DNS를 server68로 복귀 | ~30초 |
| **부분 롤백** | server5 특정 서비스 장애 | 해당 서비스만 server68 경유 | ~2분 |
| **완전 롤백** | server5 전면 장애 | DNS 복귀 + server68 DB 스냅샷 복원 | ~10분 |

---

## 산출물 5: 단계별 실행 계획

### Phase 0: 준비 ✅ 완료 (2026-05-11)

| # | 작업 | 상태 | 비고 |
|---|------|------|------|
| 1 | Nginx 설정 확인 | ✅ | Docker nginx(host모드), Cloudflare SSL |
| 2 | Systemd 서비스 4개 전송 | ✅ | pipeline-runner, telegram-bot, watchdog, bridge |
| 3 | Pipeline Runner 환경 | ✅ | Node 20.20.2 + Claude 2.1.138 + runner.env |
| 4 | Legacy 판단 | ✅ | pg9.6/php-fpm은 server68 잔류 |
| 5 | Crontab 10개 설치 | ✅ | AADS 전용 항목만 |
| 6 | SSH 키 등록 | ✅ | 211+114 → server5 (총 6키) |
| 7 | 스크립트 동기화 | ✅ | /root/aads/scripts/ rsync 완료 |

### Phase 1: Dry-run (예상 2시간)

| # | 작업 | 자율/승인 | 예상시간 |
|---|------|----------|---------|
| 1 | .env 시크릿 동기화 | **CEO 승인** | 10분 |
| 2 | Cloudflare SSL 모드 확인 | 자율 | 5분 |
| 3 | systemd 서비스 시작 테스트 | 자율 | 15분 |
| 4 | server5 E2E 접속 테스트 (IP 직접) | 자율 | 20분 |
| 5 | DNS TTL 사전 단축 | **CEO 승인** | 5분 |
| 6 | DB 정합성 최종 확인 | 자율 | 15분 |

### Phase 2: Cutover (예상 30분)

| # | 작업 | 자율/승인 | 예상시간 |
|---|------|----------|---------|
| 1 | server68 유지보수 모드 활성화 | **CEO 승인** | 1분 |
| 2 | DB 최종 동기화 | 자율 | 5분 |
| 3 | server68 동기화 cron 중단 | 자율 | 1분 |
| 4 | Cloudflare DNS A 레코드 변경 | **CEO 승인** | 1분 |
| 5 | server5 pipeline-runner 시작 | 자율 | 2분 |
| 6 | 전체 E2E 검증 | 자율 | 10분 |

### Phase 3: 검증 및 유지보수 전환 (예상 1시간)

| # | 작업 | 자율/승인 | 예상시간 |
|---|------|----------|---------|
| 1 | 30분 모니터링 (API/SSE/Dashboard) | 자율 | 30분 |
| 2 | 211/114 → server5 Runner 테스트 | 자율 | 10분 |
| 3 | server68 Docker 서비스 중지 | **CEO 승인** | 5분 |
| 4 | DNS TTL 복구 | 자율 | 1분 |
| 5 | HANDOVER.md 갱신 | 자율 | 10분 |

---

## 산출물 6: CEO 승인 게이트

### CEO 승인 필요 항목

| Phase | 결정 항목 | 리스크 | 롤백 가능 |
|-------|----------|--------|----------|
| 1 | .env 시크릿 server5 동기화 | 보안 (SSH 전송) | ✅ 즉시 |
| 1 | DNS TTL 사전 단축 (Cloudflare) | 없음 | ✅ 즉시 |
| 2 | **server68 유지보수 모드 진입** | 채팅 서비스 일시 중단 | ✅ 즉시 해제 |
| 2 | **Cloudflare DNS A 레코드 변경** | 트래픽 전환 | ✅ 30초 롤백 |
| 3 | server68 Docker 서비스 최종 중지 | 롤백 불가 (재시작 필요) | ⚠️ 5분 |

### 자율 수행 가능 항목

- systemd 서비스 시작/테스트
- DB 동기화 실행
- E2E 검증
- Cron 미세 조정
- 모니터링
- HANDOVER 갱신

---

## 부록: Server5 현재 실측 상태 (2026-05-11 12:00 KST)

| 항목 | 실측값 |
|------|--------|
| Docker 컨테이너 healthy | 7/7 |
| API health-check | ✅ 응답 정상 |
| DB pool | available (2/20) |
| 디스크 | 27.5% 사용, **139.7GB 여유** |
| 메모리 | 17.3% |
| Load | 1.63 |
| Crontab | 10개 항목 활성 |
| Systemd 서비스 | 4개 등록 (미시작) |
| SSH 키 | 6개 등록 |
| Node.js | v20.20.2 |
| Claude CLI | v2.1.138 |

---

**→ Phase 1 착수를 위해 CEO 승인을 요청합니다.**
