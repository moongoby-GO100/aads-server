"""
HANDOVER.md v3.8 핵심 데이터를 system_memory 테이블로 구조화 입력
카테고리: status, repos, architecture, agents, phase, costs, ceo_directives, pending, history
총 28건 INSERT/UPSERT — Docker Postgres (localhost:5433) 기준
"""
import asyncio, asyncpg, json, os, re
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

def get_db_url() -> str:
    """Docker Postgres(5433)에 연결. DATABASE_URL의 내부 호스트명을 localhost:5433으로 변환."""
    url = os.getenv("DATABASE_URL", "postgresql://aads:aads_dev_local@localhost:5433/aads")
    # 컨테이너 내부 호스트명(postgres / aads-postgres)을 호스트에서 접근 가능한 주소로 변환
    url = re.sub(r'@[^:/]+:5432/', '@localhost:5433/', url)
    return url

# ─── 9개 카테고리 × 28건 데이터 ───────────────────────────────────────────────

INITIAL_DATA = [
    # ── 1. status (3건) ──────────────────────────────────────────────────────
    ("status", "server_info", {
        "url": "https://aads.newtalk.kr",
        "server": "DigitalOcean 68.183.183.11",
        "migration_target": "Contabo VPS (2026-04-01)",
        "api_port": 8100,
        "dashboard_port": 3100,
        "status": "launch-ready",
    }),
    ("status", "e2e_test", {
        "result": "7/8 PASSED",
        "task_id": "LAUNCH-READY-010",
        "commit_sha": "a69c061",
        "tested_at": "2026-03-04",
        "verdict": "LAUNCH READY",
        "details": {
            "health": "PASS", "login": "PASS", "create_project": "PASS",
            "pipeline": "PASS", "costs": "PASS", "chat_api": "PASS",
            "memory": "PASS", "sandbox": "FAIL (E2B PLACEHOLDER)",
        },
        "e2e_cost_usd": 0.69,
        "llm_calls": 8,
    }),
    ("status", "versions", {
        "handover": "v3.8",
        "ceo_directives": "v2.4",
        "server": "0.2.0",
        "dashboard": "0.1.0",
        "last_updated": "2026-03-04",
    }),

    # ── 2. repos (3건) ───────────────────────────────────────────────────────
    ("repos", "aads-server", {
        "url": "https://github.com/moongoby-GO100/aads-server",
        "last_commit": "a69c061",
        "description": "FastAPI + LangGraph 8-agent pipeline",
        "pat_expires": "2026-05-27",
    }),
    ("repos", "aads-dashboard", {
        "url": "https://github.com/moongoby-GO100/aads-dashboard",
        "last_commit": "2ea0348",
        "description": "Next.js 16 + React + Tailwind dashboard",
        "live_url": "https://aads.newtalk.kr/",
    }),
    ("repos", "aads-docs", {
        "url": "https://github.com/moongoby-GO100/aads-docs",
        "files": ["HANDOVER.md", "CEO-DIRECTIVES.md", "design/aads-architecture-v1.1.md"],
    }),

    # ── 3. architecture (4건) ────────────────────────────────────────────────
    ("architecture", "memory_system", {
        "L1": "Working Memory (AADSState + AsyncPostgresSaver checkpointer)",
        "L2": "Project Memory (PostgreSQL — project_memories table)",
        "L3": "Experience Memory (pgvector — experience_memory table, RIF scoring)",
        "L4": "System Memory (PostgreSQL — system_memory table, 9 categories)",
        "L5": "Procedural Memory (PostgreSQL — procedural_memory table)",
        "layers": 5,
        "status": "operational",
    }),
    ("architecture", "agent_architecture", {
        "framework": "LangGraph >= 1.0.10",
        "pattern": "Native Tool-Based StateGraph (langgraph-supervisor 금지 R-010)",
        "agents": 8,
        "chain": "Supervisor→PM→Architect→Developer→QA→Judge→DevOps→Researcher",
        "hitl_checkpoints": 6,
        "max_llm_calls_per_task": 15,
        "concurrent_sandboxes": 5,
    }),
    ("architecture", "mcp_stack", {
        "always_on": ["Filesystem", "Git", "Memory", "PostgreSQL"],
        "on_demand": ["GitHub", "Brave Search", "Fetch"],
        "phase2_expansion": ["Puppeteer", "Sentry", "Slack"],
        "transport": "SSE (supervisord env) / stdio (direct call)",
        "manager": "MCPClientManager with HTTP ping verification",
        "total_ecosystem": "8,600+ servers",
    }),
    ("architecture", "sandbox_strategy", {
        "small_projects": "Docker local container (cost: $0)",
        "security": "--network=none --memory=512m --cpus=1 --read-only tmpfs:/tmp:100m",
        "large_projects": "User server SSH/Docker API (Phase 3)",
        "e2b": "Phase 3+ SaaS option only (currently PLACEHOLDER)",
        "confirmed_by": "CEO D-011 2026-03-03",
    }),

    # ── 4. agents (8건) ──────────────────────────────────────────────────────
    ("agents", "supervisor", {
        "model": "claude-opus-4-6",
        "role": "오케스트레이션, 태스크 분배·합성",
        "cost_input_per_1m": "$5",
        "cost_output_per_1m": "$25",
        "alt_model": "gemini-3.1-pro-preview ($2/$12)",
    }),
    ("agents", "pm", {
        "model": "claude-sonnet-4-6",
        "role": "사용자 대화, 구조화 JSON 스펙 생성, 체크포인트 관리",
        "cost_input_per_1m": "$3",
        "cost_output_per_1m": "$15",
        "alt_model": "gpt-5.2-chat-latest ($1.75/$14)",
    }),
    ("agents", "architect", {
        "model": "claude-opus-4-6",
        "role": "시스템 설계, 기술 의사결정",
        "cost_input_per_1m": "$5",
        "cost_output_per_1m": "$25",
        "alt_model": "gemini-3.1-pro-preview ($2/$12)",
    }),
    ("agents", "developer", {
        "model": "claude-sonnet-4-6",
        "role": "코드 생성·수정·리팩터",
        "cost_input_per_1m": "$3",
        "cost_output_per_1m": "$15",
        "alt_model": "gpt-5.3-codex ($1.75/$14)",
    }),
    ("agents", "qa", {
        "model": "claude-sonnet-4-6",
        "role": "테스트 생성·실행·버그 리포트",
        "cost_input_per_1m": "$3",
        "cost_output_per_1m": "$15",
        "alt_model": "gpt-5-mini ($0.25/$2)",
    }),
    ("agents", "judge", {
        "model": "claude-sonnet-4-6",
        "role": "독립 출력 검증, 스펙 대비 코드 정합성 평가",
        "cost_input_per_1m": "$3",
        "cost_output_per_1m": "$15",
        "alt_model": "gemini-3.1-pro-preview ($2/$12)",
        "accuracy_improvement": "~10%→~70%",
    }),
    ("agents", "devops", {
        "model": "gpt-5-mini",
        "role": "CI/CD, 배포, 모니터링",
        "cost_input_per_1m": "$0.25",
        "cost_output_per_1m": "$2",
        "alt_model": "claude-haiku-4-5 ($1/$5)",
    }),
    ("agents", "researcher", {
        "model": "gemini-2.5-flash",
        "role": "데이터 수집·분석·웹 검색",
        "cost_input_per_1m": "$0.30",
        "cost_output_per_1m": "$2.50",
        "alt_model": "gpt-5-nano ($0.05/$0.40)",
    }),

    # ── 5. phase (3건) ───────────────────────────────────────────────────────
    ("phase", "completed_tasks", {
        "LAUNCH-READY-010": "Docker 샌드박스(D-011), CEO-DIRECTIVES v2.4, E2E 풀사이클($0.69), 가동 준비 완료",
        "PHASE2-STABILITY-006": "JWT 86자 보안키, 호스트 PostgreSQL 연결(iptables 5432), MCP SSE ping 수정, 118 PASS",
        "PHASE2-MCP-LIVE-005": "MCP 실구동 서버 3개(Filesystem/Git/Memory FastMCP SSE), 테스트 118개, 커버리지 62%",
        "PHASE2-POLISH-004": "auth.py hmac.compare_digest, 전역 예외 핸들러, structlog 표준화, TS 타입 오류 0",
        "PHASE2-LLM-CONNECT-003": "실제 LLM 연동, 8-agent E2E completed, 45/45 PASS",
        "PHASE15-CICD-002": "GitHub Actions CI/CD, README 8-agent 반영",
        "PHASE15-REALTEST-001": "HITL 체크포인트(6단계 auto_approve), Redis 비용추적, /costs API, 56/56 PASS",
        "PHASE1-W2-005": "8-agent chain 완성(Architect/DevOps/Researcher 추가), 45/45 PASS",
    }),
    ("phase", "current_phase", {
        "phase": "Phase 2 — 가동 준비 완료",
        "status": "launch-ready",
        "next": "CEO 직접 테스트 → Phase 3 SaaS 기획",
        "dashboard": "https://aads.newtalk.kr/",
        "tests": "7/8 E2E PASS (sandbox FAIL=E2B PLACEHOLDER)",
    }),
    ("phase", "roadmap", {
        "phase_0": "완료 — 리서치 + 아키텍처 설계 + 인계서 시스템",
        "phase_1": "완료 — AADS 코어 8-agent chain, 45/45 PASS",
        "phase_1_5": "완료 — REALTEST, CI/CD, Integration, 63/63 PASS",
        "phase_2": "가동 준비 완료 — Dashboard, MCP, Stability, E2E 7/8",
        "phase_3": "대기 — SaaS 멀티유저, 결제 연동, Contabo 이전",
        "phase_4": "대기 — 자율 개선 루프",
        "phase_5": "대기 — 첫 프로젝트 HealthMate",
        "phase_6": "대기 — 범용화 SaaS 확장",
    }),

    # ── 6. costs (2건) ───────────────────────────────────────────────────────
    ("costs", "model_pricing", {
        "claude_opus_4_6": "$5/$25 per 1M tokens",
        "claude_sonnet_4_6": "$3/$15 per 1M tokens",
        "claude_haiku_4_5": "$1/$5 per 1M tokens",
        "gpt_5_mini": "$0.25/$2 per 1M tokens",
        "gpt_5_nano": "$0.05/$0.40 per 1M tokens",
        "gemini_3_1_pro": "$2/$12 per 1M tokens",
        "gemini_2_5_flash": "$0.30/$2.50 per 1M tokens",
        "forbidden": "gpt-5.2-pro ($21/$168) — 극고가 사용 금지",
        "optimization": "프롬프트 캐싱 최대 90% 절감 + 배치 API 50% 절감",
    }),
    ("costs", "monthly_estimate", {
        "contabo_vps": "$12.99/월 (Contabo VPS 30, DO 이전 목표 2026-04-01)",
        "llm_target": "$50~$150/월",
        "external_api": "$2~$10/월 (Groq Whisper/Google TTS/DALL-E 3)",
        "total_target": "$64~$172/월",
        "current_estimate": "~$55 (캐싱 후)",
        "e2e_test_sample": "$0.69 per 8 LLM calls",
        "saas_bep": "15~20명 유료 사용자, 안정기 마진 85~90%",
        "monthly_budget_cap": "$500 (D-004)",
    }),

    # ── 7. ceo_directives (3건) ──────────────────────────────────────────────
    ("ceo_directives", "principles", {
        "D-001": "단순 사고 금지 — 하나를 던지면 10을 생각하고 연구해서 반영",
        "D-002": "사소한 것도 빠짐없이 — 모든 모델/도구/옵션 누락 없이 비교",
        "D-003": "AADS의 본질 — AI 에이전트 조직처럼 협업하는 자율 개발 시스템, AADS 자체가 프로덕트",
        "D-004": "비용 효율 최우선 — 월 $23~$63 목표, 모델 라우팅 + 오픈소스 우선",
        "D-005": "컨텍스트 패키지 시스템 — 모든 세션에 HANDOVER+CEO-DIRECTIVES+설계문서 필수 읽기",
        "D-006": "교차검증 필수 — 수치·결론 논리적 일관성 확인",
        "D-007": "도구 vs 에이전트 구분 — Cursor/Windsurf(GUI도구) vs AADS 내부 에이전트(API)",
        "D-008": "속도 우선 — MVP 먼저 배포, 완벽보다 동작하는 것이 먼저",
        "D-009": "현실적 자율성 인식 — 완전 자율은 환상, 점진적 자율성 확대",
        "D-010": "사용자 중심 체크포인트 — 6단계 HITL이 경쟁사(Bolt/Lovable) 대비 핵심 차별점",
    }),
    ("ceo_directives", "absolute_rules", {
        "R-010": "langgraph-supervisor 프로덕션 사용 금지 (MCP 루프 버그 #249)",
        "R-011": "Supabase Supavisor/PgBouncer 경유 금지 (AsyncPipeline 충돌)",
        "R-012": "작업당 LLM 호출 최대 15회",
        "R-secrets": "JWT_SECRET_KEY, AADS_ADMIN_PASSWORD → 서버 .env에만 보관 (git 커밋 금지)",
        "R-gpt52pro": "gpt-5.2-pro ($21/$168) 사용 금지 — 극고가",
        "R-charts": "차트 라이브러리 추가 금지 (Tailwind CSS 순수 구현)",
        "R-db": "Supabase 직접 연결(port 5432) 사용, Supavisor 경유 금지",
        "R-handover": "모든 Task 완료 시 HANDOVER.md 업데이트 의무",
    }),
    ("ceo_directives", "active_priorities", {
        "P1_ceo_test": "CEO 직접 테스트 — 대시보드 로그인, 프로젝트 생성, 파이프라인 실행",
        "P2_phase3": "Phase 3 SaaS 기획 — 멀티유저, 결제(Stripe), Contabo 이전",
        "P3_contabo": "Contabo VPS 30 이전 (2026-04-01 목표, 월 $84 절감 88%)",
        "P4_e2b": "E2B 실제 API 키 적용 (sandbox FAIL → PASS 목표)",
        "genspark_rules": "Genspark 통합지휘: 지시서 파싱→실행→결과보고(RESULT.md)→done/폴더 이동",
        "version": "CEO-DIRECTIVES v2.4 (2026-03-04)",
    }),

    # ── 8. pending (2건) ─────────────────────────────────────────────────────
    ("pending", "next_tasks", {
        "T-019": "System Memory HANDOVER 데이터 마이그레이션 (현재 작업)",
        "T-020": "CEO 직접 E2E 테스트 — 대시보드 로그인 + 프로젝트 생성",
        "T-021": "Phase 3 SaaS 기획서 작성",
        "T-022": "Contabo 이전 실행 (2026-04-01 목표)",
        "T-023": "E2B 실제 API 키 적용 및 sandbox PASS 확인",
    }),
    ("pending", "blocked_items", {
        "E2B_sandbox": "E2B_API_KEY=PLACEHOLDER — 실제 키 필요 (CEO 조달)",
        "AADS_ADMIN_PASSWORD": "실제 값 설정 필요 (서버 .env)",
        "phase3_scope": "SaaS 서비스 범위 미확정 (CEO 승인 필요)",
        "contabo_migration": "DO→Contabo 이전 미실행 (2026-04-01 목표)",
    }),

    # ── 9. history (2건) ─────────────────────────────────────────────────────
    ("history", "version_history", {
        "v3.8": "2026-03-04 — LAUNCH-READY-010: Docker 샌드박스, CEO-DIRECTIVES v2.4, E2E 풀사이클, 가동 준비 완료",
        "v3.7": "2026-03-03 — PHASE2-STABILITY-006: JWT 86자 보안키, 호스트 PostgreSQL 연결, checkpointer fallback",
        "v3.6": "2026-03-03 — PHASE2-MCP-LIVE-005: MCP 실구동 서버 3개, 테스트 118개, 커버리지 62%",
        "v3.5": "2026-03-02 — PHASE2-POLISH-004: auth.py 보안 강화, 전역 예외 핸들러, structlog 표준화",
        "v3.4": "2026-03-02 — PHASE2-LLM-CONNECT-003: 실제 LLM 연동, 8-agent E2E completed",
        "v3.3": "2026-03-02 — PHASE2-DASHBOARD-002: JWT 인증, 시각화 고도화",
        "v3.2": "2026-03-02 — PHASE2-DASHBOARD-001: Next.js 대시보드 기초, Docker Compose",
        "v3.1": "2026-03-02 — PHASE2-INTEGRATION-003: SSE 스트리밍, 상태 API, E2E 3시나리오",
        "v3.0": "2026-03-02 — PHASE15-CICD-002: GitHub Actions CI/CD",
        "v2.9": "2026-03-02 — PHASE15-REALTEST-001: HITL 6단계, Redis 비용추적, 56/56 PASS",
        "v2.8": "2026-03-02 — PHASE1-W2-005: 8-agent chain 완성, 45/45 PASS",
        "v1.0": "2026-02-28 — 초판: 리서치 완료, 인계서 구축",
    }),
    ("history", "recent_changes", {
        "latest": "v3.8 — LAUNCH-READY-010 (2026-03-04)",
        "commit": "a69c061",
        "highlights": [
            "Docker 샌드박스 (D-011) 구현 완료",
            "CEO-DIRECTIVES v2.4 반영",
            "E2E 풀사이클 검증: CEO-Test-Calculator 성공 (8 LLM calls, $0.69)",
            "docker-compose 소켓 마운트(/var/run/docker.sock)",
            "대시보드 접근 OK — https://aads.newtalk.kr/",
        ],
        "next_version": "v3.9 (T-019 마이그레이션 완료 후)",
    }),
]


async def migrate():
    db_url = get_db_url()
    print(f"[migrate_handover] Connecting to: {db_url.split('@')[1]}")

    conn = await asyncpg.connect(db_url)
    inserted = 0
    updated = 0

    for cat, key, value in INITIAL_DATA:
        result = await conn.execute(
            """
            INSERT INTO system_memory (category, key, value, version, updated_by)
            VALUES ($1, $2, $3, 'v3.8', 'migrate_handover_v3.8')
            ON CONFLICT (category, key)
            DO UPDATE SET
                value = $3,
                version = 'v3.8',
                updated_by = 'migrate_handover_v3.8',
                updated_at = NOW()
            """,
            cat, key, json.dumps(value, ensure_ascii=False),
        )
        # result is like "INSERT 0 1" or "UPDATE 1"
        if "INSERT" in result:
            inserted += 1
        else:
            updated += 1
        print(f"  [{'INS' if 'INSERT' in result else 'UPD'}] {cat}/{key}")

    total = await conn.fetchval("SELECT COUNT(*) FROM system_memory")
    cats = await conn.fetch(
        "SELECT category, COUNT(*) as cnt FROM system_memory GROUP BY category ORDER BY category"
    )

    print(f"\n[migrate_handover] Done: {inserted} inserted, {updated} updated")
    print(f"[migrate_handover] Total rows in system_memory: {total}")
    print(f"[migrate_handover] Categories ({len(cats)}):")
    for r in cats:
        print(f"  {r['category']}: {r['cnt']} entries")

    await conn.close()

    required = {"status", "repos", "architecture", "agents", "phase", "costs", "ceo_directives", "pending", "history"}
    actual = {r["category"] for r in cats}
    missing = required - actual
    if missing:
        print(f"\n[WARN] Missing required categories: {missing}")
    else:
        print(f"\n[OK] All 9 required categories present")

    if total < 20:
        print(f"[WARN] Total {total} < 20 required entries")
    else:
        print(f"[OK] {total} entries (>=20 required)")


if __name__ == "__main__":
    asyncio.run(migrate())
