"""기존 HANDOVER.md 핵심 데이터를 system_memory 테이블로 초기 적재"""
import asyncio, asyncpg, json

INITIAL_DATA = [
    ("status", "server", {"ip": "68.183.183.11", "provider": "DigitalOcean", "migration_target": "Contabo"}),
    ("status", "versions", {"handover": "v3.7", "ceo_directives": "v2.3", "server": "0.2.0", "dashboard": "0.1.0"}),
    ("status", "health", {"api": "ok", "postgres": "ok", "mcp_servers": 3, "tests": 118, "coverage": "62%"}),
    ("repos", "aads-server", {"url": "https://github.com/moongoby-GO100/aads-server", "commits": 21, "last_commit": "046111f"}),
    ("repos", "aads-dashboard", {"url": "https://github.com/moongoby-GO100/aads-dashboard", "commits": 4, "last_commit": "2ea0348"}),
    ("repos", "aads-docs", {"url": "https://github.com/moongoby-GO100/aads-docs"}),
    ("architecture", "agent_pipeline", {"agents": 8, "framework": "LangGraph 1.0.10", "pattern": "native StateGraph", "banned": "langgraph-supervisor"}),
    ("architecture", "memory_system", {"L1": "Working Memory (AADSState+Checkpointer)", "L2": "Project Memory (PostgreSQL)", "L3": "Experience Memory (pgvector)", "L4": "System Memory (PostgreSQL)", "L5": "Procedural Memory (PostgreSQL)"}),
    ("architecture", "sandbox", {"default": "Docker local", "fallback": "code-only", "optional": "E2B"}),
    ("agents", "supervisor", {"model": "Claude Opus 4.6", "cost_input": "$5/M", "cost_output": "$25/M"}),
    ("agents", "pm", {"model": "Claude Sonnet 4.6", "cost_input": "$3/M", "cost_output": "$15/M"}),
    ("agents", "architect", {"model": "Claude Opus 4.6", "cost_input": "$5/M", "cost_output": "$25/M"}),
    ("agents", "developer", {"model": "Claude Sonnet 4.6", "cost_input": "$3/M", "cost_output": "$15/M"}),
    ("agents", "qa", {"model": "Claude Sonnet 4.6", "cost_input": "$3/M", "cost_output": "$15/M"}),
    ("agents", "judge", {"model": "Gemini 3.1 Pro", "cost_input": "$2/M", "cost_output": "$12/M"}),
    ("agents", "devops", {"model": "GPT-5 mini", "cost_input": "$0.25/M", "cost_output": "$2/M"}),
    ("agents", "researcher", {"model": "Gemini 2.5 Flash", "cost_input": "$0.30/M", "cost_output": "$2.50/M"}),
    ("phase", "phase_0", {"status": "completed", "description": "Research & Planning"}),
    ("phase", "phase_1", {"status": "completed", "description": "Core 8-agent pipeline", "tests": "45/45 PASS"}),
    ("phase", "phase_1.5", {"status": "completed", "description": "Real test, CI/CD, Integration"}),
    ("phase", "phase_2", {"status": "in_progress", "progress": "75%", "description": "Dashboard + MCP + Stability"}),
    ("phase", "phase_3", {"status": "not_started", "description": "SaaS deployment, multi-tenant"}),
    ("costs", "monthly_target", {"min": "$23", "max": "$63", "current_estimate": "$55"}),
    ("costs", "llm_pricing", {"claude_opus": "$5/$25", "claude_sonnet": "$3/$15", "gpt5_mini": "$0.25/$2"}),
    ("pending", "env_keys", {"items": ["ANTHROPIC_API_KEY verify", "E2B_API_KEY optional", "AADS_MONITOR_KEY set"]}),
    ("pending", "next_tasks", {"items": ["E2E fullcycle test", "Phase 3 SaaS plan", "Contabo migration"]}),
]

async def migrate():
    conn = await asyncpg.connect("postgresql://aads:aads_dev_local@localhost:5432/aads")
    for cat, key, value in INITIAL_DATA:
        await conn.execute("""
            INSERT INTO system_memory (category, key, value, version, updated_by)
            VALUES ($1, $2, $3, 'v3.7', 'migration')
            ON CONFLICT (category, key) DO UPDATE SET value=$3, updated_at=NOW()
        """, cat, key, json.dumps(value))
    count = await conn.fetchval("SELECT COUNT(*) FROM system_memory")
    print(f"Migrated {count} entries to system_memory")
    await conn.close()

asyncio.run(migrate())
