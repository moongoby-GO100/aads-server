"""Execute the production update function against PostgreSQL without importing LLMs.

AADS_CAS_TEST_DSN points at an expendable test database. Only a connection-local
TEMP table is written; the production chat_sessions table is never touched.
"""
import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
import uuid

import asyncpg


@unittest.skipUnless(os.getenv('AADS_CAS_TEST_DSN'), 'set AADS_CAS_TEST_DSN')
class SessionModelCASTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await asyncpg.connect(os.environ['AADS_CAS_TEST_DSN'])
        await self.conn.execute('CREATE TEMP TABLE chat_sessions (id uuid PRIMARY KEY, tenant_id uuid, current_model text, updated_at timestamptz)')
        self.sid, self.tenant = uuid.uuid4(), uuid.uuid4()
        await self.conn.execute('INSERT INTO chat_sessions VALUES ($1,$2,$3,clock_timestamp())', self.sid, self.tenant, 'original')
        conn = self.conn
        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn
        tree = ast.parse((Path(__file__).parents[2] / 'app/services/chat_service.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'update_session')
        scope = {'uuid': uuid, 'get_pool': Pool, '_require_tenant_uuid': lambda t, _: uuid.UUID(t), '_row_to_dict': dict}
        exec('from __future__ import annotations\n' + ast.unparse(node), scope)
        self.update = scope['update_session']

    async def asyncTearDown(self):
        await self.conn.close()

    async def read(self):
        return dict(await self.conn.fetchrow('SELECT * FROM chat_sessions WHERE id=$1', self.sid))

    async def write(self, model, version, tenant=None):
        return await self.update(str(self.sid), {'current_model': model, 'expected_updated_at': version}, str(tenant or self.tenant))

    async def test_delayed_timed_out_A_cannot_overwrite_successful_B(self):
        version = (await self.read())['updated_at']
        release_a = asyncio.Event()
        async def delayed_a():
            await release_a.wait()
            return await self.write('openai:A', version)
        a = asyncio.create_task(delayed_a())
        b = await self.write('codex:B', version)
        self.assertNotEqual(b['updated_at'], version)
        release_a.set()
        with self.assertRaisesRegex(ValueError, 'session_version_conflict'):
            await a
        self.assertEqual((await self.read())['current_model'], 'codex:B')

    async def test_conflict_refresh_retry_and_tenant_isolation(self):
        version = (await self.read())['updated_at']
        self.assertIsNone(await self.write('forbidden', version, uuid.uuid4()))
        a = await self.write('openai:A', version)
        with self.assertRaisesRegex(ValueError, 'session_version_conflict'):
            await self.write('codex:B', version)
        b = await self.write('codex:B', a['updated_at'])
        self.assertEqual(b['current_model'], 'codex:B')
        self.assertEqual((await self.read())['current_model'], 'codex:B')

if __name__ == '__main__':
    unittest.main()
