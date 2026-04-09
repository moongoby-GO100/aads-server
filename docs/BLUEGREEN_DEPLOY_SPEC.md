# AADS Blue-Green 무중단 배포 기술 명세서
_최종 갱신: 2026-04-09_

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
   ├── /api/v1/              → upstream aads_api        (전환 대상 ★)
   │                            ├── Blue  :8100  ◀─ active
   │                            └── Green :8102  ◀─ backup
   ├── /api/v1/pc-agent/ws/  → upstream aads_api_pc_agent_ws (전환 대상 ★)
   ├── /api/                 → [Legacy API :8001]       (독립 — 전환 대상 아님)
   └── /                     → [Next.js Dashboard :3100] (독립 — 전환 대상 아님)
```

### 핵심 메커니즘: nginx upstream + backup 키워드

트래픽 전환은 `/etc/nginx/conf.d/aads-upstream.conf`에서 `backup` 키워드를 조작하여 수행한다.
`/etc/nginx/conf.d/aads.conf`는 `proxy_pass http://aads_api/...`로 upstream을 참조하므로 **직접 수정하지 않는다**.

```nginx
# /etc/nginx/conf.d/aads-upstream.conf
upstream aads_api {
    zone aads_api 64k;
    server 127.0.0.1:8100 max_fails=3 fail_timeout=30s;          # ← active
    server 127.0.0.1:8102 max_fails=3 fail_timeout=30s backup;   # ← standby
    keepalive 32;
    least_conn;
}
```

**전환 시**: `backup` 키워드를 스왑한 후 `systemctl reload nginx`.

---

## 3. 서비스 정의

| 항목 | Blue (aads-server) | Green (aads-server-green) |
|---|---|---|
| 컨테이너 이름 | `aads-server` | `aads-server-green` |
| 포트 바인딩 | `8100→8080` | `8102→8080` |
| `restart` 정책 | `always` | `"no"` |
| Docker Compose profile | (기본 — 항상 포함) | `green` |
| 메모리 한도 (`deploy.resources.limits.memory`) | `2G` | `2G` |
| 코드 볼륨 | `app:/app/app:rw` (공유) | `app:/app/app:rw` (동일 볼륨) |
| 역할 | 상시 활성 인스턴스 | 배포 시 임시 스테이징, 완료 후 종료 |

**`restart: "no"` 의미**: Docker 데몬 재시작(서버 재부팅) 시 Green은 자동 기동되지 않는다. Blue만 항상 자동 복구된다.

---

## 4. 배포 모드 비교

| 모드 | 중단 시간 | 용도 | 명령어 |
|---|---|---|---|
| `code` | 수초~수십초 | Python 코드만 수정 (볼륨 마운트 즉시 반영 후 프로세스 재시작) | `deploy.sh code` |
| `reload` | ~10초 | 긴급 프로세스 재시작 | `deploy.sh reload` |
| `build` | 1~3분 | Dockerfile/패키지 변경, 이미지 재빌드 (서비스 일시 중단) | `deploy.sh build` |
| `bluegreen` | **0초** | 이미지 재빌드 + 무중단 — upstream 전환으로 완전 자동화 | `deploy.sh bluegreen` |

---

## 5. Bluegreen 배포 프로세스 (7단계)

### Phase 0: 사전 검증

- 의존 컨테이너 상태 확인 후 이상이 있으면 자동 복구 시도
- 대상: `aads-postgres`, `redis`, `socket-proxy`, `litellm`
- 스트리밍 플레이스홀더 정리
- Python 구문 검사 + import 검증
- 검증 실패 시: 배포 즉시 차단, 텔레그램 알림 발송

### Phase 0.5: 배포 락 획득

```
/tmp/aads-deploy.lock
```

### Phase 1-①: 비활성 인스턴스 빌드

1. **upstream 설정에서 현재 활성 포트 감지**:
   ```bash
   CURRENT_PORT=$(grep "server 127.0.0.1:" /etc/nginx/conf.d/aads-upstream.conf | grep -v backup | head -1 | grep -oP '127\.0\.0\.1:\K[0-9]+')
   ```
2. 활성이 Blue(8100)면 → Green(8102)을 빌드 대상으로 선택
3. 활성이 Green(8102)이면 → Blue(8100)를 빌드 대상으로 선택
4. `docker compose --profile green up -d --build --no-deps aads-server-green`

### Phase 1-②: 헬스체크 (최대 90초)

- 3초 간격으로 `/api/v1/health` 엔드포인트 폴링
- 90초 초과 시: 새 컨테이너 `docker stop` + `docker rm`, 텔레그램 긴급 알림

### Phase 1-③: upstream 트래픽 전환

```bash
# 예: Blue(8100) → Green(8102) 전환
UPSTREAM_CONF="/etc/nginx/conf.d/aads-upstream.conf"
cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.pre_deploy"

# Green에서 backup 제거 (활성화)
sed -i 's/server 127.0.0.1:8102 max_fails=3 fail_timeout=30s backup;/server 127.0.0.1:8102 max_fails=3 fail_timeout=30s;/g' "$UPSTREAM_CONF"
# Blue에 backup 추가 (대기)
sed -i 's/server 127.0.0.1:8100 max_fails=3 fail_timeout=30s;/server 127.0.0.1:8100 max_fails=3 fail_timeout=30s backup;/g' "$UPSTREAM_CONF"

nginx -t                     # 설정 검증
systemctl reload nginx       # 무중단 리로드
```

`nginx -t` 실패 시: `cp pre_deploy` 복원 후 `exit 1`.

### Phase 1-④: 전환 검증

- 2초 대기 후 새 포트 헬스체크
- 실패 시: upstream 복원 (`cp pre_deploy`) + nginx reload + 새 컨테이너 stop

### Phase 1-⑤: 이전 인스턴스 지연 종료 (SSE drain)

```bash
# 2분 후 이전 컨테이너 종료 (SSE 연결 완료 대기)
(sleep 120; docker stop --time 30 "$OLD_CONTAINER") &
disown
```

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

| Location | 대상 | 서비스 | Blue-Green 전환 영향 |
|---|---|---|---|
| `/api/v1/conversations` | `127.0.0.1:8101` | Conversations 독립 서버 | 없음 |
| `/api/v1/memory` | `127.0.0.1:18085` | Memory 독립 서버 | 없음 |
| `/api/v1/pc-agent/ws/` | `upstream aads_api_pc_agent_ws` | WebSocket | **전환 대상** |
| `/api/v1/` | `upstream aads_api` | AADS Server | **전환 대상** |
| `/api/` | `127.0.0.1:8001` | Legacy API | 없음 |
| `/` | `127.0.0.1:3100` | Next.js Dashboard | 없음 |

---

## 7. 안전장치

| # | 장치 | 설명 |
|---|---|---|
| 1 | 배포 락파일 | `/tmp/aads-deploy.lock` — 동시 배포 차단 |
| 2 | 의존성 자동 복구 | `postgres` / `redis` 다운 감지 시 자동 복구 |
| 3 | 코드 검증 게이트 | Python 구문 + import 실패 시 Phase 0에서 배포 차단 |
| 4 | 헬스체크 게이트 (1차) | 신규 컨테이너 90초 내 미통과 → 자동 컨테이너 제거 |
| 5 | `nginx -t` 검증 | 설정 오류 시 upstream 복원 후 즉시 롤백 |
| 6 | 전환 후 재검증 (2차) | 트래픽 전환 완료 후 추가 헬스체크 |
| 7 | SSE drain | 이전 컨테이너 120초 후 종료 (진행 중 스트리밍 보호) |
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
6. 5분간 에러 로그 모니터링

### 수동 전환

```bash
# Blue↔Green 수동 전환 (양쪽 모두 실행 중일 때)
bash /root/aads/aads-server/scripts/bluegreen_switch.sh
```

### 수동 롤백

```bash
# 1. upstream을 Blue(8100)로 강제 복원
cp /etc/nginx/conf.d/aads-upstream.conf.pre_deploy /etc/nginx/conf.d/aads-upstream.conf
nginx -t && systemctl reload nginx

# 2. Green 정리
docker stop aads-server-green && docker rm aads-server-green

# 3. Blue 상태 확인
docker exec aads-server supervisorctl status
curl -s http://127.0.0.1:8100/api/v1/health | python3 -m json.tool
```

### 트러블슈팅

| 증상 | 원인 | 해결 |
|---|---|---|
| Phase 0 실패 (코드 검증) | Python 구문/import 오류 | `python3 -m py_compile <파일>` 로 오류 위치 확인 |
| 헬스체크 90초 실패 | 컨테이너 기동 지연 또는 크래시 | `docker logs aads-server-green --tail 100` |
| `nginx -t` 실패 | upstream conf 문법 오류 | 자동 롤백됨; upstream conf 수동 검토 |
| 전환 후 502 | 신규 인스턴스 크래시 | 자동 롤백됨; `docker logs` 확인 |
| 배포 락 충돌 | 이전 배포 비정상 종료로 락 잔존 | `rm /tmp/aads-deploy.lock` 후 재시도 |

---

## 9. 메모리 설정

| 항목 | 값 |
|---|---|
| 물리 메모리 한도 (`deploy.resources.limits.memory`) | `2G` |
| 현재 사용량 (정상) | ~610MB |
| Blue / Green 동시 기동 시 최대 | ~1.2G (배포 중 일시적) |

---

## 10. 파일 위치 참조

| 파일 | 경로 |
|---|---|
| 배포 게이트웨이 | `/root/aads/aads-server/deploy.sh` |
| Blue-Green 전용 배포 | `/root/aads/aads-server/scripts/blue_green_deploy.sh` |
| 수동 전환 스크립트 | `/root/aads/aads-server/scripts/bluegreen_switch.sh` |
| nginx upstream (운영) | `/etc/nginx/conf.d/aads-upstream.conf` |
| nginx 설정 (운영) | `/etc/nginx/conf.d/aads.conf` |
| nginx 설정 (소스) | `/root/aads/aads-server/nginx-aads.conf` |
| Docker Compose (서버) | `/root/aads/aads-server/docker-compose.prod.yml` |
| 활성 포트 상태 | `/root/aads/aads-server/.active_port` |

---

## 11. 변경 이력

| 날짜 | 변경 내용 |
|---|---|
| 2026-03-28 | 초기 문서 작성. 실제 파일 검증 기반. Swing-back 섹션 포함. |
| 2026-04-09 | **아키텍처 전환**: aads.conf 직접 포트 sed → aads-upstream.conf backup 키워드 조작. Swing-back 제거. Green restart 정책 `"no"` 적용. 스크립트 3종 통합 수정. |
