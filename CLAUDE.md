# AADS — 자율 AI 개발 시스템 (서버 68)

## 릴리스 규칙의 원본은 AGENTS.md 다 (R-RELEASE)

빌드·배포·블루그린에 관한 규칙은 **`/root/aads/AGENTS.md` 가 유일한 원본**이고,
저장소별 세부는 각 저장소의 `AGENTS.md` 에 있다. 이 문서는 그것을 요약하거나
대체하지 않는다 — 배포 전에 직접 읽어라.

| 파일 | 범위 |
|---|---|
| `/root/aads/AGENTS.md` | 전역 블루/그린 릴리스 계약 11개 조항 |
| `aads-server/AGENTS.md` | API 릴리스 |
| `aads-dashboard/AGENTS.md` | 대시보드 릴리스 + 실제 실패 사례 |

**왜 이렇게 두는가.** 2026-09-13, 이 문서에는 대시보드 배포가
`docker compose build && up -d` 로 적혀 있었고 그대로 따랐다가 헛빌드를 했다.
정작 맞는 규칙("one image build per release SHA", "clean committed release
worktree")은 AGENTS.md 에만 있었다. 규칙을 두 벌로 유지하면 한쪽이 반드시
낡고, 낡은 쪽을 읽는 쪽이 사고를 낸다.

## 기술 스택
FastAPI 0.115, PostgreSQL 15, LangGraph 1.0.10, Docker Compose, Python 3.11, Next.js 16

## CEO 절대 규칙
- CEO-DIRECTIVES: https://raw.githubusercontent.com/moongoby-GO100/aads-docs/main/CEO-DIRECTIVES.md
- 핵심: Supavisor 금지, langgraph-supervisor 금지, LLM 15회/task, 비용 효율 최우선
- HANDOVER 업데이트 없이 완료 선언 금지 (R-001)
- GitHub 브라우저 경로로 보고 (R-008)

## API 키 보안 절대 규칙 (R-KEY)
- **절대 API 키를 소스코드/docker-compose/yaml에 하드코딩하지 않는다**
- 모든 시크릿은 `.env` 파일에만 저장하고, docker-compose에서는 `${VAR:-}` 참조
- `.env`는 `.gitignore`에 포함되어 있으므로 git에 커밋되지 않음
- 커밋/푸시 전 pre-commit hook이 API 키 패턴을 자동 감지하여 차단
- 위반 시: Google 등 제공사가 키를 leaked 처리하여 영구 비활성화됨

## 인증 토큰 절대 규칙 (R-AUTH)
- **AADS는 Anthropic OAuth만** — `ANTHROPIC_AUTH_TOKEN` (sk-ant-oat01-…), `ANTHROPIC_AUTH_TOKEN_2` (2계정)
- docker-compose는 이 이름들을 **`.env` 경유로만** 주입(sk-ant-api03 API 키 금지)
- **2순위 폴백**: `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_AUTH_TOKEN_2` → Gemini LiteLLM
- **원격 `claude` CLI**(pipeline_c 등): 셸에서 `ANTHROPIC_AUTH_TOKEN` 우선 export 후, 레거시 바이너리 호환용으로 동일 값을 `ANTHROPIC_API_KEY`에 **복사만** (값은 OAuth)
- 앱 Python 코드에서 `os.getenv("ANTHROPIC_API_KEY")` **신규 사용 금지** — `anthropic_client`·`ANTHROPIC_AUTH_TOKEN` 사용
- **Gemini/DeepSeek**: LiteLLM 프록시 경유 (`LITELLM_BASE_URL`)
- **중앙**: `app/core/anthropic_client.py` `call_llm_with_fallback()`

## FLOW 프레임워크
Find→Layout→Operate→Wrap up. 상세: .claude/rules/flow-rules.md

## 공유 교훈
docs/shared-lessons/INDEX.md 참조. 작업 전 관련 교훈 확인 필수.

## AADS 전용 지식
docs/knowledge/AADS-KNOWLEDGE.md — 아키텍처, 파이프라인, 교차검증, 함정

## 메모리 자동 주입 시스템 (AADS-186E, 2026-03-09)
- **모듈**: `app/core/memory_recall.py` — 5섹션 메모리 빌더 (session_notes/preferences/tool_strategy/directives/discoveries)
- **프로젝트별 필터**: ai_observations.project 컬럼으로 AADS/KIS/GO100/SF/NTV2/NAS 분리 주입
- **자동 축적**: 20턴마다 session_notes 저장 + CEO 패턴 관찰, 에이전트 완료 시 discovery 기록
- **시드 데이터**: 37건 (공통 12 + 프로젝트별 25) — `scripts/init_memory_schema.sql`
- **DB 테이블**: session_notes, ai_observations (project 컬럼), ai_meta_memory

## AADS-190: 원격 쓰기/실행 + 서브에이전트 (2026-03-10)
- **Phase 0**: 에러 리포팅(`/chat/errors/report`), StreamManager(멀티세션), CEO Chat 메모리 주입, 임베딩 검증
- **Phase 1**: 9개 원격 도구 — write_remote_file, patch_remote_file, run_remote_command, git 5종
  - 보안: blocked regex → whitelist → pipe 제한, 민감 경로 차단, force push 차단
- **Phase 2**: 서브에이전트(`spawn_subagent`, `spawn_parallel_subagents`), 턴 100/예산 $50, 압축 환경변수화
  - 서브에이전트: 독립 LLM 호출, 읽기 도구 7종, asyncio.gather 병렬 실행
- **리포트**: `reports/20260310_AADS190_phase0_phase1_phase2_report.md`

## 커밋 절대 규칙 (R-COMMIT)
- **`--no-verify` 절대 금지** — pre-commit hook은 API 키 유출·코드 품질을 보호한다. 우회하면 보안 사고.
- **hook 차단 시 원인을 수정**한 후 재커밋. 우회 방법을 찾지 마라.
- **인증 핵심 파일** (`auth_provider.py`, `model_selector.py`, `claude_relay_server.py`, `docker-compose.yml`) 수정 시: `ALLOW_AUTH_COMMIT=1 git commit -m "..."`
- **테스트 실패 시 테스트를 삭제하지 말고 수정**. 코드 변경으로 기존 테스트가 깨지면, 코드 + 테스트를 같이 수정하여 커밋.
- **커밋 전 반드시 확인**: `git log -1`로 커밋 성공 확인. "완료" 보고는 커밋+푸시 후에만.

## Docker 절대 규칙 (R-DOCKER)
- **`docker compose up -d` 전체 실행 절대 금지** — postgres/litellm/aads-server가 동시 재생성되어 채팅 시스템 전체가 중단됨.
- **단일 서비스만 재시작**: `docker compose up -d --no-deps <서비스명>` 또는 `docker compose restart <서비스명>`
- **대시보드 빌드/배포**: `cd /root/aads/aads-dashboard && bash deploy.sh` (blue-green 무중단)
  - 세부 규칙과 실패 사례는 `aads-dashboard/AGENTS.md` — 배포 전에 읽어라. 여기에 다시 적지 않는다(R-RELEASE).
  - aads-server compose 파일 사용 금지.
- **aads-server 재시작 필요 시 (무중단 배포 필수)**:
  - Python 코드만 변경: `docker exec aads-server bash /app/scripts/reload-api.sh` (0ms 다운타임)
  - 이미지 리빌드 필요: `bash /root/aads/aads-server/deploy.sh bluegreen` (0초 무중단)
  - **`supervisorctl restart aads-api` 직접 실행 절대 금지** — 활성 SSE 스트림이 전부 끊김
- **docker-compose.yml 환경변수 수정 후**: 즉시 `docker compose up -d`하지 말고 CEO 승인 후 점검 창구에서 실행.

## 코드 품질 규칙 (R-QUALITY)
- **자동 생성 코드(`check_tool_consistency --fix` 등) 실행 후 반드시 테스트** — 자동 생성이 들여쓰기, 클래스 소속을 잘못 만들 수 있음.
- **테스트 추가 시 반드시 실행 확인**: `bash scripts/run_unit_tests.sh tests/unit/test_tools_and_pipeline.py` — 전체 PASS 확인 후 커밋.
  - 런타임 이미지에 pytest가 없어 `docker exec aads-server python3 -m pytest`는 더 이상 동작하지 않는다. 이 스크립트가 운영 이미지 + 워킹트리 마운트로 실행한다.
  - 종료코드 0=통과 / 1=실패 / 2=실행 불가. 2는 게이트 미작동이므로 pre-commit이 커밋을 차단한다.
- **기존 테스트가 실패하면 방치하지 말고 즉시 수정** — 실패하는 테스트가 쌓이면 테스트 시스템 전체가 무력화됨.
- **pre-commit hook 5단계**: ①API 키 탐지 ②구문 검사 ③ruff 정적 분석 ④Docker import 검증 ⑤단위 테스트 — 모두 통과해야 커밋 가능.

## 현재 상태
- Phase: Phase 2 운영
- 최근: AADS-190(원격 쓰기+서브에이전트), AADS-186E(메모리 자동 주입), AADS-188C(도구 우선순위)
- 긴급: 없음

## 빌드/배포
docker compose -f docker-compose.prod.yml up -d --build aads-server
curl -s https://aads.newtalk.kr/api/v1/ops/health-check | python3 -m json.tool
