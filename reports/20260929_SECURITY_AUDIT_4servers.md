# 전 서버 보안 취약점 정밀 점검 보고서 — 2026-09-29

- 점검 시각: 2026-09-29 14:19 ~ 14:27 KST
- 점검자: AADS CTO AI (보안 담당 세션)
- 방식: **읽기 전용**. 설정 변경, 재시작, 공격성 시도(무차별 대입이나 쓰기 요청)는 하지 않았다.
  외부 도달성은 다른 서버에서 TCP 연결과 HTTP GET, Redis PING만으로 확인했다.
- ⚠️ 이 문서에는 공격에 쓸 수 있는 정보가 들어 있다. **공개 GitHub에 커밋하거나 푸시하지 말 것.**

## 0. 요약

| 등급 | 건수 | 대표 항목 |
|---|---|---|
| 🔴 치명(P0) | 5 | 인증 없이 열린 Claude 릴레이 `/stream`(116:8199), IPv6로 노출된 Postgres(116:5433), 비밀번호 없는 Redis(114:6380), 외부에 열린 MySQL(114:3306/3307), 3대 모두 root 계정 비밀번호 SSH 허용 |
| 🟠 높음(P1) | 6 | 인증 없는 Ollama(116:11434), 방화벽 부재(116 ufw 잔재, 114 비활성), .env 권한 665/644, 지원 종료 OS와 커널(114), 확인되지 않은 pg_hba IP, 114의 대량 포트 노출 |
| 🟡 중간(P2) | 4 | 보안 헤더 없음, reboot-required(116·14), X11Forwarding, fail2ban 미설치 |

## 1. 서버별 점검 결과

### 1.1 contabo116 (5.104.86.116) — AADS

| # | 항목 | 실측 | 판정 |
|---|---|---|---|
| A1 | `claude_relay_server.py` :8199 (root 실행) | 0.0.0.0에 바인딩됨. contabo14에서 `/sessions`와 `/leases` 호출 시 200 응답. `/stream`, `/codex-stream`, `/antigravity-stream`, `/oauth/switch` 핸들러에 `_relay_secret_ok()` 호출이 없음. 호출하는 곳은 2978~3038행 6곳뿐이고 account-login 계열만 보호됨 | 🔴 P0 — 외부 요청만으로 CEO OAuth 토큰을 써서 root 권한 Claude CLI를 실행하고 계정을 전환할 수 있을 가능성이 있음(코드 확인. 실제 악용 시험은 하지 않음) |
| A2 | aads-postgres :5433 | IPv4는 iptables와 DOCKER-USER에서 DROP 처리됨(외부 closed). **IPv6 `[::]:5433`은 contabo14에서 `2400:d320:2326:7555::1`로 접속 시 OPEN**. ip6tables에 5433 규칙이 없음 | 🔴 P0 |
| A3 | SSH :22 | `permitrootlogin yes`, `passwordauthentication yes`. 최근 24시간 로그인 실패 5,517건. fail2ban 없음 | 🔴 P0 |
| A4 | Ollama :11434 | `OLLAMA_HOST=0.0.0.0`. 외부에서 `/api/tags` 호출 시 200, cloud 모델 목록이 노출됨 | 🟠 P1 — 인증 없이 모델 호출이나 pull/delete가 가능함 |
| A5 | 방화벽 | ufw 바이너리가 없는데 INPUT 체인에서 ufw-* 체인을 참조함. `ufw-user-input` 체인은 없음. 기본 정책 INPUT ACCEPT | 🟠 P1 |
| A6 | SearXNG :8888 | 외부 200. 인증이 없어 오픈 프록시나 검색 남용에 쓰일 수 있음 | 🟡 P2 |
| A7 | 시크릿 파일 | `.env` 600 ✅, `claude_relay_secret.txt` 600 ✅, `aads-dashboard/.env.local` **644** | 🟠 P1(대시보드) |
| A8 | 컨테이너 | privileged 컨테이너 0개. aads-nginx는 host 네트워크 사용. docker.sock은 socket-proxy만 마운트 ✅ | ✅ 양호 |
| A9 | OS | Ubuntu 24.04.4, 커널 6.8.0-106. 보안 업데이트 대기 0건. `/var/run/reboot-required` 존재 | 🟡 P2 |
| A10 | 공개 도메인 aads.newtalk.kr | Cloudflare 경유. 인증서 만료 2026-12-16 ✅. HSTS, X-Frame-Options, CSP, nosniff 헤더 없음 | 🟡 P2 |

### 1.2 contabo14 (5.104.86.14) — KIS / GO100

| # | 항목 | 실측 | 판정 |
|---|---|---|---|
| B1 | SSH :22 | root 로그인 허용, 비밀번호 인증 허용. auth.log 누적 실패 16,661건. fail2ban 없음 | 🔴 P0 |
| B2 | ufw | active. 기본 정책 deny. 5432/3000/3001은 116·114·14 IP만 허용 ✅. 3099와 3301은 외부 closed ✅ | ✅ 양호 |
| B3 | pg_hba.conf | kis_admin 계정의 허용 IP에 **68.183.183.11/32**(등록부에 없는 IP)가 있음. 인증 방식은 md5(scram 아님). 이 IP는 ufw 허용 목록에 없어 현재는 차단됨 | 🟠 P1 — 출처를 확인하고 제거 |
| B4 | `/root/kis-autotrade-v4/.env*` | 본 `.env` 권한 **665**(그룹 쓰기, 전체 읽기). 백업 사본 10개 이상이 665/645 권한으로 남아 있음(소유자 go100user/root) | 🟠 P1 — 실거래 증권 키 노출 위험 |
| B5 | OS | Ubuntu 24.04.4. 보안 업데이트 대기 0건. reboot-required 존재 | 🟡 P2 |
| B6 | Docker DOCKER-USER | 비어 있음. 현재 0.0.0.0에 게시된 컨테이너 포트는 확인되지 않음(해당 명령 타임아웃으로 미검증) | ⚠️ 미검증 |

### 1.3 cafe24_114 (114.207.244.86) — SF / NTV2 / NAS

| # | 항목 | 실측 | 판정 |
|---|---|---|---|
| C1 | newtalk-v2-redis :6380 | 외부(116)에서 `PING` → `+PONG`. `requirepass`가 비어 있음 | 🔴 P0 — 데이터 탈취나 변조, `CONFIG SET`을 이용한 RCE 위험 |
| C2 | MySQL :3306(호스트), :3307(newtalk-v2-db) | 둘 다 외부 OPEN | 🔴 P0 — DB 포트는 인터넷에 노출되면 안 됨 |
| C3 | SSH :7916 | root 로그인 허용, 비밀번호 인증 허용. auth.log 실패 35,335건. fail2ban 없음 | 🔴 P0 |
| C4 | 방화벽 | ufw inactive. 외부에 열린 포트 18개 확인: 25, 3000, 3001, 3099, 3306, 3307, 5678, 6001, 6380, 8000, 8001, 8080, 8200, 8501, 9090, 9900, 16789, 7916 | 🟠 P1 |
| C5 | OS | **Ubuntu 20.04.6. 표준 지원 종료(2025-04). 커널 5.4.0-94(2022년 초 빌드)** | 🟠 P1 — 알려진 커널 LPE 취약점이 누적됨 |
| C6 | 인증 없는 대시보드 | shortflow-dashboard(Streamlit) :8501 외부 200. shortflow-worker :8000/8001은 FastAPI(404 응답이지만 `/docs` 등 확인 필요) | 🟠 P1 |
| C7 | n8n :5678 | 외부 200. owner 설정은 완료되어 로그인이 필요함 ✅. 다만 워크플로 자동화 도구가 인터넷에 직접 노출되어 있음 | 🟡 P2 |
| C8 | Webmin | 서비스 active. :10000은 외부 closed(상위 방화벽이 막는 것으로 보임) | 🟡 P2 |
| C9 | 시크릿 | `/data/shortflow/.env` **644**(전체 읽기), `/srv/newtalk-v2/src/.env` 640 ✅ | 🟠 P1 |
| C10 | SMTP :25 | 외부 OPEN. 오픈 릴레이 여부는 확인하지 못함 | ⚠️ 미검증 |

### 1.4 jinah244 (5.104.85.244) — ACCT

| 항목 | 실측 | 판정 |
|---|---|---|
| 도달성 | 116에서 22, 80, 443, 5432, 8080 포트 모두 closed. SSH는 Connection refused | ⚠️ 점검 불가. 외부 공격면은 작지만 내부 상태는 확인하지 못함 |

## 2. 조치 계획

| 순위 | 조치 | 대상 | 되돌리기 | 검증 기준 |
|---|---|---|---|---|
| P0-1 | 릴레이 전 라우트에 `_relay_secret_ok` 미들웨어 적용 + 8199를 127.0.0.1/도커망 전용으로 제한 (iptables DROP 또는 bind 변경) | 116 `scripts/claude_relay_server.py` (인증 핵심 파일, ALLOW_AUTH_COMMIT) | 규칙 삭제 / git revert | 외부에서 8199 closed, 무시크릿 `/stream` 403 |
| P0-2 | ip6tables 로 5433 DROP (IPv4 규칙과 동일) | 116 | `ip6tables -D` | contabo14 에서 v6 5433 closed |
| P0-3 | Redis 6380·MySQL 3306/3307 포트 바인딩을 127.0.0.1 로 변경 또는 ufw deny + Redis requirepass 설정 | 114 NTV2 compose | compose 원복 | 외부에서 closed, PING → NOAUTH |
| P0-4 | SSH `PasswordAuthentication no`, `PermitRootLogin prohibit-password` + fail2ban 설치 (키 접속 확인 후) | 3대 | sshd_config.bak 복원 | `sshd -T` 값, 키 로그인 성공 |
| P1 | Ollama 127.0.0.1 바인딩 / 116 ufw 재설치·정리 / 114 ufw 활성화 / .env 600 + 백업 정리 / pg_hba 68.183.183.11 제거·scram 전환 / 114 OS 업그레이드 계획 | 각 서버 | 파일 원복 | 재점검 포트 스캔 |
| P2 | 보안 헤더(HSTS 등), 재부팅 창구, X11Forwarding no, SMTP 릴레이 점검 | 전체 | — | curl -I 헤더 확인 |

## 3. 미검증 / 한계
- 외부 도달성은 등록된 다른 서버(contabo14, contabo116)에서 확인했다. 제3의 인터넷 망에서는 확인하지 않았다.
  116에는 방화벽이 없으므로 인터넷 전체에 노출되어 있을 가능성이 높다고 본다.
- 릴레이 무인증 여부는 코드와 GET 응답으로 판정했다. `/stream`에 실제 POST를 보내 악용 가능한지는 시험하지 않았다(운영 토큰을 사용하게 되므로).
- contabo14 컨테이너 권한 점검 명령은 55초 타임아웃으로 실패했다.
- jinah244는 접근할 수 없어 내부를 점검하지 못했다.
