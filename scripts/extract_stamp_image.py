#!/usr/bin/env python3
"""Extract stamp image from DB and save to file."""
import asyncio
import json
import base64
import os

async def main():
    import asyncpg
    db_url = os.environ.get('DATABASE_URL', 'postgresql://aads:aads2026secure@postgres:5432/aads')
    conn = await asyncpg.connect(dsn=db_url, ssl=False)
    row = await conn.fetchrow(
        "SELECT attachments FROM chat_messages WHERE id = $1",
        'fded0ee8-389d-457b-b6a9-0b05de0bf0b1'
    )
    if row:
        atts = json.loads(row['attachments'])
        b64 = atts[0]['base64']
        img_data = base64.b64decode(b64)
        os.makedirs('/app/exports/stamps', exist_ok=True)
        out_path = '/app/exports/stamps/original_stamps.png'
        with open(out_path, 'wb') as f:
            f.write(img_data)
        print(f'Saved: {len(img_data)} bytes -> {out_path}')
    else:
        print('NOT FOUND')
    await conn.close()

asyncio.run(main())
