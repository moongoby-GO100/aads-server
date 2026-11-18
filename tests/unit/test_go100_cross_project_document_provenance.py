import os
os.environ.setdefault("JWT_SECRET_KEY", "go100-test-only-not-production-key-20261118")

import uuid

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs


@pytest.mark.asyncio
async def test_http_does_not_supply_bound_session():
    class Conn:
        async def fetchval(self, sql, *args):
            return 1 if 'FROM chat_sessions' in sql else None

        async def fetchrow(self, sql, *args):
            raise AssertionError('unbound request must not query cross-project authority')

    body = docs.RevisionInput(document_key='test-doc', kind='report', title='보고', version='1.0.0',
                              content='내용', source_task_id='runner-test', source_session_id=uuid.uuid4(),
                              expected_generation=0)
    with pytest.raises(HTTPException, match='task_not_found'):
        await docs.create_revision_in_tx(Conn(), str(uuid.uuid4()), 'user', 'GO100', body,
                                         session_any_project=True)
