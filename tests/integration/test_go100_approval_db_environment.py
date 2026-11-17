"""Real PostgreSQL transaction checks against a disposable test container."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.test_support.go100_approval_test_env import IsolatedPostgres


@pytest.fixture(scope="module", params=range(3))
def postgres():
    server = IsolatedPostgres().start()
    try:
        yield server
    finally:
        server.close()


def test_external_dsn_rejected(postgres):
    with pytest.raises(ValueError, match="external DSNs"):
        postgres.connection_args("postgresql://production/example")


def test_transactions_on_isolated_postgres(postgres):
    import asyncpg

    async def exercise():
        args = postgres.connection_args()
        first = await asyncpg.connect(**args, timeout=5, command_timeout=5)
        second = None
        try:
            second = await asyncpg.connect(**args, timeout=5, command_timeout=5)
            identity = await first.fetchrow("SELECT current_user, current_database(), inet_server_addr()::text AS address, current_setting('data_directory') AS data_directory, system_identifier::text FROM pg_control_system()")
            assert identity[0] == identity[1] == "go100_test"
            assert identity[3] == "/var/lib/postgresql/data"
            assert identity[4].isdigit()
            control = postgres._docker("exec", postgres.container_id, "pg_controldata", "/var/lib/postgresql/data")
            assert any(line.split(":", 1)[1].strip() == identity[4] for line in control.splitlines() if line.startswith("Database system identifier:"))
            print(f"server identity: user={identity[0]} database={identity[1]} data_directory={identity[3]} system_identifier={identity[4]} container={postgres.container_id[:12]}")
            async with first.transaction():
                await first.execute("CREATE TABLE approval_probe (id integer PRIMARY KEY, value integer NOT NULL)")
                await first.execute("INSERT INTO approval_probe VALUES (1, 0)")
            assert await second.fetchval("SELECT value FROM approval_probe WHERE id=1") == 0
            with pytest.raises(asyncpg.UniqueViolationError):
                async with first.transaction():
                    await first.execute("INSERT INTO approval_probe VALUES (2, 1)")
                    await first.execute("INSERT INTO approval_probe VALUES (1, 9)")
            assert await second.fetchval("SELECT count(*) FROM approval_probe WHERE id=2") == 0

            async def increment(conn):
                for _ in range(20):
                    async with conn.transaction():
                        await conn.execute("UPDATE approval_probe SET value=value+1 WHERE id=1")
            await asyncio.gather(increment(first), increment(second))
            assert await second.fetchval("SELECT value FROM approval_probe WHERE id=1") == 40
        finally:
            await asyncio.wait_for(first.close(), 5)
            if second is not None:
                await asyncio.wait_for(second.close(), 5)

    asyncio.run(asyncio.wait_for(exercise(), 60))


def test_tcp_readiness_retries_socket_only_phase(monkeypatch):
    import asyncpg

    server = IsolatedPostgres()
    server.port = 12345
    calls = []

    class Connection:
        async def fetchrow(self, query):
            return (1, "go100_test", "go100_test", "/var/lib/postgresql/data")

        async def close(self):
            calls.append("closed")

    async def connect(**kwargs):
        calls.append((kwargs["host"], kwargs["port"]))
        if len(calls) == 1:
            raise ConnectionResetError("TCP not ready")
        return Connection()

    monkeypatch.setattr(asyncpg, "connect", connect)
    asyncio.run(server._wait_ready())
    assert calls == [("127.0.0.1", 12345), ("127.0.0.1", 12345), "closed"]


def test_readiness_timeout_cleans_owned_resources(monkeypatch):
    server = IsolatedPostgres()
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "run":
            return "owned-id"
        if args[:2] == ("inspect", "owned-id"):
            return '[{"NetworkSettings":{"Ports":{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"12345"}]}}}]'
        if args[:2] == ("ps", "-aq") and "--no-trunc" in args:
            return "owned-id"
        if args[:2] == ("volume", "ls") and not any(x[:2] == ("volume", "rm") for x in calls):
            return server.volume
        return ""

    async def timeout():
        raise RuntimeError("isolated PostgreSQL TCP readiness timed out")

    monkeypatch.setattr(server, "_docker", docker)
    monkeypatch.setattr(server, "_owned", lambda *args: True)
    monkeypatch.setattr(server, "_startup_logs", lambda: None)
    monkeypatch.setattr(server, "_wait_ready", timeout)
    with pytest.raises(RuntimeError, match="TCP readiness timed out"):
        server.start()
    assert ("rm", "-f", "owned-id") in calls
    assert ("volume", "rm", server.volume) in calls
