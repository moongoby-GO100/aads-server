#!/usr/bin/env python3
"""media_generation_jobs에서 이미지를 파일로 추출하고 manifest.json 생성."""
import asyncio
import base64
import json
import os
import sys

GALLERY_DIR = "/app/app/static/gallery"


def image_extension(uri: str) -> str:
    if uri.startswith("data:image/jpeg") or uri.startswith("data:image/jpg"):
        return "jpg"
    if uri.startswith("data:image/webp"):
        return "webp"
    return "png"

async def main():
    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed")
        sys.exit(1)

    dsn = os.environ.get("DATABASE_URL", "postgresql://aads:aads@localhost:5432/aads")
    conn = await asyncpg.connect(dsn)

    rows = await conn.fetch("""
        SELECT j.id, j.job_id, j.provider, j.model_id, j.status, j.prompt,
               j.result_uri, j.completed_at, j.created_at,
               r.id AS reference_id,
               r.ref_type AS reference_type,
               r.angle_degree AS reference_angle_degree,
               r.is_approved AS reference_is_approved,
               COALESCE((r.metadata->>'approval_recommended')::boolean, false) AS approval_recommended,
               (r.metadata->>'approval_recommendation_rank')::integer AS approval_recommendation_rank,
               r.metadata->>'approval_recommendation_reason' AS approval_recommendation_reason,
               r.metadata->>'reference_set' AS reference_set,
               r.metadata->>'outfit' AS reference_outfit,
               r.metadata->>'view' AS reference_view,
               r.metadata->>'rear_ref_type' AS reference_rear_type,
               r.metadata->>'style_preset_name' AS style_preset_name,
               r.metadata->>'style_preset_slug' AS style_preset_slug,
               r.metadata->>'trial_index' AS style_preset_trial_index
        FROM media_generation_jobs j
        LEFT JOIN LATERAL (
            SELECT id, ref_type, angle_degree, is_approved, metadata
            FROM ai_persona_references
            WHERE media_job_id = j.job_id
            ORDER BY id DESC
            LIMIT 1
        ) r ON true
        WHERE j.kind = 'image'
        ORDER BY j.id DESC
        LIMIT 200
    """)

    os.makedirs(GALLERY_DIR, exist_ok=True)
    items = []

    for r in rows:
        job_id = r["job_id"]
        ext = image_extension(r["result_uri"] or "")
        img_file = f"{job_id}.{ext}"
        img_path = os.path.join(GALLERY_DIR, img_file)

        if r["status"] == "succeeded" and r["result_uri"] and not os.path.exists(img_path):
            uri = r["result_uri"]
            if uri.startswith("data:"):
                _, b64data = uri.split(",", 1)
                with open(img_path, "wb") as f:
                    f.write(base64.b64decode(b64data))

        items.append({
            "id": r["id"],
            "job_id": job_id,
            "provider": r["provider"],
            "model_id": r["model_id"],
            "status": r["status"],
            "prompt": r["prompt"] or "",
            "has_image": os.path.exists(img_path),
            "image_file": img_file if os.path.exists(img_path) else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "reference_id": r["reference_id"],
            "reference_type": r["reference_type"],
            "reference_angle_degree": r["reference_angle_degree"],
            "reference_is_approved": r["reference_is_approved"],
            "approval_recommended": bool(r["approval_recommended"]),
            "approval_recommendation_rank": r["approval_recommendation_rank"],
            "approval_recommendation_reason": r["approval_recommendation_reason"],
            "reference_set": r["reference_set"],
            "reference_outfit": r["reference_outfit"],
            "reference_view": r["reference_view"],
            "reference_rear_type": r["reference_rear_type"],
            "style_preset_name": r["style_preset_name"],
            "style_preset_slug": r["style_preset_slug"],
            "style_preset_trial_index": r["style_preset_trial_index"],
        })

    manifest = {"total": len(items), "items": items}
    manifest_path = os.path.join(GALLERY_DIR, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, ensure_ascii=False)

    print(f"Exported {sum(1 for i in items if i['has_image'])} images, {len(items)} total")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
