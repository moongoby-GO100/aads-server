"""Disposable PostgreSQL for GO100 approval integration tests.

Never accepts a DSN: connections can only target the container created here.
"""
import asyncio
import json
import secrets
import subprocess
import time


class IsolatedPostgres:
    IMAGE = "postgres:16-alpine"
    LABEL = "aads.go100.approval-test"
    READY_TIMEOUT = 60

    def __init__(self):
        self.token = secrets.token_hex(16)
        self.name = "go100-approval-" + self.token
        self.volume = self.name + "-data"
        self.container_id = None
        self.port = None

    @staticmethod
    def _docker(*args):
        try:
            return subprocess.run(["docker", *args], check=True, capture_output=True,
                                  text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("isolated PostgreSQL Docker operation failed: " + args[0]) from exc

    def _owned(self, kind, name):
        try:
            value = self._docker(kind, "inspect", "--format",
                                 '{{ index .Labels "' + self.LABEL + '" }}' if kind == "volume"
                                 else '{{ index .Config.Labels "' + self.LABEL + '" }}', name)
            return value == self.token
        except RuntimeError:
            return False

    def start(self):
        self._docker("info", "--format", "{{.ServerVersion}}")
        try:
            self._docker("volume", "create", "--label", self.LABEL + "=" + self.token, self.volume)
            self.container_id = self._docker(
                "run", "-d", "--name", self.name, "--label", self.LABEL + "=" + self.token,
                "--cpus", "1", "--memory", "512m", "--pids-limit", "128",
                "-p", "127.0.0.1::5432", "-v", self.volume + ":/var/lib/postgresql/data",
                "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_USER=go100_test",
                "-e", "POSTGRES_DB=go100_test", self.IMAGE)
            if not self._owned("container", self.container_id) or not self._owned("volume", self.volume):
                raise RuntimeError("test resource ownership verification failed")
            mapping = json.loads(self._docker("inspect", self.container_id))[0]["NetworkSettings"]["Ports"]["5432/tcp"]
            if len(mapping) != 1 or mapping[0]["HostIp"] != "127.0.0.1":
                raise RuntimeError("test port is not loopback-only")
            self.port = int(mapping[0]["HostPort"])
            asyncio.run(self._wait_ready())
            return self
        except BaseException:
            self._startup_logs()
            self.close()
            raise

    async def _wait_ready(self):
        import asyncpg

        deadline = time.monotonic() + self.READY_TIMEOUT
        while time.monotonic() < deadline:
            conn = None
            try:
                remaining = deadline - time.monotonic()
                conn = await asyncio.wait_for(asyncpg.connect(
                    host="127.0.0.1", port=self.port, user="go100_test",
                    database="go100_test", timeout=min(2, remaining)), timeout=min(2, remaining))
                row = await asyncio.wait_for(conn.fetchrow(
                    "SELECT 1, current_user, current_database(), current_setting('data_directory')"),
                    timeout=min(2, max(0.001, deadline - time.monotonic())))
                if tuple(row) != (1, "go100_test", "go100_test", "/var/lib/postgresql/data"):
                    raise RuntimeError("test database identity mismatch")
                return
            except (OSError, asyncio.TimeoutError, asyncpg.PostgresError):
                pass
            finally:
                if conn is not None:
                    try:
                        await asyncio.wait_for(conn.close(), timeout=2)
                    except (OSError, asyncio.TimeoutError):
                        conn.terminate()
            await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))
        raise RuntimeError("isolated PostgreSQL TCP readiness timed out")

    def _startup_logs(self):
        if not self.container_id or not self._owned("container", self.container_id):
            return
        try:
            result = subprocess.run(["docker", "logs", "--tail", "50", self.container_id],
                                    capture_output=True, text=True, timeout=5, check=True)
            logs = result.stdout + result.stderr
            # Only emit fixed, non-sensitive startup markers, never arbitrary log text.
            for marker in ("database system is ready to accept connections",
                           "PostgreSQL init process complete", "database system is shut down"):
                print(f"PostgreSQL startup marker: {marker}: {logs.count(marker)}")
        except (OSError, subprocess.SubprocessError):
            print("PostgreSQL startup markers unavailable")

    def connection_args(self, dsn=None):
        if dsn is not None:
            raise ValueError("external DSNs are forbidden")
        if not self.port or not self._owned("container", self.container_id):
            raise RuntimeError("test container ownership lost")
        return dict(host="127.0.0.1", port=self.port, user="go100_test", database="go100_test")

    def close(self):
        errors = []
        # A timed-out docker run may have created the container without returning its ID.
        # Require both the exact generated name and the per-run label before removal.
        try:
            ids = self._docker("ps", "-aq", "--no-trunc", "--filter",
                               "name=^/" + self.name + "$", "--filter",
                               "label=" + self.LABEL + "=" + self.token).splitlines()
            for container_id in ids:
                if not self._owned("container", container_id):
                    raise RuntimeError("container ownership could not be verified")
                self._docker("rm", "-f", container_id)
            if self._docker("ps", "-aq", "--filter", "name=^/" + self.name + "$",
                            "--filter", "label=" + self.LABEL + "=" + self.token):
                raise RuntimeError("container still exists")
        except RuntimeError as exc:
            errors.append("container cleanup failed: " + str(exc))
        try:
            volumes = self._docker("volume", "ls", "-q", "--filter",
                                   "label=" + self.LABEL + "=" + self.token).splitlines()
            if self.volume in volumes:
                if not self._owned("volume", self.volume):
                    raise RuntimeError("volume ownership could not be verified")
                self._docker("volume", "rm", self.volume)
            remaining = self._docker("volume", "ls", "-q", "--filter",
                                     "label=" + self.LABEL + "=" + self.token).splitlines()
            if self.volume in remaining:
                raise RuntimeError("volume still exists")
        except RuntimeError as exc:
            errors.append("volume cleanup failed: " + str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))
        print("isolated PostgreSQL cleanup: complete (container and volume absent)")
