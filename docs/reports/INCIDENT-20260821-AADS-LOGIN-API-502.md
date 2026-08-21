# AADS 로그인/API 502 장애 사후 보고서

- 장애 일자: 2026-08-21 KST
- 상태: 해결 완료
- 영향 서비스: `aads.newtalk.kr` 로그인 이후 인증·채팅 API
- 운영 서버: `contabo116` (`5.104.86.116`)
- 관련 커밋: `d613ae74`, `3a3beabd`
- 운영 롤백 백업: `/root/aads/backups/aads-login-incident-20260821-1018`

## 1. 요약

Dashboard와 TLS는 정상이라 로그인 페이지 자체는 HTTP 200을 반환했지만, Nginx가 가리키던 Blue API 슬롯의 `aads-api` Supervisor 자식 프로세스가 `STOPPED` 상태로 남아 `/api/v1/*` 요청이 HTTP 502를 반환했다.

직접 원인은 Supervisor process-control RPC로 시작된 API 종료다. 장기 SSE/WebSocket 연결 drain 중 graceful shutdown이 완료되지 않아 약 2분 뒤 SIGKILL됐고, Supervisor가 이를 "예상된 stop"으로 분류해 `autorestart=true`를 적용하지 않았다. 컨테이너 PID 1은 계속 실행 중이어서 Docker restart policy도 발동하지 않았다.

장애 발생 전에 Supervisor RPC 감사가 구성돼 있지 않았으므로 과거 명령의 정확한 호출 주체는 확정할 수 없다. 다만 OOM, Docker 컨테이너 stop/restart, 공식 배포 실행은 같은 시각에 확인되지 않았다.

## 2. 사용자 영향

| 항목 | 장애 당시 상태 | 영향 |
|---|---|---|
| DNS/TLS/Cloudflare | 정상 | 도메인 접속과 인증서에는 문제 없음 |
| `/login` | HTTP 200 | 로그인 화면은 표시됨 |
| `/api/v1/health` | HTTP 502 | API backend 연결 실패 |
| `/api/v1/auth/me` | HTTP 502 | 로그인 상태 확인 실패 |
| 채팅 API | backend 접근 불가 | 로그인 이후 채팅 사용 불가 |

## 3. 타임라인

모든 시각은 KST다.

| 시각 | 사건 |
|---|---|
| 09:11:17 | Blue 컨테이너 기동 후 정상 health 확인 |
| 09:33:33 | Supervisor가 `waiting for aads-api to stop` 기록, Uvicorn graceful shutdown 시작 |
| 09:35:34 | 연결 drain 미완료 상태에서 `aads-api` SIGKILL 종료 |
| 09:35 이후 | Supervisor 상태 `STOPPED`, Blue 컨테이너 자체는 계속 `running` |
| 09:51:27 | `supervisorctl start aads-api`로 Blue API 복구 |
| 09:51:47 | 외부 `/api/v1/health` HTTP 200 확인 |
| 10:25~10:40 | watchdog, Nginx failover, 명령 차단, 배포 세대 보호 적용 |
| 10:41:49 | 로그인 200, health 200, Blue/Green 모두 healthy 최종 확인 |

## 4. 근본 원인

### 4.1 직접 원인

Supervisor에 API stop/restart 계열 process-control RPC가 전달됐다. `supervisord.log`에는 Supervisor 자체 SIGTERM이 없고 `aads-api`만 stop 대기 상태로 진입했으므로 컨테이너 종료가 아닌 자식 프로세스 제어 요청으로 판단한다.

### 4.2 API가 자동 복구되지 않은 이유

1. SSE/WebSocket 연결 때문에 graceful shutdown이 종료 제한시간까지 지속됐다.
2. Supervisor가 SIGKILL로 프로세스를 종료했다.
3. 해당 종료는 Supervisor가 요청한 expected stop이므로 `autorestart=true`가 재기동하지 않았다.
4. 컨테이너 PID 1인 `supervisord`는 실행 중이어서 Docker `restart: always`가 동작하지 않았다.
5. 기존 감시기는 컨테이너 `.State.Status=running`만 보거나 비활성 상태였으며 Supervisor 자식 상태를 복구 조건으로 사용하지 않았다.

### 4.3 502가 지속된 이유

1. 활성 upstream은 Blue `:8100`에 고정돼 있었다.
2. 활성 서버가 `max_fails=0`이라 Nginx passive failure 처리가 비활성화돼 있었다.
3. `/api/v1/`는 SSE 보호를 위해 `proxy_next_upstream off`였으므로 현재 요청을 Green으로 재시도하지 않았다.
4. 정상 Green `:8102`가 있어도 자동 전환하는 호스트 제어기가 없었다.

### 4.4 호출 주체 판정 범위

- 확인됨: Supervisor process-control RPC 계열 요청
- 배제됨: OOM, Docker 컨테이너 kill/stop/restart, 같은 시각 공식 `deploy.sh` 실행
- 확정 불가: RPC를 호출한 정확한 프로세스·세션. 당시 Supervisor RPC 감사 및 process accounting 부재 때문

## 5. 즉시 복구

1. Blue 컨테이너 내부 Supervisor 상태가 `STOPPED`임을 확인했다.
2. `supervisorctl start aads-api`만 수행했다. 컨테이너 재생성이나 DB 변경은 하지 않았다.
3. Blue 내부 health, 외부 health, 로그인 페이지, 비로그인 `auth/me=401`을 순서대로 검증했다.

## 6. 영구 조치

### 6.1 호스트 watchdog

- 파일: `scripts/aads_api_watchdog.sh`
- systemd: `aads-api-watchdog.service`, `aads-api-watchdog.timer`
- 주기: 15초
- 판정: 컨테이너 running + Supervisor `RUNNING` + `/health/live`
- 2회 연속 실패 시 정상 standby로 원자 전환
- standby도 불가하고 현재 자식이 `STOPPED`일 때만 `start`
- 실행 중 프로세스 restart 및 무한 복구 루프 금지
- 배포 락과 Nginx 공통 락 존중

### 6.2 Nginx failover

- 활성/대기 슬롯 모두 `max_fails=1 fail_timeout=10s`
- `/api/v1/health`, `/api/v1/auth/me`만 안전한 upstream retry 허용
- 일반 채팅 SSE와 WebSocket은 한 upstream에 계속 고정
- 설정 변경 전 `nginx -t`, reload 실패 시 자동 롤백

### 6.3 self-control 차단

애플리케이션 도구에서 다음 직접 제어를 차단했다.

- `supervisorctl start|stop|restart|signal|shutdown`의 `aads-api` 대상 호출
- API 컨테이너 `docker stop|kill|rm`
- `docker exec`를 통한 API 자식 kill
- `nginx -s stop|quit`, `systemctl stop|restart nginx`
- `deploy_safe restart-single`의 Blue/Green API 슬롯 대상 호출

API 변경은 hot-reload 또는 `deploy.sh bluegreen`만 사용한다.

### 6.4 배포 세대 보호

- 각 배포에 `.deploy_generation` 값을 발급한다.
- 지연 drain/standby sync 작업은 세대 ID와 현재 active slot을 재검증한다.
- 더 최신 배포가 시작됐거나 대상 슬롯이 active로 바뀌면 이전 백그라운드 작업은 아무 변경 없이 종료한다.
- Nginx 전환은 direct health와 Nginx 경유 health를 모두 검증한 뒤 성공 처리한다.

### 6.5 감사와 레거시 정리

- 자동 제어 기록: `/var/log/aads-control-audit.jsonl`
- 배포 이력: PostgreSQL `deploy_history`
- 애플리케이션 제어 요청: 구조화 애플리케이션 로그
- 기존 `container_watchdog.sh` 데몬 종료
- 기존 cron wrapper는 새 systemd timer 활성 시 no-op
- 레거시 `watchdog-host.sh`는 새 watchdog compatibility entrypoint로 변경

## 7. 검증 결과

| 검증 | 결과 |
|---|---|
| Shell 구문 검사 | 통과 |
| Python `py_compile` | 통과 |
| Nginx 전체 설정 검사 | 통과 |
| 애플리케이션 hot-reload | 67개 모듈 성공 |
| self-control 차단 단위시험 | 통과 |
| 격리 장애 시뮬레이션 | Green 2회 연속 장애 → Blue 전환 성공 |
| systemd timer | enabled/active, 최근 실행 `Result=success` |
| Blue/Green | 양쪽 `running/healthy`, Supervisor `RUNNING` |
| 외부 로그인 | HTTP 200 |
| 외부 health | HTTP 200 |
| 비로그인 `auth/me` | HTTP 401 정상 |

실제 운영 API를 고의 중단하는 chaos test는 수행하지 않았다. 동일 watchdog을 가짜 Docker/Nginx/Curl 환경에서 실행해 전환 설정, 상태 파일, 감사 로그를 검증했다.

## 8. 운영 대응 절차

```bash
# 외부 상태
curl -fsS https://aads.newtalk.kr/api/v1/health

# 활성 슬롯
cat /root/aads/aads-server/.active_port
cat /root/aads/aads-server/.active_container

# watchdog
systemctl status aads-api-watchdog.timer
journalctl -u aads-api-watchdog.service --since "10 minutes ago" --no-pager

# 양쪽 슬롯
docker exec aads-server supervisorctl status aads-api
docker exec aads-server-green supervisorctl status aads-api
curl -fsS http://127.0.0.1:8100/health/live
curl -fsS http://127.0.0.1:8102/health/live

# 제어 감사
tail -n 100 /var/log/aads-control-audit.jsonl
```

수동 `supervisorctl restart aads-api`와 컨테이너명 기준 강제 stop은 금지한다. active/standby 판정 없이 슬롯을 조작하면 진행 중 SSE 유실 또는 현재 active 중단이 발생할 수 있다.

## 9. 완료 기준

- 외부 로그인과 API health가 정상이다.
- Blue/Green 양쪽 슬롯이 healthy다.
- watchdog timer가 enabled/active이고 반복 실행이 성공한다.
- 직접 self-control 명령이 차단된다.
- 지연 standby 작업이 세대·소유권을 검증한다.
- 운영 변경이 `origin/main`에 반영되고 본 문서가 연결돼 있다.
