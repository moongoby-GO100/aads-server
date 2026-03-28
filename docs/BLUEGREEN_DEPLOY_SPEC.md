# AADS Blue-Green 무중단 배포 기술 명세서
_최종 갱신: 2026-03-28_

---

## 1. 개요

- **목적**: 서비스 중단 없는 배포 — 이미지 재빌드가 필요한 경우에도 사용자 요청을 끊지 않는다.
- **대상**: aads-server (FastAPI 0.115, 포트 8100/8102)
- **도메인**: aads.newtalk.kr
- **운영 서버**: 68.183.183.11 (서버 68)

---

## 2. 아키텍처

```
[Client]
   │
   ▼
[nginx :80/443]
   ├── /api/v1/conversations → [Conversations :8101]  (독립 서버 — 전환 대상 아님)
   ├── /api/v1/memory        → [Memory :18085]         (독립 서버 — 전환 대상 아님)
   ├── /api/v1/              → [Blue :8100]  ◀─ 상시 활성 (전환 대상)
   │                         → [Green :8102] ◀─ 배포 시 임시 스테이징
   ├── /api/                 → [Legacy API :8001]       (독립 — 전환 대상 아님)
   └── /                     → [Next.js Dashboard :3100] (독립 — 전환 대상 아님)
```

**핵심 원칙**: Blue(포트 8100)가 항상 최종 활성 인스턴스다. Green은 배포 중 스테이징 역할만 수행하며, 배포 완료 후 Swing-back으로 Blue로 복귀하고 제거된다.

---

## 3. 서비스 정의

| 항목 | Blue (aads-server) | Green (aads-server-green) |
|---|---|---|
| 컨테이너 이름 | `aads-server` | `aads-server-green` |
| 포트 바인딩 | `8100→8080` | `8102→8080` |
| `restart` 정책 | `always` | `"no"` |
| Docker Compose profile | (기본 — 항상 포함) | `green` |
| 메모리 한도 (`mem_limit`) | `2G` (2,147,483,648 bytes) | `2G` (동일) |
| 코드 볼륨 | `app:/app/app:rw` (공유) | `app:/app/app:rw` (동일 볼륨) |
| 역할 | 상시 활성 인스턴스 | 배포 시 임시 스테이징, 완료 후 제거 |

**볼륨 공유 의미**: Blue와 Green은 `app` named volume을 공유하므로, `code` 모드처럼 파일을 직접 수정하면 두 인스턴스에 즉시 반영된다. `bluegreen` 모드는 이미지 재빌드가 필요한 변경(Dockerfile, requirements.txt 등)에 사용한다.

**`restart: always` vs `restart: "no"` 의미**: Docker 데몬 재시작(서버 재부팅 포함) 시 Blue만 자동 기동된다. Swing-back이 항상 Blue를 활성으로 복귀시키는 이유다.

---

## 4. 배포 모드 비교

| 모드 | 중단 시간 | 용도 | 명령어 |
|---|---|---|---|
| `code` | 수초~수십초 | Python 코드만 수정 (볼륨 마운트 즉시 반영 후 프로세스 재시작) | `deploy.sh code` |
| `reload` | ~10초 | 긴급 프로세스 재시작 | `deploy.sh reload` |
| `build` | 1~3분 | Dockerfile/패키지 변경, 이미지 재빌드 (서비스 일시 중단) | `deploy.sh build` |
| `bluegreen` | **0초** | 이미지 재빌드 + 무중단 — Swing-back으로 Blue 복귀까지 완전 자동화 | `deploy.sh bluegreen` |

---

## 5. Bluegreen 배포 프로세스 (7단계)

### Phase 0: 사전 검증

의존 컨테이너 상태 확인 후 이상이 있으면 자동 복구를 시도한다.

- 대상: `aads-postgres`, `redis`, `socket-proxy`, `litellm`
- 스트리밍 플레이스홀더 정리 (미완료 SSE 세션 처리)
- Python 구문 검사 (`python3 -m py_compile`)
- import 검증 (`python3 -c "import app.main"`)
- 검증 실패 시: 배포 즉시 차단, 텔레그램 알림 발송

### Phase 0.5: 배포 락 획득

```
/tmp/aads-deploy.lock
```

파일이 이미 존재하면 중복 배포로 간주하고 즉시 종료한다. 배포가 비정상 종료된 경우 수동으로 삭제한다.

### Phase 1-①: 비활성 인스턴스 빌드

1. nginx 설정에서 현재 활성 포트 감지:
   ```bash
   grep 'location /api/v1/ {' /etc/nginx/conf.d/aads.conf -A2 | grep proxy_pass
   ```
2. 활성이 Blue(8100)면 → Green(8102)을 빌드 대상으로 선택
3. 활성이 Green(8102)이면 → Blue(8100)를 빌드 대상으로 선택 (비정상 상태)
4. 대상이 Green인 경우: `docker compose --profile green build aads-server-green`
5. 대상이 Blue인 경우: `docker compose build aads-server`
6. 빌드 완료 후 컨테이너 시작

### Phase 1-②: 헬스체크 (최대 90초)

- 3초 간격으로 `/api/v1/health` 엔드포인트 폴링
- 연속 성공 확인 시 다음 단계 진행
- 90초 초과 시:
  - 새 컨테이너 `docker stop` + `docker rm`
  - 텔레그램 긴급 알림 (CEO 수신)
  - `exit 1` — 기존 서비스는 영향 없음

### Phase 1-③: nginx 트래픽 전환

```bash
# 예: Blue → Green 전환
sed -i 's|proxy_pass http://127.0.0.1:8100/api/v1/;|proxy_pass http://127.0.0.1:8102/api/v1/;|g' \
    /etc/nginx/conf.d/aads.conf

# 설정 검증
nginx -t

# 검증 실패 시: 즉시 sed 역방향으로 복원 + 새 컨테이너 중지
# 검증 성공 시:
systemctl reload nginx   # 무중단 리로드 (기존 커넥션 유지)

# 라이브 설정 → 소스 파일 동기화
cp /etc/nginx/conf.d/aads.conf /root/aads/aads-server/nginx-aads.conf
```

`nginx -t` 실패 시 자동 롤백 후 `exit 1`.

### Phase 1-④: 전환 검증

- 2초 대기 (커넥션 드레인 여유)
- 새 포트로 재차 헬스체크
- 실패 시:
  - `sed`로 nginx 포트 원복
  - `systemctl reload nginx`
  - 새 컨테이너 `docker stop`

### Phase 1-⑤: 이전 인스턴스 종료

```bash
docker stop --time 30 <이전_컨테이너>   # SIGTERM 후 30초 대기 (Graceful Shutdown)
# Green이었으면 docker rm 추가
```

### Phase 1-⑥: Swing-back (Green→Blue 복귀)

> 이 단계는 Green이 활성화된 경우(Blue→Green 전환 후)에만 실행된다.

1. Blue 이미지를 Green과 동일 빌드로 재빌드
2. Blue 컨테이너 시작
3. Blue 헬스체크 통과 확인
4. nginx를 Green(8102) → Blue(8100)로 복원
5. Green 컨테이너 중지 + 제거 (`docker rm aads-server-green`)

**Swing-back의 목적**: `restart: always`인 Blue가 항상 최종 활성 인스턴스여야 Docker 데몬 재시작(서버 재부팅) 시 자동 복구가 보장된다.

### Phase 2~6: 후속 검증

| Phase | 내용 | 실패 시 |
|---|---|---|
| Phase 2 | Health Check 30초 연속 모니터링 | 텔레그램 알림 |
| Phase 3 | DB 스키마 검증 (필수 컬럼 존재 여부) | 누락 컬럼 자동 생성 시도 |
| Phase 4 | 채팅 기능 E2E 테스트 | 텔레그램 알림 |
| Phase 5 | LLM 연결 테스트 (Anthropic + LiteLLM 프록시) | 텔레그램 알림 |
| Phase 6 | 프론트엔드 QA (대시보드 변경 시에만 실행) | 텔레그램 알림 |

---

## 6. nginx 라우팅 테이블

| Location | 대상 포트 | 서비스 | Blue-Green 전환 영향 |
|---|---|---|---|
| `/api/v1/conversations` | `8101` | Conversations 독립 서버 | 없음 |
| `/api/v1/memory` | `18085` | Memory 독립 서버 | 없음 |
| `/api/v1/` | `8100` (Blue 활성 시) | AADS Server | **전환 대상** |
| `/api/` | `8001` | Legacy API | 없음 |
| `/` | `3100` | Next.js Dashboard | 없음 |

**주의**: `/api/v1/conversations`와 `/api/v1/memory`는 더 구체적인 location으로 먼저 매칭되므로, `/api/v1/` 전환이 이들에 영향을 주지 않는다.

---

## 7. 안전장치

| # | 장치 | 설명 |
|---|---|---|
| 1 | 배포 락파일 | `/tmp/aads-deploy.lock` — 동시 배포 차단 |
| 2 | 의존성 자동 복구 | `postgres` / `redis` 다운 감지 시 자동 `docker compose up -d` |
| 3 | 코드 검증 게이트 | Python 구문 + import 실패 시 Phase 0에서 배포 차단 |
| 4 | 헬스체크 게이트 (1차) | 신규 컨테이너 90초 내 미통과 → 자동 컨테이너 제거 |
| 5 | `nginx -t` 검증 | 설정 오류 감지 시 포트 복원 후 즉시 롤백 |
| 6 | 전환 후 재검증 (2차) | 트래픽 전환 완료 후 추가 헬스체크 |
| 7 | Swing-back | 배포 완료 후 항상 Blue 활성으로 복귀 (daemon-restart 안전 보장) |
| 8 | Graceful Shutdown | `docker stop --time 30` — SIGTERM 후 30초 대기 |
| 9 | 텔레그램 알림 | 배포 성공/실패 모두 CEO에게 즉시 알림 |
| 10 | DB 스키마 검증 | Phase 3에서 누락 컬럼 자동 감지 + 생성 시도 |

---

## 8. 운영 가이드

### 모드 선택 기준

| 변경 사항 | 권장 모드 | 이유 |
|---|---|---|
| Python 코드만 수정 (볼륨 마운트 반영) | `code` 또는 `reload` | 이미지 재빌드 불필요, 빠름 |
| `requirements.txt` / `Dockerfile` 변경 | `build` 또는 `bluegreen` | 이미지 재빌드 필수 |
| 서비스 중단 불가 상황 (피크 타임 등) | `bluegreen` | 0초 다운타임 |
| 긴급 핫픽스 (빠른 반영 우선) | `reload` | ~10초, 가장 빠름 |

### 배포 체크리스트

1. `git status` — 변경 파일 확인
2. `git diff` — 의도한 변경인지 검증
3. `deploy.sh bluegreen` 실행
4. 텔레그램 알림 확인 (성공 메시지)
5. `curl https://aads.newtalk.kr/api/v1/health` — 외부 접근 확인
6. 5분간 에러 로그 모니터링: `docker exec aads-server supervisorctl tail -f aads-api`

### 수동 롤백

nginx를 강제로 Blue(8100)로 복원하는 절차:

```bash
# 1. nginx 포트 강제 전환 (Green → Blue)
sed -i 's|proxy_pass http://127.0.0.1:8102/api/v1/;|proxy_pass http://127.0.0.1:8100/api/v1/;|g' \
    /etc/nginx/conf.d/aads.conf
nginx -t && systemctl reload nginx

# 2. Green 정리
docker stop aads-server-green && docker rm aads-server-green

# 3. Blue 상태 확인
docker exec aads-server supervisorctl status

# 4. 헬스체크
curl -s http://127.0.0.1:8100/api/v1/health | python3 -m json.tool
```

### 트러블슈팅

| 증상 | 원인 | 해결 |
|---|---|---|
| Phase 0 실패 (코드 검증) | Python 구문 오류 또는 import 오류 | `python3 -m py_compile <파일>` 로 오류 위치 확인 후 수정 |
| 헬스체크 90초 실패 | 컨테이너 기동 지연 또는 크래시 | `docker logs aads-server-green --tail 100` |
| `nginx -t` 실패 | `sed` 치환 결과 문법 오류 | 자동 롤백됨; `/etc/nginx/conf.d/aads.conf` 수동 검토 |
| 전환 후 502 | 신규 인스턴스 크래시 | 자동 롤백됨; `docker logs aads-server-green` 확인 |
| 배포 락 충돌 | 이전 배포 비정상 종료로 락 잔존 | `rm /tmp/aads-deploy.lock` 후 재시도 |
| Swing-back 실패 | Blue 빌드/기동 오류 | Green이 활성인 채로 유지됨 (서비스 영향 없음); Blue 수동 복구 |
| DB 스키마 검증 실패 | 마이그레이션 누락 | Phase 3 로그 확인 후 `migrations/` 스크립트 수동 실행 |

---

## 9. 메모리 설정

| 항목 | 값 |
|---|---|
| 물리 메모리 한도 (`mem_limit`) | `2G` (2,147,483,648 bytes) |
| 스왑 (`memswap_limit` 미지정 = 물리 × 2) | 4G 합계 (물리 2G + 스왑 2G) |
| 호스트 스왑 여유 | ~14G |
| 현재 사용량 (정상) | ~610MB |
| 예상 피크 사용량 | ~1.2G |
| Blue / Green 동시 기동 시 최대 | ~2.4G (Swing-back 중 일시적) |

---

## 10. 파일 위치 참조

| 파일 | 경로 |
|---|---|
| 배포 스크립트 | `/root/aads/aads-server/deploy.sh` |
| nginx 설정 (운영) | `/etc/nginx/conf.d/aads.conf` |
| nginx 설정 (소스) | `/root/aads/aads-server/nginx-aads.conf` |
| Docker Compose (서버) | `/root/aads/aads-server/docker-compose.prod.yml` |
| Docker Compose (대시보드) | `/root/aads/aads-dashboard/docker-compose.yml` |
| 배포 락파일 | `/tmp/aads-deploy.lock` |

---

## 11. 변경 이력

| 날짜 | 변경 내용 |
|---|---|
| 2026-03-28 | 초기 문서 작성. 실제 파일 검증 기반 (nginx diff=0, 메모리 2G, Blue/Green 포트 확인). Swing-back 섹션 추가. |
