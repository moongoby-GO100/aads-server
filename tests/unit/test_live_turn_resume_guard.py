"""Exercise the production scan guard and generation lifecycle without app boot."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock


def service_namespace():
    tree = ast.parse(Path('app/services/chat_service.py').read_text())
    names = {'_execution_generation_alive', '_generation_heartbeat', '_track_generation_stream'}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    ns = {'_heartbeat_asyncio': asyncio, '_generation_tasks': {}, '_generation_contexts': {},
          '_streaming_state': {}, '_bg_time': SimpleNamespace(monotonic=lambda: 60),
          'logger': SimpleNamespace(warning=lambda *a: None)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<production generation helpers>', 'exec'), ns)
    return ns


def scan_guard(ns, row):
    source = Path('app/main.py').read_text()
    start = source.index('                    if _generation_alive_exec(execution_id):')
    end = source.index('                    _stale_sec =', start)
    # Run the actual candidate guards; reaching the end represents eligibility
    # for the existing fenced claim, which remains outside this change.
    import textwrap
    body = textwrap.dedent(source[start:end])
    body = 'for row in rows:\n    sid = row["session_id"]\n    execution_id = row["execution_id"]\n' + textwrap.indent(body, '    ') + '\n    eligible.append(execution_id)\n'
    env = dict(ns, rows=[row], eligible=[], _generation_alive_exec=ns['_execution_generation_alive'],
               _abt_exec={}, _ss_exec=ns['_streaming_state'], min_stale_seconds=8,
               _resume_time=SimpleNamespace(monotonic=lambda: 60))
    env['logger'] = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
    exec(body, env)
    return env['eligible']


class LiveTurnGuardTests(IsolatedAsyncioTestCase):
    async def test_sixty_second_tool_wait_is_not_reclaimed(self):
        ns = service_namespace()
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait())
        ns['_generation_tasks']['turn'] = {'task': task}
        ns['_streaming_state']['session'] = {'execution_id': 'turn', 'updated_at': 0, 'completed': False}
        row = {'execution_id': 'turn', 'session_id': 'session', 'owner_epoch': 1, 'retry_count': 0}
        try:
            self.assertEqual(scan_guard(ns, row), [])
            self.assertEqual((row['owner_epoch'], row['retry_count']), (1, 0))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_missing_or_finished_task_remains_reclaimable(self):
        ns = service_namespace()
        row = {'execution_id': 'turn', 'session_id': 'session'}
        ns['_streaming_state']['session'] = {'updated_at': 0, 'completed': False}
        self.assertEqual(scan_guard(ns, row), ['turn'])
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        ns['_generation_tasks']['turn'] = {'task': task}
        self.assertEqual(scan_guard(ns, row), ['turn'])

    async def test_heartbeat_renews_fenced_lease_during_tool_wait(self):
        ns = service_namespace()
        ticks = []
        async def sleep(interval):
            ticks.append(interval)
            if len(ticks) > 6:
                raise asyncio.CancelledError
        ns['_heartbeat_asyncio'] = SimpleNamespace(sleep=sleep)
        task = asyncio.current_task()
        record = {'execution_id': 'turn', 'owner_epoch': 1, 'task': task}
        ns['_generation_tasks']['turn'] = record
        state = {'execution_id': 'turn', 'updated_at': 0}
        ns['_streaming_state']['session'] = state
        class Acquire:
            async def __aenter__(self): return 'connection'
            async def __aexit__(self, *args): pass
        ns['get_pool'] = lambda: SimpleNamespace(acquire=Acquire)
        renew = AsyncMock(return_value=True)
        ns['_heartbeat_execution_lease'] = renew
        with self.assertRaises(asyncio.CancelledError):
            await ns['_generation_heartbeat']('session', record)
        self.assertEqual(renew.await_count, 6)
        renew.assert_awaited_with('connection', 'turn', 1)
        self.assertEqual(state['updated_at'], 60)

    async def test_direct_stream_registry_cleanup(self):
        ns = service_namespace()
        @ns['_track_generation_stream']
        async def stream(session_id):
            record = ns['_generation_contexts'][asyncio.current_task()]
            record.update(execution_id='turn', owner_epoch=1)
            ns['_generation_tasks']['turn'] = record
            yield 'chunk'
        gen = stream('session')
        self.assertEqual(await anext(gen), 'chunk')
        self.assertTrue(ns['_execution_generation_alive']('turn'))
        await gen.aclose()
        self.assertEqual(ns['_generation_tasks'], {})
        self.assertEqual(ns['_generation_contexts'], {})
