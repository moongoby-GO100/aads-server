# AADS-LAYOUT-002: 자율 루프 에이전트 (Option C) 상세 구현 기획서

> **작성일**: 2026-08-03 (v3 — GPT-5.6 Pro 변형 + Programmatic Tool Calling + 1.05M 컨텍스트 반영)
> **작성자**: OHVIS PM
> **CEO 지시**: "C안으로 기획서를 상세하게 작성하고, Claude CLI와 Codex CLI를 비교하고 장점이 큰 모델들로 처리하고 폴백구조를 서로 보완되게 진행될 수 있게 반영"
> **상위 문서**: AADS-LAYOUT-001_OHVIS-LOOP-SYSTEM.md
> **Phase**: 설계 (FLOW: Lay out)

---

## 1. 개요

### 1.1 목표

현재 OHVIS 루프 시스템은 "감시 → 보고 → CEO 수동 조치"의 반자동 구조입니다.
C안(완전 자율)은 이를 **"감시 → 이상 감지 → AI 자동 수정 → 자동 배포 → 결과 검증"** 까지 완전히 자동화합니다.

```
[현재]
루프 → LLM 감시 → "에러 발견" → 텔레그램 알림 → CEO 수동 조치

[C안: 완전 자율]
루프 → LLM 감시 → "에러 발견" → AI 원인 분석
                                → AI 코드 수정 (Claude CLI / Codex CLI)
                                → 자동 테스트 실행
                                → 자동 배포 (Blue/Green)
                                → 다음 루프에서 수정 결과 검증
                                → 실패 시 자동 롤백
```

### 1.2 핵심 원칙

| 원칙 | 설명 |
|------|------|
| **자율 실행** | 감지 → 수정 → 배포 → 검증 전 과정 무인 운영 |
| **자동 롤백** | 수정 후 헬스체크 실패 시 이전 슬롯으로 즉시 복원 |
| **비용 효율** | 모델별 단가에 따라 자동 라우팅 (경량 진단 → 저렴 모델, 코드 수정 → 고성능 모델) |
| **이중 폴백** | Claude CLI 실패 → Codex CLI 폴백 → LiteLLM API 폴백 |
| **감사 추적** | 모든 자율 조치는 `autonomous_actions` 테이블에 기록 |
| **위험 등급 분류** | 조치 유형별 위험 등급 → 등급별 허용 범위 자동 결정 |

---

## 2. Claude CLI vs Codex CLI 비교 (2026-08 최신)

### 2.1 모델 및 가격 비교

#### Claude Code CLI 모델

| 모델 | Input $/1M | Output $/1M | Context | 용도 |
|------|-----------|------------|---------|------|
| **Claude Sonnet 5** | $2 (→9/1부터 $3) | $10 (→$15) | 200K | **1순위: 코드 수정** (비용/성능 최적) |
| Claude Haiku 4.5 | $1 | $5 | 200K | **진단/분류** (저비용 고속) |
| Claude Opus 5 | $5 | $25 | 200K | 복잡 아키텍처 수정 (필요시만) |
| Claude Fable 5 | $10 | $50 | 200K | 최고급 추론 (특수 케이스) |
| Claude Opus 4.6/4.7/4.8 | $5 | $25 | 200K | 레거시 호환 |

#### Codex CLI 모델 (GPT-5.6 Sol/Terra/Luna + Pro 변형 포함)

| 모델 | Input $/1M | Output $/1M | Cached | Context | 용도 |
|------|-----------|------------|--------|---------|------|
| **GPT-5.6 Sol** | $5.00 | $30.00 | $0.50 | **1.05M** | 플래그십 — 복합 추론·코딩 최강 |
| **GPT-5.6 Sol Pro** | $5.00 | $30.00 | $0.50 | **1.05M** | Sol + `reasoning.mode=pro` — 심층 추론 |
| **GPT-5.6 Terra** | $2.00 | $12.00 | $0.20 | **1.05M** | **2순위: 코드 수정** (Sol 절반 비용) |
| **GPT-5.6 Terra Pro** | $2.00 | $12.00 | $0.20 | **1.05M** | Terra + `reasoning.mode=pro` — 비용 효율 심층 추론 |
| **GPT-5.6 Luna** | $0.20 | $1.20 | $0.02 | **1.05M** | **진단 폴백** (초저비용 대량 처리) |
| **GPT-5.6 Luna Pro** | $0.20 | $1.20 | $0.02 | **1.05M** | Luna + `reasoning.mode=pro` — 저비용 고품질 추론 |
| GPT-5.5 | $5.00 | $30.00 | $0.50 | 1.05M | 이전 세대 플래그십 (프로덕션 안정) |
| GPT-5.5 Pro | $30.00 | $180.00 | — | 1.05M | 최고급 추론 (극고가, 특수 케이스만) |
| GPT-5.4 | $2.50 | $15.00 | $0.25 | 1.05M | 범용 코딩·추론 (구세대 안정) |
| GPT-5.4-mini | $0.75 | $4.50 | $0.075 | 400K | 경량 코딩/서브에이전트 |
| GPT-5.4-nano | $0.20 | $1.25 | $0.02 | 400K | 초경량 분류/필터 |
| ~~GPT-5.3-Codex~~ | ~~$1.75~~ | ~~$14.00~~ | — | 400K | **세대교체됨** → GPT-5.6 Terra 대체 |

> **Pro 변형**: `reasoning.mode=pro`를 설정한 동일 모델. 복잡한 작업에서 더 높은 품질의 응답을 생성하며, 가격 동일(추론 시간만 증가). [OpenAI 공식문서, 2026-08-02]
> **출시일**: GPT-5.6 시리즈 2026-06-26 프리뷰, 2026-07-09 공식 출시 [OpenAI 공식, 2026-07-09]
> **Batch API**: 모든 모델에서 Input/Output **-50% 할인** (비동기 24시간 처리) [OpenAI 가격표, 2026-08-02]
> **참고**: GPT-5.4/5.4-mini는 2026-08-31부로 ChatGPT 로그인 Codex에서 제거 예정이나, API 키 인증 모드에서는 계속 사용 가능.

### 2.2 기능 비교

| 기능 | Claude Code CLI | Codex CLI | 우위 |
|------|----------------|-----------|------|
| **비대화형 실행** | `claude -p "prompt" --bare` | `codex exec "prompt" --full-auto` | 동등 |
| **파일 편집** | ✅ Edit/Write 도구 내장 | ✅ 파일 편집 내장 | 동등 |
| **도구 사용(MCP)** | ✅ `--mcp-config` 네이티브 | ✅ `config.toml [mcp_servers]` | **Claude 우위** (AADS MCP 즉시 연동) |
| **서브에이전트** | ✅ Agent SDK 내장 | ✅ `spawn_agent`/`wait`/`close_agent` | 동등 |
| **구조화 출력** | ✅ `--output-format json` | ✅ `--output-schema` (JSON Schema 강제) | **Codex 우위** |
| **샌드박스** | ❌ 없음 (OS 레벨) | ✅ `--sandbox workspace-write` | **Codex 우위** |
| **자동 허용 도구** | ✅ `--allowedTools` 화이트리스트 | ✅ `--full-auto` / 3단계 승인모드 | Codex 우위 |
| **인증 체계** | OAuth 토큰 (`sk-ant-oat01-`) | API 키 (`OPENAI_API_KEY`) | **Claude 우위** (AADS 기존 인증 재사용) |
| **이미지 첨부** | ✅ | ✅ | 동등 |
| **추론 제어** | `--model` 선택으로 간접 | `reasoning.mode` (pro) + `reasoning_effort` (low~xhigh) **2축 제어** | **Codex 우위** (세밀 제어) |
| **캐시 할인** | ❌ | ✅ Input 90% 할인 (캐시 히트 시) | **Codex 우위** |
| **Programmatic Tool Calling** | ❌ | ✅ 도구 호출을 프로그래밍 방식으로 정의·실행 | **Codex 우위** (5.6 신기능) |
| **Ultra Multi-Agent** | ✅ (Workflow/Agent 내장) | ✅ 에이전트 간 동시 협업 모드 | 동등 (5.6에서 추가) |
| **컨텍스트 윈도우** | 200K (전 모델) | **1.05M** (전 5.6 모델) | **Codex 우위** (5.25배 넓음) |
| **배치 처리 할인** | ❌ | ✅ Batch API -50% (비동기 24h) | **Codex 우위** |
| **컴퓨터 사용** | ✅ Computer Use 도구 | ✅ Computer Use 내장 | 동등 |

> **Programmatic Tool Calling** (GPT-5.6 신기능): 도구 호출을 JSON으로 프로그래밍 방식으로 정의하고 모델이 자동으로 실행하는 기능. 기존 function calling보다 더 안정적이고 구조화된 도구 파이프라인 구성 가능. [OpenAI 공식, 2026-07-09]

### 2.3 모델별 최적 역할 배정

```
┌─────────────────────────────────────────────────────────────┐
│                 자율 루프 에이전트 — 모델 라우팅              │
│                                                             │
│  [진단] 반복 빈도 높음, 90%는 "정상" 1줄 응답                │
│  ├─ 1순위: Claude Haiku 4.5 ($1/M) — AADS 네이티브         │
│  ├─ 2순위: GPT-5.6 Luna ($0.20/M) — 초저비용               │
│  ├─ 2-1순위: GPT-5.6 Luna Pro ($0.20/M) — 복잡 진단 시     │
│  └─ 3순위: Groq Llama 70B (~$0/M) — 무료                   │
│                                                             │
│  [코드 수정] 발생 빈도 낮음, 정확도 최우선                    │
│  ├─ 1순위: Claude Sonnet 5 ($2~3/M) — MCP 연동, 정확도 최고  │
│  ├─ 2순위: GPT-5.6 Terra ($2/M) — 동급 성능, 캐시 할인      │
│  ├─ 2-1순위: GPT-5.6 Terra Pro ($2/M) — 복잡 수정 시        │
│  └─ 3순위: GPT-5.6 Sol ($5/M) — 최강 추론 (복잡 케이스)     │
│                                                             │
│  [복잡 아키텍처] 극소 빈도                                    │
│  ├─ 1순위: Claude Opus 5 ($5/M) — 서브에이전트 활용          │
│  ├─ 2순위: GPT-5.6 Sol Pro ($5/M) — 심층 추론 + 1.05M ctx  │
│  └─ 3순위: GPT-5.5 Pro ($30/M) — 극고가 최종 수단           │
│                                                             │
│  예상 일일 비용: ~$0.30~$1.50 (감시 위주)                     │
│  에러 발생 시: +$0.50~$5.00 (수정 작업 포함)                  │
└─────────────────────────────────────────────────────────────┘
```

### 2.4 이중 폴백 구조 (상호 보완)

```
┌─────────────────────────────────────────────────────────┐
│                    자율 루프 에이전트                      │
│                                                         │
│  [1순위] Claude Code CLI (Sonnet 5)                     │
│     ├─ 장점: AADS MCP 직접 연동, OAuth 재사용            │
│     ├─ 강점: 도구 생태계 밀착, 서브에이전트               │
│     └─ 실패 시 ↓                                        │
│                                                         │
│  [2순위] Codex CLI (GPT-5.6 Terra / Terra Pro)          │
│     ├─ 장점: 샌드박스 격리, 캐시 할인, 2축 추론 제어      │
│     ├─ 강점: 1.05M 컨텍스트, Programmatic Tool Calling   │
│     ├─ Terra Pro: 복잡 케이스 시 reasoning.mode=pro      │
│     └─ 실패 시 ↓                                        │
│                                                         │
│  [3순위] Codex CLI (GPT-5.6 Sol / Sol Pro)              │
│     ├─ 장점: 최강 추론력, 1.05M 컨텍스트                 │
│     ├─ 용도: 다른 모델이 모두 실패한 복잡 에러            │
│     └─ 실패 시 ↓                                        │
│                                                         │
│  [4순위] LiteLLM API (GPT-5.6 Luna / Luna Pro / Groq)   │
│     ├─ 장점: $0.20/M 초저비용, 고속                      │
│     ├─ 용도: 단순 진단, 로그 분석, 알림 메시지 생성       │
│     └─ 실패 시 → 텔레그램 CEO 긴급 알림                  │
└─────────────────────────────────────────────────────────┘
```

**상호 보완 포인트**:
- **Claude**: AADS 도구 생태계(MCP, 서브에이전트)와 밀착 → 정밀한 코드 수정에 최적
- **Codex Terra/Pro**: 샌드박스 격리 + 캐시 할인 + 2축 추론 제어 + 1.05M 컨텍스트 → Claude 장애 시 안전한 대행, 대규모 코드베이스 분석에 유리
- **Codex Sol/Pro**: 최강 추론력 → 다른 모델이 해결하지 못한 복잡 문제의 최종 수단
- **Luna/Groq**: 진단 전용 초저비용 경로 → 감시 90%의 "이상 없음" 응답을 최소 비용으로 처리
- **Pro 변형 활용**: 동일 비용으로 더 높은 추론 품질 → 복잡 진단/수정 시 reasoning.mode=pro로 승격

---

## 3. 아키텍처

### 3.1 전체 흐름

```
                    ┌──────────────┐
                    │  APScheduler  │
                    │  loop_tick    │
                    │  (30초 폴링)  │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ run_iteration │
                    │ (loop_executor)│
                    └──────┬───────┘
                           │
              ┌────────────▼────────────┐
              │  _execute_autonomous()   │
              │  (신규 함수)              │
              └────────────┬────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
    ┌────▼────┐      ┌────▼────┐      ┌────▼────┐
    │ Phase 1  │      │ Phase 2  │      │ Phase 3  │
    │ 진단     │      │ 수정     │      │ 검증     │
    │(Haiku/   │      │(Sonnet5/ │      │ (자동)   │
    │ Luna)    │      │ Terra)   │      │          │
    └────┬────┘      └────┬────┘      └────┬────┘
         │                │                │
    로그 분석        코드 수정/배포     헬스체크/롤백
    원인 분류        테스트 실행        결과 기록
    위험도 판정      git commit         다음 루프 전달
```

### 3.2 Phase 1: 진단 (Diagnosis)

**실행 모델**: Haiku 4.5 (저비용 고속) → GPT-5.6 Luna 폴백 → Luna Pro (복잡 진단) → Groq 폴백

```python
async def _diagnose(loop: dict, context: str) -> dict:
    """
    반환: {
        "issue_type": "service_down|code_error|config_error|performance|data_error",
        "severity": "critical|high|medium|low",
        "root_cause": "에러 원인 요약",
        "recommended_action": "restart_service|patch_code|update_config|scale_resource|alert_only",
        "affected_files": ["app/services/xxx.py"],
        "confidence": 0.85
    }
    """
```

| 진단 소스 | 수집 방법 | 용도 |
|-----------|----------|------|
| Docker 로그 | `docker logs --since 5m aads-server` | 런타임 에러 감지 |
| 헬스체크 API | `GET /api/v1/ops/health-check` | 서비스 상태 |
| DB 에러 로그 | `SELECT * FROM error_log ORDER BY id DESC LIMIT 20` | 누적 에러 패턴 |
| 프로세스 상태 | `docker ps`, `systemctl status` | 서비스 alive 여부 |
| 메트릭 | Grafana API (있을 경우) | 성능 지표 |

### 3.3 Phase 2: 수정 (Repair)

**위험 등급별 자동 조치 범위**:

| 위험 등급 | 조치 유형 | 자동 실행 | 예시 |
|-----------|----------|-----------|------|
| **Level 0** (무위험) | 서비스 재시작 | ✅ 즉시 | `docker restart`, `systemctl restart` |
| **Level 1** (저위험) | 설정 변경 | ✅ 즉시 | 환경변수 수정, nginx 리로드 |
| **Level 2** (중위험) | 코드 패치 | ✅ 테스트 통과 후 | 버그 수정, 예외 처리 추가 |
| **Level 3** (고위험) | 아키텍처 변경 | ✅ 테스트+스테이징 후 | DB 스키마 변경, API 인터페이스 변경 |
| **Level 4** (위험) | 보안/인증 관련 | ⚠️ CEO 알림 후 자동 | 인증 로직, 권한 체계 |

**CLI 호출 구조**:

```python
async def _repair_with_cli(diagnosis: dict, loop: dict) -> dict:
    """Claude CLI 1순위 → Codex CLI 2순위 폴백"""

    # 1순위: Claude Code CLI (Sonnet 5)
    result = await _invoke_claude_cli(
        prompt=f"다음 문제를 수정해: {diagnosis['root_cause']}",
        model="sonnet",
        allowed_tools=["Read", "Edit", "Write", "Bash"],
        max_tokens=8000,
        working_dir=PROJECT_DIRS[loop["project"]],
    )

    if result["success"]:
        return result

    # 2순위: Codex CLI (GPT-5.6 Terra — 샌드박스 격리)
    result = await _invoke_codex_cli(
        prompt=f"Fix this issue: {diagnosis['root_cause']}",
        model="gpt-5.6-terra",
        sandbox="workspace-write",
        reasoning_effort="high",
        output_schema=REPAIR_RESULT_SCHEMA,
        working_dir=PROJECT_DIRS[loop["project"]],
    )

    if result["success"]:
        return result

    # 2-1순위: 복잡 케이스 — GPT-5.6 Terra Pro (Pro 추론 모드)
    if diagnosis["severity"] in ("high", "critical"):
        result = await _invoke_codex_cli(
            prompt=f"Fix this complex issue: {diagnosis['root_cause']}",
            model="gpt-5.6-terra",
            sandbox="workspace-write",
            reasoning_effort="high",
            reasoning_mode="pro",
            output_schema=REPAIR_RESULT_SCHEMA,
            working_dir=PROJECT_DIRS[loop["project"]],
        )
        if result["success"]:
            return result

    # 3순위: 최종 — GPT-5.6 Sol (최강 추론)
    if diagnosis["severity"] == "critical":
        result = await _invoke_codex_cli(
            prompt=f"Fix this critical issue: {diagnosis['root_cause']}",
            model="gpt-5.6-sol",
            sandbox="workspace-write",
            reasoning_effort="xhigh",
            output_schema=REPAIR_RESULT_SCHEMA,
            working_dir=PROJECT_DIRS[loop["project"]],
        )
        if result["success"]:
            return result

    return {"success": False, "fallback": "all_failed", "message": "CLI 모두 실패"}
```

**Claude CLI 호출 상세**:

```python
async def _invoke_claude_cli(
    prompt: str,
    model: str = "sonnet",
    allowed_tools: list[str] = None,
    max_tokens: int = 8000,
    working_dir: str = "/root/aads/aads-server",
) -> dict:
    """
    claude -p "prompt" --bare --model sonnet \
        --allowedTools "Read,Edit,Write,Bash" \
        --output-format json \
        --max-turns 10
    """
    cmd = [
        "claude", "-p", prompt,
        "--bare",
        "--model", model,
        "--output-format", "json",
        "--max-turns", "10",
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    env = {
        **os.environ,
        "ANTHROPIC_AUTH_TOKEN": os.getenv("ANTHROPIC_AUTH_TOKEN"),
    }

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=working_dir,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=300  # 5분 타임아웃
    )

    if proc.returncode == 0:
        result = json.loads(stdout.decode())
        return {"success": True, "cli": "claude", "model": model, "result": result}

    return {"success": False, "cli": "claude", "error": stderr.decode()[:500]}
```

**Codex CLI 호출 상세 (GPT-5.6 + Pro 변형 반영)**:

```python
async def _invoke_codex_cli(
    prompt: str,
    model: str = "gpt-5.6-terra",
    sandbox: str = "workspace-write",
    reasoning_effort: str = "high",
    reasoning_mode: str = None,  # "pro" for Pro variants
    output_schema: dict = None,
    working_dir: str = "/root/aads/aads-server",
) -> dict:
    """
    codex exec "prompt" --model gpt-5.6-terra \
        --full-auto --sandbox workspace-write \
        --json --output-schema schema.json
    """
    cmd = [
        "codex", "exec", prompt,
        "--model", model,
        "--full-auto",
        "--sandbox", sandbox,
        "--json",
    ]
    if output_schema:
        schema_path = f"/tmp/codex_schema_{uuid4().hex[:8]}.json"
        async with aiofiles.open(schema_path, "w") as f:
            await f.write(json.dumps(output_schema))
        cmd.extend(["--output-schema", schema_path])

    env = {
        **os.environ,
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
    }

    # config.toml에서 reasoning_effort + reasoning_mode 설정
    config_path = f"/tmp/codex_config_{uuid4().hex[:8]}.toml"
    config_content = f'model = "{model}"\nmodel_reasoning_effort = "{reasoning_effort}"\n'
    if reasoning_mode == "pro":
        config_content += f'reasoning_mode = "pro"\n'
    async with aiofiles.open(config_path, "w") as f:
        await f.write(config_content)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=working_dir,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=300
    )

    if proc.returncode == 0:
        events = [json.loads(line) for line in stdout.decode().strip().split("\n") if line.strip()]
        return {"success": True, "cli": "codex", "model": model, "reasoning_mode": reasoning_mode, "events": events}

    return {"success": False, "cli": "codex", "error": stderr.decode()[:500]}
```

### 3.4 Phase 3: 검증 및 배포 (Verify & Deploy)

```python
async def _verify_and_deploy(repair_result: dict, loop: dict) -> dict:
    """수정 후 검증 → 배포 → 헬스체크 → 롤백 판정"""

    project = loop.get("project", "AADS")

    # Step 1: 구문 검사
    syntax_ok = await _run_syntax_check(project, repair_result["changed_files"])
    if not syntax_ok:
        await _rollback_changes(project)
        return {"deployed": False, "reason": "syntax_error"}

    # Step 2: 단위 테스트
    test_ok = await _run_unit_tests(project)
    if not test_ok:
        await _rollback_changes(project)
        return {"deployed": False, "reason": "test_failure"}

    # Step 3: Git 커밋 (자동)
    commit_hash = await _auto_commit(
        project,
        message=f"fix(auto): {repair_result['summary'][:50]}",
        files=repair_result["changed_files"],
    )

    # Step 4: 무중단 배포
    deploy_ok = await _deploy(project)
    if not deploy_ok:
        await _rollback_deploy(project)
        return {"deployed": False, "reason": "deploy_failure"}

    # Step 5: 헬스체크 (30초 대기 후)
    await asyncio.sleep(30)
    health_ok = await _health_check(project)
    if not health_ok:
        await _rollback_deploy(project)
        await _revert_commit(project, commit_hash)
        return {"deployed": False, "reason": "health_check_failure", "rolled_back": True}

    return {
        "deployed": True,
        "commit": commit_hash,
        "changed_files": repair_result["changed_files"],
        "cli_used": repair_result["cli"],
        "model_used": repair_result["model"],
    }
```

---

## 4. 모델 자동 라우팅 (비용 최적화)

### 4.1 작업 유형별 모델 선택

```python
MODEL_ROUTING = {
    # Phase 1: 진단 (저비용 우선)
    "diagnose": {
        "primary":    {"cli": "api",   "model": "claude-haiku-4-5-20251001", "cost_out_1m": 5.0},
        "fallback_1": {"cli": "api",   "model": "gpt-5.6-luna",             "cost_out_1m": 1.2},
        "fallback_2": {"cli": "api",   "model": "gpt-5.6-luna",  "reasoning_mode": "pro", "cost_out_1m": 1.2},
        "fallback_3": {"cli": "api",   "model": "groq-llama-70b",           "cost_out_1m": 0.0},
    },

    # Phase 2: 코드 수정 (성능 우선)
    "repair_code": {
        "primary":    {"cli": "claude", "model": "sonnet",          "cost_out_1m": 10.0},
        "fallback_1": {"cli": "codex",  "model": "gpt-5.6-terra",  "cost_out_1m": 12.0},
        "fallback_2": {"cli": "codex",  "model": "gpt-5.6-terra",  "reasoning_mode": "pro", "cost_out_1m": 12.0},
        "fallback_3": {"cli": "codex",  "model": "gpt-5.6-sol",    "cost_out_1m": 30.0},
    },

    # Phase 2: 서비스 재시작 (CLI 불필요)
    "restart_service": {
        "primary":    {"cli": "direct", "command": "docker restart"},
        "fallback_1": {"cli": "direct", "command": "systemctl restart"},
    },

    # Phase 2: 설정 변경 (경량 CLI)
    "update_config": {
        "primary":    {"cli": "claude", "model": "haiku",          "cost_out_1m": 5.0},
        "fallback_1": {"cli": "codex",  "model": "gpt-5.6-luna",  "reasoning_mode": "pro", "cost_out_1m": 1.2},
    },

    # Phase 3: 검증 (도구 직접 실행)
    "verify": {
        "primary":    {"cli": "direct", "command": "pytest + health_check"},
    },
}
```

### 4.2 비용 한도

| 항목 | 기본값 | 설명 |
|------|--------|------|
| 단일 iteration 한도 | $0.50 | 1회 진단+수정+검증 합산 |
| 루프 전체 한도 | $10.00 | monitor 타입 기준 |
| 일일 자율 조치 한도 | $20.00 | 전체 활성 루프 합산 |
| 모델 폴백 시 비용 재계산 | ✅ | 상위 모델 실패 → 하위 모델 사용 시 한도 상향 조정 |
| Batch API 적용 | ✅ | 비긴급 진단은 Batch API(-50%)로 처리 |

### 4.3 비용 시뮬레이션

| 시나리오 | 진단 모델 | 수정 모델 | 예상 비용 |
|----------|----------|----------|----------|
| 정상 감시 (1일 48회) | Haiku × 48 | — | ~$0.05 |
| 정상 감시 (Luna Batch) | Luna × 48 (Batch -50%) | — | ~$0.006 |
| 경미한 에러 (서비스 재시작) | Haiku × 1 | 직접 실행 | ~$0.001 |
| 코드 버그 수정 (Sonnet 5) | Haiku × 1 | Sonnet 5 × 1 | ~$0.10 |
| 코드 버그 (Sonnet 실패 → Terra Pro 폴백) | Haiku × 1 | Sonnet + Terra Pro | ~$0.20 |
| 복잡 에러 (Sol 동원) | Haiku × 1 | Sonnet + Terra + Sol | ~$0.80 |
| **월간 예상 (에러 3건/주 가정)** | — | — | **~$5~15** |

---

## 5. 안전장치

### 5.1 자동 롤백 체계

```
수정 적용
  ├─ 구문 검사 실패 → git checkout -- <files> (즉시 롤백)
  ├─ 테스트 실패 → git checkout -- <files> (즉시 롤백)
  ├─ 배포 실패 → 이전 슬롯 복원 (Blue/Green)
  ├─ 헬스체크 실패 → 이전 슬롯 복원 + git revert
  └─ 다음 루프에서 동일 에러 재발 → 자율 조치 중단, CEO 알림
```

### 5.2 보호 규칙

| 규칙 | 설명 |
|------|------|
| **수정 금지 파일** | `auth_provider.py`, `model_selector.py`, `.env`, `docker-compose.yml`, `deploy.sh` |
| **수정 금지 디렉토리** | `.git/`, `migrations/`, `scripts/deploy*` |
| **동일 에러 반복 차단** | 같은 에러에 2회 연속 자율 수정 실패 → 3회차부터 CEO 알림만 |
| **동시 수정 차단** | 1개 프로젝트에 동시에 1개 자율 수정만 허용 |
| **비용 초과 차단** | iteration 비용 > $0.50 → 즉시 중단 + CEO 알림 |
| **커밋 규칙 준수** | `--no-verify` 절대 금지. pre-commit hook 통과 필수 |
| **인증 파일 보호** | R-AUTH 규칙 적용. 인증 관련 파일 수정 불가 → CEO 알림 |

### 5.3 감사 로그 (Audit Trail)

```sql
CREATE TABLE autonomous_actions (
    id              SERIAL PRIMARY KEY,
    loop_id         INTEGER REFERENCES ohvis_loops(id),
    iteration_num   INTEGER NOT NULL,
    action_type     VARCHAR(50) NOT NULL,  -- diagnose/repair/deploy/rollback/alert
    severity        VARCHAR(20),           -- critical/high/medium/low
    cli_used        VARCHAR(20),           -- claude/codex/api/direct
    model_used      VARCHAR(50),           -- claude-sonnet-5/gpt-5.6-terra/gpt-5.6-terra-pro/...
    reasoning_mode  VARCHAR(10),           -- NULL/pro (Pro 변형 사용 시)
    diagnosis       JSONB,
    repair_summary  TEXT,
    changed_files   TEXT[],
    commit_hash     VARCHAR(40),
    deployed        BOOLEAN DEFAULT FALSE,
    rolled_back     BOOLEAN DEFAULT FALSE,
    cost_usd        DECIMAL(10,4),
    duration_ms     INTEGER,
    success         BOOLEAN,
    error_message   TEXT,
    acted_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_autonomous_actions_loop ON autonomous_actions(loop_id);
CREATE INDEX idx_autonomous_actions_acted ON autonomous_actions(acted_at);
```

---

## 6. 멀티 프로젝트 지원

### 6.1 프로젝트별 설정

```python
PROJECT_CONFIG = {
    "AADS": {
        "workdir": "/root/aads/aads-server",
        "deploy_cmd": "bash /root/aads/aads-server/deploy.sh bluegreen",
        "reload_cmd": "docker exec aads-server bash /app/scripts/reload-api.sh",
        "test_cmd": "docker exec aads-server python3 -m pytest tests/unit/ -x -q",
        "health_url": "https://aads.newtalk.kr/api/v1/ops/health-check",
        "cli_auth_env": "ANTHROPIC_AUTH_TOKEN",
    },
    "GO100": {
        "workdir": "/root/aads/go100",
        "deploy_cmd": "systemctl restart go100-scheduler",
        "reload_cmd": None,
        "test_cmd": "cd /root/aads/go100 && python3 -m pytest tests/ -x -q",
        "health_url": None,
        "cli_auth_env": "ANTHROPIC_AUTH_TOKEN",
    },
    "KIS": {
        "workdir": "/root/aads/kis-api",
        "deploy_cmd": "systemctl restart kis-v41-api",
        "reload_cmd": None,
        "test_cmd": "cd /root/aads/kis-api && python3 -m pytest tests/ -x -q",
        "health_url": None,
        "cli_auth_env": "ANTHROPIC_AUTH_TOKEN",
    },
    "NTV2": {
        "workdir": "/root/newtalk-v2",
        "deploy_cmd": "ssh server114 'cd /app && docker compose up -d --build ntv2-api'",
        "reload_cmd": None,
        "test_cmd": "ssh server114 'cd /app && python3 -m pytest tests/ -x -q'",
        "health_url": "https://newtalk.kr/api/health",
        "cli_auth_env": "ANTHROPIC_AUTH_TOKEN",
    },
    "SF": {
        "workdir": "/root/soulful",
        "deploy_cmd": "ssh server114 'cd /app/sf && docker compose up -d --build'",
        "reload_cmd": None,
        "test_cmd": None,
        "health_url": None,
        "cli_auth_env": "ANTHROPIC_AUTH_TOKEN",
    },
}
```

### 6.2 프로젝트별 CLI 가용성

| 프로젝트 | 서버 | Claude CLI | Codex CLI | LiteLLM |
|----------|------|-----------|-----------|---------|
| AADS | 68 | ✅ 설치됨 | 🔧 설치 필요 | ✅ 가동 중 |
| GO100 | 68 | ✅ 공유 | 🔧 설치 필요 | ✅ 공유 |
| KIS | 68 | ✅ 공유 | 🔧 설치 필요 | ✅ 공유 |
| NTV2 | 114 | 🔧 설치 필요 | 🔧 설치 필요 | 🔧 프록시 필요 |
| SF | 114 | 🔧 설치 필요 | 🔧 설치 필요 | 🔧 프록시 필요 |

---

## 7. 구현 계획

### 7.1 Phase 구분

| Phase | 내용 | 예상 기간 | 우선순위 |
|-------|------|----------|---------|
| **Phase 1** | DB 테이블 + 진단 엔진 + Level 0 자동 조치 (서비스 재시작) | 1일 | P0 |
| **Phase 2** | Claude CLI 연동 + Level 1-2 코드 수정 + 자동 테스트 | 2일 | P0 |
| **Phase 3** | Codex CLI(GPT-5.6 + Pro) 폴백 설치/연동 + 이중 폴백 구현 | 1일 | P1 |
| **Phase 4** | 자동 배포 (Blue/Green) + 자동 롤백 | 1일 | P0 |
| **Phase 5** | 멀티 프로젝트 확장 (GO100, KIS) | 1일 | P1 |
| **Phase 6** | 원격 서버 확장 (NTV2, SF) + CLI 설치 | 1일 | P2 |
| **Phase 7** | 감사 대시보드 UI + CEO 알림 고도화 | 1일 | P2 |

### 7.2 Phase 1 상세 (P0, 1일)

**수정 대상 파일**:

1. `migrations/120_autonomous_actions.sql` — 감사 테이블 생성
2. `app/services/loop_executor.py`
   - `_execute_by_type()` L237에 `"autonomous"` 분기 추가
   - `_execute_autonomous()` 신규 함수 추가 (진단 + Level 0 조치)
3. `app/services/loop_controller.py`
   - `_DEFAULT_LIMITS`에 `"autonomous"` 타입 추가
   - `create_loop()`에서 `"autonomous"` 허용
4. `app/services/loop_chat_handler.py`
   - `LOOP_START_KW`에 "자동 수정", "자율 감시", "자동 조치" 키워드 추가
   - `detect_loop_intent()`에서 자율 모드 감지
5. `app/services/autonomous_repair.py` — **신규 모듈**
   - `diagnose()` — 진단 엔진
   - `repair_level_0()` — 서비스 재시작
   - `record_action()` — 감사 로그 기록

### 7.3 Phase 2 상세 (P0, 2일)

1. `app/services/autonomous_repair.py` 확장
   - `_invoke_claude_cli()` — Claude Code CLI subprocess 호출
   - `_invoke_codex_cli()` — Codex CLI subprocess 호출 (Phase 3에서 활성화)
   - `repair_level_1()` — 설정 변경
   - `repair_level_2()` — 코드 패치 (Claude CLI)
2. `app/services/autonomous_deploy.py` — **신규 모듈**
   - `auto_commit()` — git add + commit (pre-commit hook 준수)
   - `auto_deploy()` — reload-api.sh 또는 deploy.sh bluegreen
   - `verify_health()` — 헬스체크
   - `rollback()` — 자동 롤백

### 7.4 Phase 3 상세 (P1, 1일)

1. Codex CLI 설치: `npm install -g @openai/codex`
2. `.env`에 `OPENAI_API_KEY` 추가
3. Codex config: `~/.codex/config.toml`
   ```toml
   [defaults]
   model = "gpt-5.6-terra"
   approval_mode = "full-auto"

   [profiles.diagnose]
   model = "gpt-5.6-luna"
   model_reasoning_effort = "low"

   [profiles.diagnose-pro]
   model = "gpt-5.6-luna"
   model_reasoning_effort = "medium"
   reasoning_mode = "pro"

   [profiles.repair]
   model = "gpt-5.6-terra"
   sandbox = "workspace-write"
   model_reasoning_effort = "high"

   [profiles.repair-pro]
   model = "gpt-5.6-terra"
   sandbox = "workspace-write"
   model_reasoning_effort = "high"
   reasoning_mode = "pro"

   [profiles.critical]
   model = "gpt-5.6-sol"
   sandbox = "workspace-write"
   model_reasoning_effort = "xhigh"

   [profiles.critical-pro]
   model = "gpt-5.6-sol"
   sandbox = "workspace-write"
   model_reasoning_effort = "xhigh"
   reasoning_mode = "pro"
   ```
4. `autonomous_repair.py`에서 `_invoke_codex_cli()` 활성화

### 7.5 완료 판정 기준

| 항목 | 기준 |
|------|------|
| Level 0 | 서비스 다운 감지 → 자동 재시작 → 헬스체크 통과 (E2E 테스트) |
| Level 1 | 설정 오류 감지 → 자동 수정 → 테스트 통과 → 배포 (E2E 테스트) |
| Level 2 | 코드 에러 감지 → Claude CLI 수정 → 테스트 통과 → 배포 → 헬스체크 (E2E 테스트) |
| 폴백 | Claude 실패 → Codex(Terra) 성공 → 배포 (E2E 테스트) |
| Pro 폴백 | Terra 실패 → Terra Pro 성공 → 배포 (E2E 테스트) |
| 에스컬레이션 | Terra Pro 실패 → Sol 성공 → 배포 (E2E 테스트) |
| 롤백 | 수정 후 헬스체크 실패 → 자동 롤백 → 이전 상태 복원 (E2E 테스트) |
| 감사 | 모든 조치가 `autonomous_actions` 테이블에 기록됨 확인 |

---

## 8. 리스크 및 대응

| 리스크 | 확률 | 영향 | 대응 |
|--------|------|------|------|
| AI가 잘못된 코드를 배포 | 중 | 심각 | 자동 롤백 + 동일 에러 2회 차단 + pre-commit hook |
| 무한 수정 루프 | 중 | 비용 | 동일 에러 2회 실패 → 자율 중단 + CEO 알림 |
| CLI 프로세스 좀비화 | 저 | 중 | 5분 타임아웃 + 프로세스 킬 |
| 비용 폭주 | 저 | 비용 | iteration $0.50, 일일 $20 하드 캡 |
| 보안 파일 수정 시도 | 저 | 심각 | 수정 금지 파일 리스트 + pre-commit hook |
| GPT-5.4 모델 폐기 | 확정 (08-31) | 낮음 | GPT-5.6 Terra/Luna로 이미 대체 설계 |
| Pro 모드 추론 시간 증가 | 중 | 낮음 | 5분 타임아웃 동일 적용, 비긴급 시 Pro 생략 |

---

## 9. CEO 지시문 예시

### 9.1 자율 감시 루프 시작

```
"AADS 서버 자율 감시해 이상 있으면 자동 수정하고 배포까지 진행해"
→ autonomous 타입 루프 생성, 30분 간격, 100회 반복
```

### 9.2 GO100 매매 시스템 자율 감시

```
"GO100 상한가따라잡기 자율 감시해 에러 발생하면 즉시 자동 수정해"
→ autonomous 타입 루프 생성, project=GO100, 10분 간격
```

### 9.3 멀티 프로젝트 동시 감시

```
"모든 서비스 자율 감시해"
→ AADS/GO100/KIS 각각 autonomous 루프 3개 동시 생성
```

---

## 10. 교훈 및 참고

- **AADS-190 Phase 2 서브에이전트**: `spawn_subagent`는 도구 사용 가능한 독립 LLM 호출로, 자율 루프의 진단 단계에 재사용 가능
- **Pipeline Runner**: `pipeline_runner_submit`은 CEO 승인 게이트가 내장되어 있어 C안에서는 우회하되, 감사 로그로 대체
- **Blue/Green 배포**: `deploy.sh bluegreen`은 이전 슬롯이 살아있어 즉시 롤백 가능 — C안의 안전망
- **Pre-commit Hook**: 5단계 검증(API 키 탐지, 구문 검사, ruff, Docker import, 단위 테스트)이 자동 커밋의 품질 게이트 역할
- **R-AUTH 규칙**: Claude CLI는 `ANTHROPIC_AUTH_TOKEN`(OAuth)을 사용하며, `ANTHROPIC_API_KEY`에 동일 값 복사만 허용
- **Pro 변형 활용**: reasoning.mode=pro는 동일 비용으로 추론 품질을 높이므로, 복잡 에러 시 Pro 승격을 우선하고 상위 모델 에스컬레이션은 최후 수단

---

## 부록 A: Codex CLI 설치 및 GPT-5.6 설정 (Pro 변형 포함)

```bash
# 설치
npm install -g @openai/codex

# API 키 설정 (.env에 추가)
echo 'OPENAI_API_KEY=sk-...' >> /root/aads/aads-server/.env

# 설정 파일 (Pro 프로파일 포함)
mkdir -p ~/.codex
cat > ~/.codex/config.toml << 'EOF'
[defaults]
model = "gpt-5.6-terra"
approval_mode = "full-auto"

[profiles.diagnose]
model = "gpt-5.6-luna"
model_reasoning_effort = "low"

[profiles.diagnose-pro]
model = "gpt-5.6-luna"
model_reasoning_effort = "medium"
reasoning_mode = "pro"

[profiles.repair]
model = "gpt-5.6-terra"
sandbox = "workspace-write"
model_reasoning_effort = "high"

[profiles.repair-pro]
model = "gpt-5.6-terra"
sandbox = "workspace-write"
model_reasoning_effort = "high"
reasoning_mode = "pro"

[profiles.critical]
model = "gpt-5.6-sol"
sandbox = "workspace-write"
model_reasoning_effort = "xhigh"

[profiles.critical-pro]
model = "gpt-5.6-sol"
sandbox = "workspace-write"
model_reasoning_effort = "xhigh"
reasoning_mode = "pro"
EOF

# 비대화형 모드 테스트
codex exec "list files in current directory" --full-auto --json --model gpt-5.6-terra

# Pro 모드 테스트
codex exec "analyze this code for bugs" --full-auto --json --model gpt-5.6-terra --reasoning-mode pro
```

## 부록 B: Claude CLI 설정 (서버 68 기준)

```bash
# 이미 설치됨 (/usr/local/bin/claude)
claude --version

# 비대화형 모드 테스트
claude -p "echo hello" --bare --model haiku --output-format json

# MCP 설정 (AADS 도구 연동)
cat > ~/.claude/mcp_config.json << 'EOF'
{
  "mcpServers": {
    "aads-tools": {
      "command": "node",
      "args": ["/root/aads/aads-mcp-server/dist/index.js"]
    }
  }
}
EOF
```

---

## 부록 C: v2 → v3 변경 사항 요약

| 항목 | v2 | v3 |
|------|----|----|
| **Codex 모델** | Sol/Terra/Luna 3종 | + **Sol Pro/Terra Pro/Luna Pro** 3종 추가 (6종) |
| **GPT-5.5 Pro** | 미수록 | 추가 ($30/$180, 극고가 최종 수단) |
| **GPT-5.4 base** | 미수록 | 추가 ($2.50/$15.00) |
| **컨텍스트 윈도우** | 미표기 | **1.05M** (5.6 전 모델), 200K (Claude), 400K (5.4) |
| **Programmatic Tool Calling** | 미수록 | Codex 5.6 신기능으로 추가 |
| **Ultra Multi-Agent** | 미수록 | 양측 동등 기능으로 추가 |
| **Batch API -50%** | 미수록 | 비긴급 진단에 적용 가능 |
| **reasoning.mode=pro** | 미수록 | 2축 추론 제어 (mode + effort) 반영 |
| **폴백 단계** | 3단계 (Claude→Terra→Sol) | **4단계** (Claude→Terra→Terra Pro→Sol) |
| **감사 테이블** | 7컬럼 | + `reasoning_mode` 컬럼 추가 |
| **Codex 프로파일** | 3개 | **6개** (기본 + Pro 3쌍) |
| **출처 표기** | 없음 | 모든 가격/출시일에 [출처, 날짜] 추가 |

---

*끝 — v3 (2026-08-03, GPT-5.6 Sol/Terra/Luna + Pro 변형 + Programmatic Tool Calling + 1.05M Context 반영)*
*출처: [OpenAI API Pricing, 2026-08-02] [OpenRouter, 2026-08-03] [OpenAI Community, 2026-07-09] [GitHub Copilot Changelog, 2026-07-09]*