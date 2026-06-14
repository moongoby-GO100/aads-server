import asyncio
import asyncpg
import json
import base64

async def main():
    conn = await asyncpg.connect(
        host='aads-postgres',
        database='aads',
        user='aads',
        password='aads2026secure'
    )
    row = await conn.fetchrow(
        "SELECT attachments FROM chat_messages WHERE id = $1",
        'ed6e5642-2213-4863-9243-85a7b8e8bc9c'
    )
    attachments = json.loads(row['attachments'])
    b64 = attachments[0]['base64']
    img_data = base64.b64decode(b64)
    with open('/tmp/kakaotalk_photo.jpg', 'wb') as f:
        f.write(img_data)
    print(f'saved {len(img_data)} bytes')
    await conn.close()

asyncio.run(main())
