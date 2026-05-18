#!/usr/bin/env python3
"""Extract face seed images from media_generation_jobs and save as PNG files."""
import os
import base64
import asyncio
import asyncpg

OUTPUT_DIR = "/tmp/face_seeds"
os.makedirs(OUTPUT_DIR, exist_ok=True)

async def main():
    db_url = os.environ.get("DATABASE_URL", "postgresql://aads:aads@postgres:5432/aads")
    conn = await asyncpg.connect(db_url)
    rows = await conn.fetch(
        "SELECT id, job_id, result_uri, substring(prompt from 1 for 200) as prompt_preview "
        "FROM media_generation_jobs WHERE model_id LIKE '%imagen%' ORDER BY id ASC LIMIT 10"
    )
    for row in rows:
        uri = row["result_uri"]
        if uri and uri.startswith("data:image/png;base64,"):
            b64 = uri.split(",", 1)[1]
            img_data = base64.b64decode(b64)
            fname = f"face_seed_{row['id']:02d}.png"
            fpath = os.path.join(OUTPUT_DIR, fname)
            with open(fpath, "wb") as f:
                f.write(img_data)
            print(f"Saved {fpath} ({len(img_data)} bytes) - {row['prompt_preview'][:80]}")
        else:
            print(f"Skipped id={row['id']} - no base64 PNG data")
    await conn.close()
    print(f"\nDone. {len(rows)} images processed.")

asyncio.run(main())
