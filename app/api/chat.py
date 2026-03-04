"""
AADS Chat Endpoint - 자연어 → 액션 라우터
Genspark AI 채팅 / 브릿지에서 자연어로 AADS를 운용하기 위한 엔드포인트.
Phase 1: 키워드 기반 룰 라우터 (LLM 호출 없음, 비용 $0)
Phase 2: PM Agent LLM 라우터로 업그레이드
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime
import re
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

class ChatRequest(BaseModel):
    message: str
    sender: str = "ceo"
    context: Optional[Dict[str, Any]] = None

class ChatResponse(BaseModel):
    intent: str
    action: str
    message: str
    data: Optional[Dict[str, Any]] = None
    api_call: Optional[Dict[str, Any]] = None
    timestamp: str

# === 의도 분류 규칙 ===
INTENT_RULES = [
    {
        "intent": "create_project",
        "keywords": ["만들어", "개발해", "생성해", "구축해", "build", "create", "make", "develop"],
        "action": "POST /api/v1/projects",
        "description": "새 프로젝트 생성"
    },
    {
        "intent": "check_status",
        "keywords": ["상태", "현황", "진행", "어떻게", "status", "health", "확인", "보고"],
        "action": "GET /api/v1/health + GET /api/v1/context/system/status",
        "description": "시스템 상태 확인"
    },
    {
        "intent": "check_project",
        "keywords": ["프로젝트", "project", "결과", "완료", "진척"],
        "action": "GET /api/v1/projects/{id}",
        "description": "프로젝트 상태 확인"
    },
    {
        "intent": "check_cost",
        "keywords": ["비용", "cost", "얼마", "과금", "요금", "pricing"],
        "action": "GET /api/v1/context/system/costs",
        "description": "비용 조회"
    },
    {
        "intent": "check_memory",
        "keywords": ["메모리", "memory", "경험", "experience", "기억", "저장"],
        "action": "GET /api/v1/context/experiences",
        "description": "메모리/경험 조회"
    },
    {
        "intent": "check_agents",
        "keywords": ["에이전트", "agent", "파이프라인", "pipeline"],
        "action": "GET /api/v1/context/system/agents",
        "description": "에이전트 설정 조회"
    },
    {
        "intent": "handover",
        "keywords": ["handover", "핸드오버", "전체현황", "요약", "summary"],
        "action": "GET /api/v1/context/handover",
        "description": "HANDOVER 전체 요약"
    },
    {
        "intent": "list_projects",
        "keywords": ["프로젝트 목록", "리스트", "list", "전체 프로젝트"],
        "action": "GET /api/v1/projects",
        "description": "프로젝트 목록 조회"
    },
]

def classify_intent(message: str) -> Dict:
    msg_lower = message.lower()
    scores = []
    for rule in INTENT_RULES:
        score = sum(1 for kw in rule["keywords"] if kw in msg_lower)
        if score > 0:
            scores.append((score, rule))
    if scores:
        scores.sort(key=lambda x: x[0], reverse=True)
        return scores[0][1]
    return {"intent": "unknown", "action": "none", "description": "의도를 파악할 수 없습니다"}

def extract_project_spec(message: str) -> Dict:
    """자연어에서 프로젝트 스펙 추출 (기본)"""
    spec = {
        "description": message,
        "extracted_keywords": [],
        "suggested_tech": []
    }
    tech_map = {
        "react": "React", "next": "Next.js", "vue": "Vue.js",
        "python": "Python", "fastapi": "FastAPI", "django": "Django",
        "node": "Node.js", "express": "Express",
        "docker": "Docker", "postgres": "PostgreSQL",
        "typescript": "TypeScript", "tailwind": "Tailwind CSS"
    }
    msg_lower = message.lower()
    for key, val in tech_map.items():
        if key in msg_lower:
            spec["suggested_tech"].append(val)
    return spec

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    자연어 메시지를 받아 의도를 파악하고 적절한 액션을 반환.
    브릿지가 api_call 필드를 보고 실제 API를 호출하면 됨.
    """
    logger.info(f"Chat received from {req.sender}: {req.message[:100]}")

    intent_result = classify_intent(req.message)
    intent = intent_result["intent"]
    action = intent_result["action"]

    response_data = {
        "intent": intent,
        "action": action,
        "timestamp": datetime.now().isoformat(),
        "data": None,
        "api_call": None,
        "message": ""
    }

    if intent == "create_project":
        spec = extract_project_spec(req.message)
        response_data["message"] = f"프로젝트 생성 요청을 감지했습니다. 스펙: {spec['description'][:200]}"
        response_data["api_call"] = {
            "method": "POST",
            "url": "/api/v1/projects",
            "body": {
                "description": req.message,
                "tech_stack": spec["suggested_tech"],
                "auto_run": False
            }
        }
        response_data["data"] = spec

    elif intent == "check_status":
        response_data["message"] = "시스템 상태를 확인합니다."
        response_data["api_call"] = {
            "method": "GET",
            "url": "/api/v1/health"
        }

    elif intent == "check_cost":
        response_data["message"] = "비용 현황을 조회합니다."
        response_data["api_call"] = {
            "method": "GET",
            "url": "/api/v1/context/system/costs"
        }

    elif intent == "check_memory":
        response_data["message"] = "경험 메모리를 조회합니다."
        response_data["api_call"] = {
            "method": "GET",
            "url": "/api/v1/context/experiences"
        }

    elif intent == "handover":
        response_data["message"] = "전체 시스템 현황(HANDOVER)을 생성합니다."
        response_data["api_call"] = {
            "method": "GET",
            "url": "/api/v1/context/handover"
        }

    elif intent == "check_agents":
        response_data["message"] = "에이전트 설정을 조회합니다."
        response_data["api_call"] = {
            "method": "GET",
            "url": "/api/v1/context/system/agents"
        }

    elif intent == "list_projects":
        response_data["message"] = "프로젝트 목록을 조회합니다."
        response_data["api_call"] = {
            "method": "GET",
            "url": "/api/v1/projects"
        }

    elif intent == "check_project":
        response_data["message"] = "프로젝트 상태를 확인합니다. 프로젝트 ID를 지정해주세요."
        response_data["api_call"] = {
            "method": "GET",
            "url": "/api/v1/projects"
        }

    else:
        response_data["message"] = "요청을 이해했습니다. 구체적인 명령을 주시면 처리하겠습니다. 가능한 명령: 프로젝트 생성, 상태 확인, 비용 조회, 메모리 조회, 핸드오버, 에이전트 설정, 프로젝트 목록"
        response_data["api_call"] = None

    return ChatResponse(**response_data)

@router.get("/chat/intents")
async def list_intents():
    """사용 가능한 의도(intent) 목록 반환 - 브릿지 설정용"""
    return {
        "status": "ok",
        "intents": [
            {"intent": r["intent"], "keywords": r["keywords"], "description": r["description"]}
            for r in INTENT_RULES
        ]
    }
