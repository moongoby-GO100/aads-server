"""OHVIS harness, Skill Find, LLM Wiki, and Hermes-pattern APIs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.ohvis_harness import (
    RISK_POLICIES,
    find_skills,
    get_harness_status,
    recommend_hermes_improvements,
    search_wiki,
)

router = APIRouter()


class SkillFindRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    project: Optional[str] = Field(None, max_length=40)
    intent: Optional[str] = Field(None, max_length=80)
    limit: int = Field(5, ge=1, le=20)


class WikiSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    project: Optional[str] = Field(None, max_length=40)
    limit: int = Field(10, ge=1, le=50)


class HermesRecommendRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    project: Optional[str] = Field(None, max_length=40)
    recent_failure: Optional[str] = Field(None, max_length=2000)


@router.get("/ohvis/harness/status", tags=["ohvis-harness"])
async def ohvis_harness_status(project: Optional[str] = Query(None, max_length=40)):
    return await get_harness_status(project=project)


@router.get("/ohvis/harness/policies", tags=["ohvis-harness"])
async def ohvis_harness_policies():
    return {"risk_policies": RISK_POLICIES}


@router.post("/ohvis/harness/skill-find", tags=["ohvis-harness"])
async def ohvis_skill_find(req: SkillFindRequest):
    return await find_skills(
        query=req.query,
        project=req.project,
        intent=req.intent,
        limit=req.limit,
    )


@router.post("/ohvis/harness/wiki/search", tags=["ohvis-harness"])
async def ohvis_wiki_search(req: WikiSearchRequest):
    return await search_wiki(query=req.query, project=req.project, limit=req.limit)


@router.post("/ohvis/harness/hermes/recommend", tags=["ohvis-harness"])
async def ohvis_hermes_recommend(req: HermesRecommendRequest):
    return await recommend_hermes_improvements(
        goal=req.goal,
        project=req.project,
        recent_failure=req.recent_failure,
    )

