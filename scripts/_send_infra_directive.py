"""인프라관리 세션(AADS-013)에 서버68→Contabo5 이전 위임 지시 메시지 전송."""
import json
import urllib.request

SESSION_ID = "b165b490-9f8a-40fb-aeab-0b483ff95406"

CONTENT = """[CEO 위임 — PM/CTO 경유 전달]

## 미션
**서버68(DigitalOcean SGP1) → Contabo 서버5 전면 이전 준비**를 인프라관리자 책임으로 위임한다.
본 세션은 이전 실행 전 모든 사전 점검·갭 분석·실행 계획·리스크 평가를 산출하여 CEO 승인 게이트에 올린다.

## 현재 실측 상태 (PM/CTO 사전 조사, 2026-05-11 KST)
- **서버68**: DO SGP1, 8 vCPU / 15GB RAM(사용 4.6GB) / 160GB 디스크(사용 135GB, 가용 26GB)
- **동기화 현황**: /etc/cron.d/contabo-sync 5분 주기 활성. 코드 rsync + API 핫리로드 동작. 마지막 실행 12:15 KST 성공
- **DB 동기화**: 1시간 atomic swap. 마지막 11:32:30 KST. 현재 차이 server68=38,837건 / server5=38,799건 (38건 lag)
- **대시보드**: rsync는 되나 Next.js 컨테이너 자동 rebuild/restart 미구성 (수동)
- **이전 논의 이력**: 2026-04-30 SaaS 전환 아젠다 9건 등록(pending). 68서버 이전 구체 계획은 미수립.

## 산출물 (Deliverables)
다음 6개 산출물을 본 세션에서 순차 작성·보고하라:

1. **현황 인벤토리** — server68에서 운영 중인 컨테이너·서비스·도메인·DNS·SSL 인증서·환경변수·외부 의존성(서버211, 서버114, GitHub, Telegram, Anthropic OAuth, LiteLLM, NAS 등) 전수 목록
2. **갭 분석** — server68 ↔ server5 차이 (이미지 버전, 환경변수, .env 시크릿, cron, systemd, nginx 설정, Docker volumes, PostgreSQL pgvector, Redis, 파일 첨부 저장소)
3. **이전 체크리스트** — Pre-flight / Cutover / Post-cutover 3단계 체크리스트 (각 항목에 검증 명령 포함)
4. **리스크 평가** — 다운타임 윈도우, 데이터 일관성 보장 방식, DNS TTL 단축 계획, 롤백 시나리오 3종
5. **단계별 실행 계획 (Phase 0~3)**
   - Phase 0: 준비 (백업·이미지 빌드·시크릿 동기화)
   - Phase 1: Dry-run (서버5에서 staging 검증)
   - Phase 2: Cutover (DNS 전환·DB 최종 sync·서비스 전환)
   - Phase 3: 검증 및 server68 유지보수 모드 전환
6. **CEO 승인 게이트** — 각 Phase별 CEO 승인이 필요한 결정 항목과 자율 수행 가능 항목 분류

## 제약·금지 사항
- **본 세션은 계획 수립만 수행**. 실제 cutover·DNS 변경·서비스 중단·DB 마이그레이션 명령은 CEO 승인 게이트 전 절대 실행 금지
- 사전 점검용 읽기 도구(run_remote_command, query_db, read_remote_file, list_remote_dir)는 자유 사용
- .env 시크릿 노출 금지, docker compose up -d 전체 실행 금지(R-DOCKER), --no-verify 금지(R-COMMIT)
- 모든 수치는 실측만 사용. 추정값은 "미측정"으로 표기

## 보고 형식
- 각 산출물은 마크다운 표·코드블록·체크리스트 형식
- 완료 시 docs/plans/AADS-INFRA-MIGRATION-68-TO-CONTABO5.md로 저장 (write_remote_file)
- 아젠다 등록: ceo_agenda 테이블에 P0/pending으로 INSERT 요청 (또는 PM에게 요청)
- 진행 상황은 이 세션에 단계별로 보고하라

## 시작 지시
1. 먼저 **현황 인벤토리** 작성에 착수하라
2. server68 컨테이너 목록, nginx 설정, /etc/cron.d 전체, DNS A 레코드(aads.newtalk.kr 등), SSL 인증서 만료일을 실측하라
3. 인벤토리 완료 후 보고하고 갭 분석으로 진행하라

— CEO 직접 지시 / PM·CTO 전달 (2026-05-11 KST)
"""

payload = json.dumps({
    "session_id": SESSION_ID,
    "content": CONTENT,
    "model_override": "claude-opus",
}).encode("utf-8")

req = urllib.request.Request(
    "http://localhost:8080/api/v1/chat/messages/send",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-Monitor-Key": "internal-pm-cto-delegation",
    },
    method="POST",
)

try:
    # SSE 스트림 응답이지만 헤더만 받고 닫음 (서버는 백그라운드로 처리 계속)
    resp = urllib.request.urlopen(req, timeout=4)
    print("HTTP_STATUS:", resp.status)
    print("FIRST_BYTES:", resp.read(200).decode("utf-8", errors="replace"))
except urllib.error.HTTPError as e:
    print("HTTP_ERROR:", e.code, e.reason)
    print("BODY:", e.read().decode("utf-8", errors="replace")[:500])
except Exception as e:
    # 타임아웃이어도 서버는 이미 메시지를 큐에 넣고 처리 중일 수 있음
    print("EXCEPTION:", type(e).__name__, str(e))
