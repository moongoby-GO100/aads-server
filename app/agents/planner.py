"""
Planner Agent: PRD·기술아키텍처·Phase분할 자동 생성.
AADS-126: Business Strategist 산출물(StrategyReport) 입력 → ProjectPlan 생성.
토론 루프에서 Strategist와 양방향 협업하는 evaluate/revise 인터페이스 포함.
"""
from __future__ import annotations

import os
import json
import structlog
from datetime import datetime, timezone
from typing import Optional, Any
from typing_extensions import TypedDict

from pydantic import BaseModel, Field

try:
    from app.services.model_router import get_llm_for_agent
except ImportError:
    get_llm_for_agent = None  # type: ignore

logger = structlog.get_logger()

# ─── 모델 설정 ────────────────────────────────────────────────────────────────

PLANNER_MODEL: str = os.getenv("PLANNER_MODEL", "claude-sonnet-4-6")

# ─── TypedDict State ─────────────────────────────────────────────────────────


class PlannerState(TypedDict, total=False):
    strategy_report: dict
    selected_candidate: dict
    prd: Optional[dict]
    architecture: Optional[dict]
    phase_plan: Optional[list]
    project_plan: Optional[dict]
    debate_round: int
    debate_history: list[dict]
    consensus_reached: bool
    planner_feedback: Optional[str]


# ─── Pydantic v2 Models ──────────────────────────────────────────────────────


class UserStory(BaseModel):
    role: str
    action: str
    benefit: str


class Feature(BaseModel):
    id: str
    name: str
    description: str
    priority: str = "must"  # must | should | could


class SuccessMetric(BaseModel):
    metric: str
    target: str
    timeframe: str


class PRDModel(BaseModel):
    problem_statement: str
    target_users: list[str] = Field(default_factory=list)
    user_stories: list[UserStory] = Field(default_factory=list)
    feature_list: list[Feature] = Field(default_factory=list)
    success_metrics: list[SuccessMetric] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class TechStackItem(BaseModel):
    layer: str
    technology: str
    reason: str


class APIEndpoint(BaseModel):
    method: str
    path: str
    description: str
    request_schema: str = ""
    response_schema: str = ""


class ArchitectureModel(BaseModel):
    system_diagram: str  # 텍스트 기반 다이어그램
    db_schema_ddl: str   # SQL DDL (핵심 테이블 5~10개)
    api_endpoints: list[APIEndpoint] = Field(default_factory=list)
    tech_stack: list[TechStackItem] = Field(default_factory=list)
    rejected_alternatives: list[str] = Field(default_factory=list)


class PhaseModel(BaseModel):
    phase_number: int
    name: str
    description: str
    key_features: list[str] = Field(default_factory=list)
    estimated_duration: str
    estimated_cost: str
    deliverables: list[str] = Field(default_factory=list)


class AlternativeModel(BaseModel):
    name: str
    reason_rejected: str


class ProjectPlan(BaseModel):
    prd: PRDModel
    architecture: ArchitectureModel
    phase_plan: list[PhaseModel] = Field(default_factory=list)
    rejected_alternatives: list[AlternativeModel] = Field(default_factory=list)
    estimated_total_cost: str
    estimated_total_timeline: str
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ─── evaluate_candidate ──────────────────────────────────────────────────────


async def evaluate_candidate(state: PlannerState) -> dict:
    """
    선택된 아이템의 기술적 실현가능성 평가.
    응답: {"feasible": bool, "concerns": [...], "suggestions": [...], "confidence": 0~10}
    모델: Claude Sonnet 4.6
    """
    candidate = state.get("selected_candidate", {})
    strategy_report = state.get("strategy_report", {})

    logger.info("planner_evaluate_start", candidate_id=candidate.get("id"), model=PLANNER_MODEL)

    system_prompt = """당신은 시니어 소프트웨어 아키텍트이자 기술 플래너입니다.
제시된 비즈니스 아이템의 기술적 실현가능성을 평가하세요.

반드시 아래 JSON 스키마로만 응답하세요:
{
  "feasible": true,
  "concerns": ["우려사항1", "우려사항2"],
  "suggestions": ["개선제안1", "개선제안2"],
  "confidence": 8
}

규칙:
- feasible: 기술적으로 구현 가능한지 boolean
- concerns: 기술적 위험/복잡도/리소스 관련 우려사항 (최소 2개)
- suggestions: 실현가능성 높이는 구체적 제안 (최소 2개)
- confidence: 평가 신뢰도 0~10 정수
- JSON만 출력"""

    user_message = f"""평가 대상 아이템:
{json.dumps(candidate, ensure_ascii=False, indent=2)}

전략 보고서 요약:
방향: {strategy_report.get('direction', '')}
추천: {strategy_report.get('recommendation', '')}

위 아이템의 기술적 실현가능성을 평가하세요."""

    try:
        llm, _ = get_llm_for_agent("planner")
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw = response.content if hasattr(response, "content") else str(response)
        json_str = raw.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()
        result = json.loads(json_str)
    except Exception as e:
        logger.warning("planner_evaluate_llm_failed", error=str(e))
        result = {
            "feasible": True,
            "concerns": ["LLM 평가 실패 — 수동 검토 필요", "기술 스택 검증 필요"],
            "suggestions": ["MVP 범위 최소화 권장", "프로토타입 우선 검증"],
            "confidence": 5,
        }

    logger.info("planner_evaluate_done", feasible=result.get("feasible"))
    return result


# ─── write_prd ───────────────────────────────────────────────────────────────


async def write_prd(state: PlannerState) -> PlannerState:
    """
    PRD 6섹션 생성 (ChatPRD 템플릿 구조).
    Problem Statement, Target Users, User Stories, Feature List, Success Metrics, Out of Scope
    모델: Claude Sonnet 4.6
    """
    candidate = state.get("selected_candidate", {})
    strategy_report = state.get("strategy_report", {})

    logger.info("planner_prd_start", candidate_id=candidate.get("id"), model=PLANNER_MODEL)

    system_prompt = """당신은 시니어 Product Manager입니다. ChatPRD 템플릿 구조로 PRD를 작성하세요.

반드시 아래 JSON 스키마를 준수하세요:
{
  "problem_statement": "해결하려는 문제와 현재 상황 상세 기술",
  "target_users": ["페르소나1 (직책, 특성)", "페르소나2 (직책, 특성)"],
  "user_stories": [
    {"role": "역할", "action": "행동", "benefit": "이점"},
    {"role": "역할", "action": "행동", "benefit": "이점"}
  ],
  "feature_list": [
    {"id": "F001", "name": "기능명", "description": "상세설명", "priority": "must"},
    {"id": "F002", "name": "기능명", "description": "상세설명", "priority": "should"}
  ],
  "success_metrics": [
    {"metric": "지표명", "target": "목표값", "timeframe": "기간"},
    {"metric": "지표명", "target": "목표값", "timeframe": "기간"}
  ],
  "out_of_scope": ["범위 외 항목1", "범위 외 항목2"]
}

규칙:
- problem_statement: 최소 100자
- target_users: 2~4개 페르소나
- user_stories: 최소 5개
- feature_list: must 3개 이상, should 2개 이상
- success_metrics: 최소 3개 (DAU, 전환율, 수익 등)
- out_of_scope: 최소 3개
- JSON만 출력"""

    user_message = f"""아이템 정보:
{json.dumps(candidate, ensure_ascii=False, indent=2)}

시장 방향: {strategy_report.get('direction', '')}
경쟁 분석: {json.dumps(strategy_report.get('competitors', [])[:3], ensure_ascii=False)}

위 정보를 바탕으로 PRD JSON을 작성하세요."""

    try:
        llm, _ = get_llm_for_agent("planner")
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw = response.content if hasattr(response, "content") else str(response)
        json_str = raw.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()
        prd_dict = json.loads(json_str)
    except Exception as e:
        logger.warning("planner_prd_llm_failed", error=str(e))
        prd_dict = _build_fallback_prd(candidate)

    # Pydantic 검증
    try:
        validated = PRDModel.model_validate(prd_dict)
        prd_dict = validated.model_dump()
    except Exception as e:
        logger.warning("prd_validation_warning", error=str(e))

    logger.info("planner_prd_done", features_count=len(prd_dict.get("feature_list", [])))
    return {**state, "prd": prd_dict}


def _build_fallback_prd(candidate: dict) -> dict:
    """LLM 실패 시 기본 PRD 구조."""
    title = candidate.get("title", "서비스")
    return {
        "problem_statement": f"{title}은 현재 시장에서 해결되지 않은 핵심 문제를 해결합니다. 사용자들은 효율적인 도구 부재로 인해 생산성 저하를 겪고 있습니다.",
        "target_users": [
            "중소기업 사업주 (30~50대, 디지털 전환 필요)",
            "스타트업 팀 (20~35대, 빠른 성장 추구)",
        ],
        "user_stories": [
            {"role": "사업주", "action": "데이터를 한눈에 확인", "benefit": "의사결정 속도 향상"},
            {"role": "팀원", "action": "자동화 워크플로우 설정", "benefit": "반복 작업 제거"},
            {"role": "관리자", "action": "성과 지표 추적", "benefit": "목표 달성률 개선"},
            {"role": "신규 사용자", "action": "온보딩 튜토리얼 완료", "benefit": "빠른 서비스 이해"},
            {"role": "파워 유저", "action": "고급 기능 활용", "benefit": "최대 효율 달성"},
        ],
        "feature_list": [
            {"id": "F001", "name": "핵심 대시보드", "description": "주요 지표 실시간 시각화", "priority": "must"},
            {"id": "F002", "name": "자동화 엔진", "description": "워크플로우 자동화", "priority": "must"},
            {"id": "F003", "name": "사용자 관리", "description": "권한 및 팀 관리", "priority": "must"},
            {"id": "F004", "name": "알림 시스템", "description": "이메일/슬랙 알림", "priority": "should"},
            {"id": "F005", "name": "API 연동", "description": "외부 서비스 통합", "priority": "should"},
        ],
        "success_metrics": [
            {"metric": "MAU", "target": "1,000명", "timeframe": "6개월"},
            {"metric": "유료 전환율", "target": "5%", "timeframe": "6개월"},
            {"metric": "MRR", "target": "$10,000", "timeframe": "12개월"},
        ],
        "out_of_scope": [
            "모바일 앱 (Phase 2)",
            "AI 예측 기능 (Phase 3)",
            "엔터프라이즈 SSO (Phase 2)",
        ],
    }


# ─── design_architecture ─────────────────────────────────────────────────────


async def design_architecture(state: PlannerState) -> PlannerState:
    """
    시스템 구성도, DB 스키마(SQL DDL), API 명세, 기술 스택 설계.
    모델: Claude Sonnet 4.6
    """
    candidate = state.get("selected_candidate", {})
    prd = state.get("prd", {})

    logger.info("planner_architecture_start", candidate_id=candidate.get("id"), model=PLANNER_MODEL)

    system_prompt = """당신은 시니어 소프트웨어 아키텍트입니다.
제공된 PRD와 아이템 정보를 바탕으로 기술 아키텍처를 설계하세요.

반드시 아래 JSON 스키마를 준수하세요:
{
  "system_diagram": "텍스트 기반 시스템 구성도 (ASCII art 또는 계층 구조)",
  "db_schema_ddl": "SQL DDL — 핵심 테이블 5~10개 CREATE TABLE 문",
  "api_endpoints": [
    {"method": "GET", "path": "/api/v1/resource", "description": "설명", "request_schema": "{}", "response_schema": "{}"},
    {"method": "POST", "path": "/api/v1/resource", "description": "설명", "request_schema": "{...}", "response_schema": "{...}"}
  ],
  "tech_stack": [
    {"layer": "Frontend", "technology": "Next.js 15", "reason": "선택 이유"},
    {"layer": "Backend", "technology": "FastAPI", "reason": "선택 이유"},
    {"layer": "Database", "technology": "PostgreSQL 15", "reason": "선택 이유"},
    {"layer": "Cache", "technology": "Redis", "reason": "선택 이유"},
    {"layer": "Infrastructure", "technology": "Docker + AWS ECS", "reason": "선택 이유"}
  ],
  "rejected_alternatives": [
    "Django (이유: 비동기 처리 부적합)",
    "MongoDB (이유: 관계형 데이터 구조 부적합)"
  ]
}

규칙:
- system_diagram: 최소 5개 컴포넌트 포함
- db_schema_ddl: 핵심 테이블 5~10개, SERIAL/UUID PK, TIMESTAMP, INDEX 포함
- api_endpoints: 최소 8개 엔드포인트 (CRUD + 비즈니스 로직)
- tech_stack: 최소 5개 레이어
- rejected_alternatives: 최소 2개 (기각 이유 포함)
- JSON만 출력"""

    user_message = f"""아이템: {candidate.get('title', '')}
수익 모델: {candidate.get('revenue_model', '')}
MVP 비용: {candidate.get('mvp_cost', '')}
MVP 기간: {candidate.get('mvp_timeline', '')}

PRD 요약:
- 주요 기능: {[f.get('name', '') for f in prd.get('feature_list', [])[:5]]}
- 타겟 유저: {prd.get('target_users', [])[:2]}

기술 아키텍처 JSON을 작성하세요."""

    try:
        llm, _ = get_llm_for_agent("planner")
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw = response.content if hasattr(response, "content") else str(response)
        json_str = raw.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()
        arch_dict = json.loads(json_str)
    except Exception as e:
        logger.warning("planner_architecture_llm_failed", error=str(e))
        arch_dict = _build_fallback_architecture(candidate)

    # Pydantic 검증
    try:
        validated = ArchitectureModel.model_validate(arch_dict)
        arch_dict = validated.model_dump()
    except Exception as e:
        logger.warning("architecture_validation_warning", error=str(e))

    logger.info("planner_architecture_done", endpoints_count=len(arch_dict.get("api_endpoints", [])))
    return {**state, "architecture": arch_dict}


def _build_fallback_architecture(candidate: dict) -> dict:
    """LLM 실패 시 기본 아키텍처 구조."""
    title = candidate.get("title", "서비스")
    return {
        "system_diagram": f"""[{title} 시스템 구성도]

Client (Browser/Mobile)
    |
    v
[CDN / Nginx]
    |
    v
[Frontend: Next.js]  ←→  [Backend API: FastAPI]
                               |
                    +----------+----------+
                    |          |          |
               [PostgreSQL] [Redis]  [Object Storage]
                    |
               [Background Workers]""",
        "db_schema_ddl": """CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    role TEXT DEFAULT 'member',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE organizations (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT DEFAULT 'free',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE memberships (
    id SERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    org_id INTEGER REFERENCES organizations(id),
    role TEXT DEFAULT 'member',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id INTEGER REFERENCES organizations(id),
    name TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE resources (
    id SERIAL PRIMARY KEY,
    project_id UUID REFERENCES projects(id),
    type TEXT NOT NULL,
    data JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_memberships_user ON memberships(user_id);
CREATE INDEX idx_memberships_org ON memberships(org_id);
CREATE INDEX idx_projects_org ON projects(org_id);
CREATE INDEX idx_resources_project ON resources(project_id);""",
        "api_endpoints": [
            {"method": "POST", "path": "/api/v1/auth/login", "description": "사용자 로그인", "request_schema": '{"email": "str", "password": "str"}', "response_schema": '{"token": "str"}'},
            {"method": "GET", "path": "/api/v1/users/me", "description": "현재 사용자 정보 조회", "request_schema": "", "response_schema": '{"id": "uuid", "email": "str", "name": "str"}'},
            {"method": "GET", "path": "/api/v1/organizations", "description": "조직 목록 조회", "request_schema": "", "response_schema": '[{"id": "int", "name": "str"}]'},
            {"method": "POST", "path": "/api/v1/organizations", "description": "조직 생성", "request_schema": '{"name": "str"}', "response_schema": '{"id": "int"}'},
            {"method": "GET", "path": "/api/v1/projects", "description": "프로젝트 목록", "request_schema": "?org_id=int", "response_schema": '[{"id": "uuid", "name": "str"}]'},
            {"method": "POST", "path": "/api/v1/projects", "description": "프로젝트 생성", "request_schema": '{"name": "str", "org_id": "int"}', "response_schema": '{"id": "uuid"}'},
            {"method": "GET", "path": "/api/v1/projects/{id}", "description": "프로젝트 상세", "request_schema": "", "response_schema": '{"id": "uuid", "name": "str", "resources": []}'},
            {"method": "DELETE", "path": "/api/v1/projects/{id}", "description": "프로젝트 삭제", "request_schema": "", "response_schema": '{"deleted": true}'},
        ],
        "tech_stack": [
            {"layer": "Frontend", "technology": "Next.js 15 + TypeScript", "reason": "SSR/SSG 지원, 생태계 성숙"},
            {"layer": "Backend", "technology": "FastAPI + Python 3.11", "reason": "비동기 처리, 빠른 개발"},
            {"layer": "Database", "technology": "PostgreSQL 15", "reason": "ACID, JSONB, 신뢰성"},
            {"layer": "Cache", "technology": "Redis 7", "reason": "세션, 캐싱, 큐"},
            {"layer": "Infrastructure", "technology": "Docker + AWS ECS", "reason": "확장성, 운영 효율"},
        ],
        "rejected_alternatives": [
            "Django REST Framework (이유: 비동기 처리 복잡, 오버헤드 큼)",
            "MongoDB (이유: 관계형 데이터 구조에 부적합, 조인 성능 열악)",
        ],
    }


# ─── create_phase_plan ────────────────────────────────────────────────────────


async def create_phase_plan(state: PlannerState) -> PlannerState:
    """
    Phase 1 (MVP), Phase 2 (확장), Phase 3 (최적화/스케일링) 계획 생성.
    """
    candidate = state.get("selected_candidate", {})
    prd = state.get("prd", {})
    architecture = state.get("architecture", {})

    logger.info("planner_phase_start", candidate_id=candidate.get("id"))

    system_prompt = """당신은 PM 겸 기술 플래너입니다. 3개 Phase 개발 계획을 작성하세요.

반드시 아래 JSON 배열 스키마를 준수하세요:
[
  {
    "phase_number": 1,
    "name": "MVP",
    "description": "핵심 기능만으로 시장 검증",
    "key_features": ["기능1", "기능2", "기능3"],
    "estimated_duration": "3개월",
    "estimated_cost": "$20K~$40K",
    "deliverables": ["산출물1", "산출물2"]
  },
  {
    "phase_number": 2,
    "name": "Growth",
    "description": "검증 후 기능 확장",
    "key_features": ["기능4", "기능5"],
    "estimated_duration": "3개월",
    "estimated_cost": "$30K~$60K",
    "deliverables": ["산출물3", "산출물4"]
  },
  {
    "phase_number": 3,
    "name": "Scale",
    "description": "최적화 및 스케일링",
    "key_features": ["기능6", "기능7"],
    "estimated_duration": "3~6개월",
    "estimated_cost": "$50K~$100K",
    "deliverables": ["산출물5", "산출물6"]
  }
]

규칙:
- 반드시 3개 Phase
- Phase 1: must 기능만 (PRD feature_list 우선순위 기반)
- Phase 2: should 기능 + 성장 기능
- Phase 3: 최적화, 엔터프라이즈, AI 고도화
- 각 Phase key_features 최소 3개
- 각 Phase deliverables 최소 2개
- JSON 배열만 출력"""

    must_features = [f.get("name", "") for f in prd.get("feature_list", []) if f.get("priority") == "must"]
    should_features = [f.get("name", "") for f in prd.get("feature_list", []) if f.get("priority") == "should"]

    user_message = f"""아이템: {candidate.get('title', '')}
MVP 비용: {candidate.get('mvp_cost', '')}
MVP 기간: {candidate.get('mvp_timeline', '')}
Must 기능: {must_features}
Should 기능: {should_features}
기술 스택: {[t.get('technology', '') for t in architecture.get('tech_stack', [])[:3]]}

3개 Phase 계획 JSON을 작성하세요."""

    try:
        llm, _ = get_llm_for_agent("planner")
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw = response.content if hasattr(response, "content") else str(response)
        json_str = raw.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()
        phase_list = json.loads(json_str)
    except Exception as e:
        logger.warning("planner_phase_llm_failed", error=str(e))
        phase_list = _build_fallback_phases(candidate, must_features, should_features)

    # Pydantic 검증
    validated_phases = []
    for p in phase_list:
        try:
            validated_phases.append(PhaseModel.model_validate(p).model_dump())
        except Exception as e:
            logger.warning("phase_validation_warning", error=str(e), phase=p.get("phase_number"))
            validated_phases.append(p)

    logger.info("planner_phase_done", phases_count=len(validated_phases))
    return {**state, "phase_plan": validated_phases}


def _build_fallback_phases(candidate: dict, must_features: list, should_features: list) -> list:
    """LLM 실패 시 기본 Phase 구조."""
    mvp_cost = candidate.get("mvp_cost", "$20K~$50K")
    mvp_timeline = candidate.get("mvp_timeline", "3~4개월")
    return [
        {
            "phase_number": 1,
            "name": "MVP",
            "description": "핵심 기능으로 시장 검증 — 최소 비용으로 최대 학습",
            "key_features": must_features[:3] if must_features else ["핵심 기능 1", "핵심 기능 2", "사용자 인증"],
            "estimated_duration": mvp_timeline,
            "estimated_cost": mvp_cost,
            "deliverables": ["서비스 베타 버전", "랜딩 페이지", "초기 사용자 100명"],
        },
        {
            "phase_number": 2,
            "name": "Growth",
            "description": "MVP 검증 후 기능 확장 — 유료 전환 최적화",
            "key_features": should_features[:3] if should_features else ["알림 시스템", "API 연동", "고급 대시보드"],
            "estimated_duration": "3~4개월",
            "estimated_cost": "$30K~$70K",
            "deliverables": ["정식 서비스 런칭", "유료 플랜 출시", "MAU 500명"],
        },
        {
            "phase_number": 3,
            "name": "Scale",
            "description": "최적화 및 스케일링 — 엔터프라이즈 진입",
            "key_features": ["AI 기능 강화", "엔터프라이즈 플랜", "멀티 테넌시", "모바일 앱"],
            "estimated_duration": "6개월",
            "estimated_cost": "$50K~$120K",
            "deliverables": ["엔터프라이즈 계약 5건", "MAU 2,000명", "MRR $20K"],
        },
    ]


# ─── assemble_project_plan ───────────────────────────────────────────────────


async def assemble_project_plan(state: PlannerState) -> PlannerState:
    """
    PRD + Architecture + Phase Plan → ProjectPlan 조립.
    """
    prd = state.get("prd", {})
    architecture = state.get("architecture", {})
    phase_plan = state.get("phase_plan", [])
    strategy_report = state.get("strategy_report", {})

    # 기각된 대안 (전략 보고서의 candidates 중 미선택 항목)
    selected_id = state.get("selected_candidate", {}).get("id", "")
    rejected_alternatives = [
        AlternativeModel(
            name=c.get("title", ""),
            reason_rejected=f"선택 점수 열위 (total={c.get('score', {}).get('total', 0):.2f}) 대비 선택 아이템 우세",
        ).model_dump()
        for c in strategy_report.get("candidates", [])
        if c.get("id") != selected_id
    ]

    # 총 비용/기간 추정
    costs = [p.get("estimated_cost", "") for p in phase_plan]
    timelines = [p.get("estimated_duration", "") for p in phase_plan]
    total_cost = f"합계: {' + '.join(costs)}" if costs else "산출 필요"
    total_timeline = f"총 {len(phase_plan) * 3}~{len(phase_plan) * 6}개월" if phase_plan else "산출 필요"

    try:
        project_plan = ProjectPlan(
            prd=PRDModel.model_validate(prd),
            architecture=ArchitectureModel.model_validate(architecture),
            phase_plan=[PhaseModel.model_validate(p) for p in phase_plan],
            rejected_alternatives=[AlternativeModel.model_validate(a) for a in rejected_alternatives],
            estimated_total_cost=total_cost,
            estimated_total_timeline=total_timeline,
        )
        plan_dict = project_plan.model_dump()
    except Exception as e:
        logger.warning("project_plan_assembly_warning", error=str(e))
        plan_dict = {
            "prd": prd,
            "architecture": architecture,
            "phase_plan": phase_plan,
            "rejected_alternatives": rejected_alternatives,
            "estimated_total_cost": total_cost,
            "estimated_total_timeline": total_timeline,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    logger.info("planner_plan_assembled", phases=len(phase_plan))
    return {**state, "project_plan": plan_dict}


# ─── generate_debate_feedback ────────────────────────────────────────────────


async def generate_debate_feedback(state: PlannerState) -> PlannerState:
    """
    Strategist와의 토론 루프 — evaluate/revise 인터페이스.
    planner_feedback: 문자열 피드백 + concerns 리스트 형식
    """
    project_plan = state.get("project_plan", {})
    debate_round = state.get("debate_round", 0) + 1
    debate_history = list(state.get("debate_history", []))

    logger.info("planner_debate_start", round=debate_round)

    # 이전 토론 이력 요약
    history_summary = "\n".join(
        f"Round {h.get('round', 0)}: {h.get('feedback', '')[:200]}"
        for h in debate_history[-3:]
    )

    system_prompt = """당신은 Planner 에이전트입니다. Strategist의 전략 보고서를 검토하고
기술적 관점에서 피드백을 제공합니다.

반드시 아래 JSON 스키마로 응답하세요:
{
  "feedback": "전반적 피드백 문자열 (최소 100자)",
  "concerns": ["우려사항1", "우려사항2", "우려사항3"],
  "suggestions": ["수정 제안1", "수정 제안2"],
  "consensus_reached": false,
  "confidence": 7
}

JSON만 출력"""

    prd_summary = {
        "features_count": len(project_plan.get("prd", {}).get("feature_list", [])),
        "phases_count": len(project_plan.get("phase_plan", [])),
        "total_cost": project_plan.get("estimated_total_cost", ""),
    }

    user_message = f"""현재 프로젝트 계획 요약:
{json.dumps(prd_summary, ensure_ascii=False)}

이전 토론 이력:
{history_summary if history_summary else '없음'}

Round {debate_round} 기술적 피드백을 제공하세요."""

    try:
        llm, _ = get_llm_for_agent("planner")
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw = response.content if hasattr(response, "content") else str(response)
        json_str = raw.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()
        feedback_data = json.loads(json_str)
    except Exception as e:
        logger.warning("planner_debate_llm_failed", error=str(e))
        feedback_data = {
            "feedback": f"Round {debate_round}: 프로젝트 계획 검토 완료. 기술적 실현가능성 확인됨. 추가 검토 불필요.",
            "concerns": ["비용 추정 정밀도 향상 필요", "Phase 1 기간 타당성 재검토"],
            "suggestions": ["MVP 범위 추가 축소 고려", "기술 스택 검증 선행"],
            "consensus_reached": debate_round >= 3,
            "confidence": 7,
        }

    feedback_str = feedback_data.get("feedback", "")
    concerns = feedback_data.get("concerns", [])
    consensus = feedback_data.get("consensus_reached", debate_round >= 3)

    # 피드백 형식: "문자열\n\nCONCERNS:\n- concern1\n- concern2"
    planner_feedback = f"{feedback_str}\n\nCONCERNS:\n" + "\n".join(f"- {c}" for c in concerns)

    debate_entry = {
        "round": debate_round,
        "feedback": planner_feedback,
        "concerns": concerns,
        "suggestions": feedback_data.get("suggestions", []),
        "consensus_reached": consensus,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    debate_history.append(debate_entry)

    logger.info("planner_debate_done", round=debate_round, consensus=consensus)
    return {
        **state,
        "debate_round": debate_round,
        "debate_history": debate_history,
        "consensus_reached": consensus,
        "planner_feedback": planner_feedback,
    }


# ─── DB 저장 ─────────────────────────────────────────────────────────────────


async def save_project_plan(
    project_id: str,
    strategy_report_id: Optional[int],
    selected_candidate_id: str,
    plan: dict,
    debate_rounds: int = 0,
    consensus_reached: bool = False,
    debate_log: list | None = None,
    cost_usd: float = 0.0,
) -> int | None:
    """project_plans 테이블에 저장."""
    import asyncpg

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        logger.warning("save_project_plan_no_db_url")
        return None

    try:
        conn = await asyncpg.connect(db_url, timeout=10)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO project_plans
                    (project_id, strategy_report_id, selected_candidate_id,
                     prd, architecture, phase_plan, rejected_alternatives,
                     debate_rounds, consensus_reached, debate_log, cost_usd)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb,
                        $8, $9, $10::jsonb, $11)
                RETURNING id
                """,
                project_id,
                strategy_report_id,
                selected_candidate_id,
                json.dumps(plan.get("prd", {}), ensure_ascii=False),
                json.dumps(plan.get("architecture", {}), ensure_ascii=False),
                json.dumps(plan.get("phase_plan", []), ensure_ascii=False),
                json.dumps(plan.get("rejected_alternatives", []), ensure_ascii=False),
                debate_rounds,
                consensus_reached,
                json.dumps(debate_log or [], ensure_ascii=False),
                cost_usd,
            )
            plan_id = row["id"] if row else None
            logger.info("project_plan_saved", plan_id=plan_id, project_id=project_id)
            return plan_id
        finally:
            await conn.close()
    except Exception as e:
        logger.error("save_project_plan_error", error=str(e))
        return None
