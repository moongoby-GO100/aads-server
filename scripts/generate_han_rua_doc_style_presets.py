#!/usr/bin/env python3
"""Seed Han Rua's documented style presets and generate test images.

This script follows the P0 model management plan:
five style presets per model, each verified through trial generation before use.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
from google import genai
from google.genai import types

try:
    from app.config import settings
except Exception:  # pragma: no cover - script fallback
    settings = None


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://aads:aads@aads-postgres:5432/aads")
PERSONA_ID = 3
PERSONA_NAME = "한루아"
MODEL_ID = "gemini-3.1-flash-image-preview"
PROVIDER = "gemini"
REFERENCE_SET = "han_rua_doc_style_preset"


@dataclass(frozen=True)
class StylePreset:
    slug: str
    name: str
    category: str
    season: str
    hair: dict[str, Any]
    makeup: dict[str, Any]
    accessories: dict[str, Any]
    expression: dict[str, Any]
    visual_note: str


PRESETS = [
    StylePreset(
        slug="spring_daily_natural",
        name="봄 데일리 내추럴",
        category="season",
        season="spring",
        hair={"style": "C컬", "color": "밝은브라운", "bangs": "시스루뱅", "part": "자연 6:4"},
        makeup={"base": "글로우", "blush": "피치블러셔", "lip": "코랄립", "eye": "소프트 브라운"},
        accessories={"earrings": "작은 스터드 귀걸이", "necklace": "없음", "glasses": "없음"},
        expression={"expression": "부드러운 미소", "gaze": "카메라 정면"},
        visual_note="fresh spring daily mood, soft warm studio light, clean neutral top",
    ),
    StylePreset(
        slug="summer_cool_tone",
        name="여름 쿨톤",
        category="season",
        season="summer",
        hair={"style": "웨이브 묶음", "color": "자연블랙", "bangs": "잔머리 정리", "part": "자연스러운 가르마"},
        makeup={"base": "세미매트", "eye": "핑크섀도", "lip": "누드립", "brow": "정돈된 자연 눈썹"},
        accessories={"sunglasses": "머리 위 또는 손에 든 선글라스", "earrings": "작은 실버 이어링"},
        expression={"expression": "활짝 웃음", "gaze": "카메라 정면 또는 살짝 측면"},
        visual_note="clear summer cool tone styling, bright clean catalog light, no product logo",
    ),
    StylePreset(
        slug="autumn_mood",
        name="가을 무드",
        category="season",
        season="autumn",
        hair={"style": "S컬", "color": "와인브라운", "bangs": "사이드뱅", "part": "깊은 사이드 파트"},
        makeup={"eye": "브라운섀도", "lip": "버건디립", "base": "벨벳 세미매트"},
        accessories={"earrings": "후프 귀걸이", "necklace": "없음"},
        expression={"expression": "시크한 표정", "gaze": "카메라 정면"},
        visual_note="autumn fashion mood, refined warm brown palette, calm editorial attitude",
    ),
    StylePreset(
        slug="winter_minimal",
        name="겨울 미니멀",
        category="season",
        season="winter",
        hair={"style": "생머리", "color": "다크브라운", "bangs": "이마노출", "part": "깔끔한 센터 또는 6:4"},
        makeup={"base": "풀매트", "eye": "누드섀도", "lip": "MLBB", "brow": "선명한 자연 눈썹"},
        accessories={"earrings": "없음", "necklace": "없음", "glasses": "없음"},
        expression={"expression": "무표정 쿨", "gaze": "카메라 정면"},
        visual_note="winter minimal styling, clean high-end catalog mood, restrained expression",
    ),
    StylePreset(
        slug="office_calm",
        name="오피스 차분한 미소",
        category="occasion",
        season="all",
        hair={"style": "반묶음", "color": "자연블랙", "bangs": "커튼뱅", "part": "정돈된 6:4"},
        makeup={"base": "세미매트", "eye": "브라운아이라인", "lip": "코랄립", "brow": "깔끔한 눈썹"},
        accessories={"watch": "미니멀 시계", "earrings": "작은 스터드", "necklace": "없음"},
        expression={"expression": "차분한 미소", "gaze": "카메라 정면"},
        visual_note="office-ready calm styling, polished but natural, product coordination friendly",
    ),
]


def secret_value(name: str) -> str:
    value = getattr(settings, name, "") if settings is not None else os.getenv(name, "")
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter() or "")
    return str(value or os.getenv(name, "") or "")


def parse_data_uri(uri: str) -> tuple[str, bytes]:
    match = re.match(r"^data:([^;]+);base64,(.+)$", uri or "", flags=re.S)
    if not match:
        raise ValueError("reference image is not a data URI")
    return match.group(1), base64.b64decode(match.group(2))


async def upsert_preset(conn: asyncpg.Connection, preset: StylePreset) -> int:
    row = await conn.fetchrow(
        """
        SELECT id
        FROM ai_style_presets
        WHERE persona_id = $1 AND preset_name = $2
        ORDER BY id DESC
        LIMIT 1
        """,
        PERSONA_ID,
        preset.name,
    )
    if row:
        preset_id = int(row["id"])
        await conn.execute(
            """
            UPDATE ai_style_presets
            SET is_shared = false,
                category = $3,
                season = $4,
                hair_params = $5::jsonb,
                makeup_params = $6::jsonb,
                accessory_params = $7::jsonb,
                expression_params = $8::jsonb,
                embedding_verified = false,
                embedding_score = NULL,
                updated_at = NOW()
            WHERE id = $1 AND persona_id = $2
            """,
            preset_id,
            PERSONA_ID,
            preset.category,
            preset.season,
            json.dumps(preset.hair, ensure_ascii=False),
            json.dumps(preset.makeup, ensure_ascii=False),
            json.dumps(preset.accessories, ensure_ascii=False),
            json.dumps(preset.expression, ensure_ascii=False),
        )
        return preset_id

    row = await conn.fetchrow(
        """
        INSERT INTO ai_style_presets (
            preset_name, persona_id, is_shared, category, season,
            hair_params, makeup_params, accessory_params, expression_params,
            embedding_verified, embedding_score, created_at, updated_at
        )
        VALUES ($1, $2, false, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, false, NULL, NOW(), NOW())
        RETURNING id
        """,
        preset.name,
        PERSONA_ID,
        preset.category,
        preset.season,
        json.dumps(preset.hair, ensure_ascii=False),
        json.dumps(preset.makeup, ensure_ascii=False),
        json.dumps(preset.accessories, ensure_ascii=False),
        json.dumps(preset.expression, ensure_ascii=False),
    )
    return int(row["id"])


async def reference_parts(conn: asyncpg.Connection) -> list[types.Part]:
    rows = await conn.fetch(
        """
        (
            SELECT j.result_uri
            FROM media_generation_jobs j
            WHERE j.id = 89 AND j.status = 'succeeded' AND j.result_uri IS NOT NULL
            LIMIT 1
        )
        UNION ALL
        (
            SELECT j.result_uri
            FROM ai_persona_references r
            JOIN media_generation_jobs j ON j.job_id = r.media_job_id
            WHERE r.persona_id = $1
              AND r.is_approved = true
              AND r.ref_type LIKE 'fullbody%'
              AND j.status = 'succeeded'
              AND j.result_uri IS NOT NULL
            ORDER BY r.id ASC
            LIMIT 2
        )
        """,
        PERSONA_ID,
    )
    parts: list[types.Part] = []
    for row in rows:
        mime, data = parse_data_uri(row["result_uri"])
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    if not parts:
        raise RuntimeError("no reference images found")
    return parts


def build_prompt(preset: StylePreset, trial_index: int) -> str:
    view = "front half-body portrait" if trial_index == 1 else "front three-quarter half-body portrait"
    return f"""Use case: photorealistic-natural adult fashion model documented style preset.
Asset type: NewTalk AI model style preset verification image {trial_index}/2.
Persona: {PERSONA_NAME} (Han Rua), 24-year-old adult Korean female AI fashion model, street casual / short-form traffic persona.
Reference images: first image is approved face seed #89, following images are CEO-approved full-body references. Preserve the same person: facial identity, skin tone, dark-brown layered hair base, body proportion impression, and adult energetic mood.
Documented preset name: {preset.name}.
Documented preset slug: {preset.slug}.
Hair settings: {json.dumps(preset.hair, ensure_ascii=False)}.
Makeup settings: {json.dumps(preset.makeup, ensure_ascii=False)}.
Accessory settings: {json.dumps(preset.accessories, ensure_ascii=False)}.
Expression settings: {json.dumps(preset.expression, ensure_ascii=False)}.
Scene/framing: {view}, head to waist visible, clean fashion catalog crop, neutral light gray studio background, soft realistic studio lighting.
Wardrobe baseline: simple fitted neutral top only, no product-specific garment dominance, styling should demonstrate hair, makeup, accessories, expression, and mood rather than a sellable outfit.
Visual note: {preset.visual_note}.
Quality rules: photorealistic Korean fashion catalog, natural skin texture, no over-smoothing, no face distortion, identity must remain the same as the references.
Safety/business purpose: adult fashion model style-preset test for future NewTalk product coordination. Non-erotic commercial catalog styling.
No logo, no text, no watermark, no extra people, no celebrity resemblance, no school-uniform cues, no minor-coded styling, no lingerie, no nudity, no transparent fabric, no sexual pose, no heavy retouching, do not change identity."""


async def insert_running_job(conn: asyncpg.Connection, prompt: str, preset: StylePreset, trial_index: int) -> str:
    job_id = f"media-{uuid.uuid4().hex[:16]}"
    await conn.execute(
        """
        INSERT INTO media_generation_jobs (
            job_id, kind, provider, model_id, prompt, input_refs, status,
            result_metadata, requested_by, created_at, updated_at
        )
        VALUES ($1, 'image', $2, $3, $4, $5::jsonb, 'running', '{}'::jsonb, 'ceo_han_rua_style_preset', NOW(), NOW())
        """,
        job_id,
        PROVIDER,
        MODEL_ID,
        prompt,
        json.dumps({"persona_id": PERSONA_ID, "style_preset_slug": preset.slug, "trial_index": trial_index}, ensure_ascii=False),
    )
    return job_id


async def mark_failed(conn: asyncpg.Connection, job_id: str, exc: Exception) -> None:
    await conn.execute(
        """
        UPDATE media_generation_jobs
        SET status = 'failed',
            error_message = $2,
            result_metadata = $3::jsonb,
            updated_at = NOW(),
            completed_at = NOW()
        WHERE job_id = $1
        """,
        job_id,
        str(exc)[:1000],
        json.dumps({"error_code": "STYLE_PRESET_GENERATION_FAILED"}, ensure_ascii=False),
    )


async def store_success(
    conn: asyncpg.Connection,
    *,
    job_id: str,
    preset_id: int,
    preset: StylePreset,
    trial_index: int,
    result_uri: str,
    recommendation_rank: int,
) -> None:
    image_url = f"/api/v1/image/gallery/{job_id}/image"
    await conn.execute(
        """
        UPDATE media_generation_jobs
        SET status = 'succeeded',
            result_uri = $2,
            result_metadata = $3::jsonb,
            updated_at = NOW(),
            completed_at = NOW()
        WHERE job_id = $1
        """,
        job_id,
        result_uri,
        json.dumps({"provider": PROVIDER, "model_id": MODEL_ID, "style_preset_id": preset_id}, ensure_ascii=False),
    )
    metadata = {
        "source_stage": "documented_style_preset_trial",
        "reference_set": REFERENCE_SET,
        "seed_face_job_id": 89,
        "style_preset_id": preset_id,
        "style_preset_name": preset.name,
        "style_preset_slug": preset.slug,
        "category": preset.category,
        "season": preset.season,
        "trial_index": trial_index,
        "approval_recommended": True,
        "approval_recommendation_rank": recommendation_rank,
        "approval_recommendation_reason": f"기획서 기본 프리셋 '{preset.name}' 시험 생성 {trial_index}/2",
        "hair_params": preset.hair,
        "makeup_params": preset.makeup,
        "accessory_params": preset.accessories,
        "expression_params": preset.expression,
    }
    await conn.execute(
        """
        INSERT INTO ai_persona_references (
            persona_id, ref_type, angle_degree, image_url, media_job_id,
            embedding_similarity, is_approved, metadata, created_at
        )
        VALUES ($1, 'style_preset', NULL, $2, $3, NULL, false, $4::jsonb, NOW())
        """,
        PERSONA_ID,
        image_url,
        job_id,
        json.dumps(metadata, ensure_ascii=False),
    )
    if trial_index == 1:
        await conn.execute(
            """
            UPDATE ai_style_presets
            SET thumbnail_url = $2,
                updated_at = NOW()
            WHERE id = $1
            """,
            preset_id,
            image_url,
        )


def extract_image_data(response: Any) -> str:
    for candidate in response.candidates or []:
        content = candidate.content
        for part in content.parts or []:
            inline_data = getattr(part, "inline_data", None)
            if inline_data and inline_data.mime_type and inline_data.mime_type.startswith("image/"):
                b64 = base64.b64encode(inline_data.data).decode()
                return f"data:{inline_data.mime_type};base64,{b64}"
    raise RuntimeError("Gemini response did not include image data")


async def generate_one(
    conn: asyncpg.Connection,
    client: genai.Client,
    refs: list[types.Part],
    preset: StylePreset,
    preset_id: int,
    trial_index: int,
    recommendation_rank: int,
) -> str:
    prompt = build_prompt(preset, trial_index)
    job_id = await insert_running_job(conn, prompt, preset, trial_index)
    contents = [*refs, prompt]
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_ID,
            contents=contents,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )
        result_uri = extract_image_data(response)
        await store_success(
            conn,
            job_id=job_id,
            preset_id=preset_id,
            preset=preset,
            trial_index=trial_index,
            result_uri=result_uri,
            recommendation_rank=recommendation_rank,
        )
        return job_id
    except Exception as exc:
        await mark_failed(conn, job_id, exc)
        raise


async def main() -> None:
    api_key = secret_value("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured")

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        refs = await reference_parts(conn)
        client = genai.Client(api_key=api_key)
        rank = 1
        for preset in PRESETS:
            preset_id = await upsert_preset(conn, preset)
            print(f"preset {preset_id}: {preset.name}", flush=True)
            for trial_index in (1, 2):
                job_id = await generate_one(conn, client, refs, preset, preset_id, trial_index, rank)
                print(f"  trial {trial_index}: {job_id}", flush=True)
                rank += 1
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
