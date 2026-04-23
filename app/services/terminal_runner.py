"""
PTY 기반 터미널 세션 러너.

P0 범위:
- 사용자별 in-memory PTY 세션 생성/조회/종료
- stdin 입력 및 명령 실행 편의 API
- 최근 출력 버퍼 + SSE 재연결용 seq replay
- 기본 민감정보 마스킹
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import fcntl
import logging
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import termios
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional
from uuid import uuid4

from app.models.terminal import TerminalSessionOut

logger = logging.getLogger(__name__)

DEFAULT_SHELL = "/bin/bash"
DEFAULT_CWD = "/root"
DEFAULT_COLS = 120
DEFAULT_ROWS = 32
MAX_BUFFERED_EVENTS = 1000
MAX_COMMAND_HISTORY = 100
QUEUE_MAXSIZE = 500
CLOSED_SESSION_TTL = timedelta(hours=6)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(authorization)\s*:\s*bearer\s+[^\s]+"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._-]+"),
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)\b(\s*[:=]\s*)([^\s\"']+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _redact_sensitive_text(text: str) -> str:
    redacted = text
    redacted = _SECRET_PATTERNS[0].sub(r"\1: Bearer [REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[1].sub(r"\1 [REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[2].sub(r"\1\2[REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[3].sub("[REDACTED]", redacted)
    if "BEGIN OPENSSH PRIVATE KEY" in redacted or "BEGIN RSA PRIVATE KEY" in redacted:
        return "[REDACTED PRIVATE KEY MATERIAL]\n"
    return redacted


def _normalize_cwd(cwd: str | None) -> str:
    raw = (cwd or DEFAULT_CWD).strip()
    if not raw:
        raw = DEFAULT_CWD
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(DEFAULT_CWD, expanded)
    normalized = os.path.abspath(expanded)
    if not os.path.isdir(normalized):
        raise ValueError(f"cwd not found: {normalized}")
    return normalized


def _normalize_shell(shell: str | None) -> List[str]:
    raw = (shell or DEFAULT_SHELL).strip()
    if not raw:
        raw = DEFAULT_SHELL
    argv = shlex.split(raw)
    if not argv:
        raise ValueError("shell command is empty")
    binary = argv[0]
    if not os.path.isabs(binary):
        resolved = shutil.which(binary)
        if not resolved:
            raise ValueError(f"shell not found: {binary}")
        argv[0] = resolved
    elif not os.path.exists(binary):
        raise ValueError(f"shell not found: {binary}")
    shell_name = os.path.basename(argv[0])
    if shell_name in {"bash", "sh", "zsh", "fish"} and "-i" not in argv and "--interactive" not in argv:
        argv.append("-i")
    return argv


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


@dataclass
class TerminalSession:
    session_id: str
    user_id: str
    user_email: str
    title: str
    shell: str
    cwd: str
    backend_mode: str
    cols: int
    rows: int
    created_at: datetime
    updated_at: datetime
    proc: asyncio.subprocess.Process
    master_fd: Optional[int]
    env: Dict[str, str] = field(default_factory=dict)
    status: str = "running"
    returncode: Optional[int] = None
    closed_at: Optional[datetime] = None
    last_seq: int = -1
    output_bytes: int = 0
    recent_commands: Deque[str] = field(default_factory=lambda: deque(maxlen=MAX_COMMAND_HISTORY))
    buffer: Deque[dict] = field(default_factory=lambda: deque(maxlen=MAX_BUFFERED_EVENTS))
    subscribers: List[asyncio.Queue] = field(default_factory=list)
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace")
    )
    stderr_decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace")
    )
    wait_task: Optional[asyncio.Task] = None
    reader_tasks: List[asyncio.Task] = field(default_factory=list)

    def snapshot(self) -> TerminalSessionOut:
        return TerminalSessionOut(
            session_id=self.session_id,
            title=self.title,
            shell=self.shell,
            cwd=self.cwd,
            status="running" if self.proc.returncode is None and self.status == "running" else "exited",
            backend_mode=self.backend_mode,
            pid=self.proc.pid,
            returncode=self.returncode if self.returncode is not None else self.proc.returncode,
            cols=self.cols,
            rows=self.rows,
            created_at=self.created_at,
            updated_at=self.updated_at,
            closed_at=self.closed_at,
            last_seq=max(self.last_seq, 0),
            output_bytes=self.output_bytes,
            recent_commands=list(self.recent_commands),
        )


class TerminalRunner:
    """사용자별 PTY 세션 매니저."""

    def __init__(self) -> None:
        self._sessions: Dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        *,
        user_id: str,
        user_email: str,
        cwd: str | None = None,
        shell: str | None = None,
        env: Optional[Dict[str, str]] = None,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        title: str | None = None,
    ) -> TerminalSessionOut:
        async with self._lock:
            self._prune_closed_sessions()

            normalized_cwd = _normalize_cwd(cwd)
            argv = _normalize_shell(shell)
            session_id = str(uuid4())

            proc_env = os.environ.copy()
            proc_env.update(
                {
                    "TERM": "xterm-256color",
                    "COLORTERM": "truecolor",
                    "PYTHONUNBUFFERED": "1",
                    "COLUMNS": str(cols),
                    "LINES": str(rows),
                    "AADS_TERMINAL_SESSION_ID": session_id,
                }
            )
            if env:
                proc_env.update({str(k): str(v) for k, v in env.items()})

            backend_mode = "pty"
            master_fd: Optional[int] = None
            launched_argv = list(argv)
            try:
                master_fd, slave_fd = pty.openpty()
                _set_winsize(slave_fd, rows, cols)

                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        cwd=normalized_cwd,
                        env=proc_env,
                        preexec_fn=os.setsid,
                    )
                finally:
                    os.close(slave_fd)
            except OSError as exc:
                backend_mode = "pipe"
                master_fd = None
                launched_argv = self._fallback_argv(argv)
                logger.warning("terminal_pty_unavailable session=%s err=%s", session_id, exc)
                proc = await asyncio.create_subprocess_exec(
                    *launched_argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=normalized_cwd,
                    env=proc_env,
                    preexec_fn=os.setsid,
                )

            now = _utcnow()
            session = TerminalSession(
                session_id=session_id,
                user_id=user_id,
                user_email=user_email,
                title=(title or os.path.basename(normalized_cwd) or "terminal").strip()[:200] or "terminal",
                shell=" ".join(launched_argv),
                cwd=normalized_cwd,
                backend_mode=backend_mode,
                cols=cols,
                rows=rows,
                created_at=now,
                updated_at=now,
                proc=proc,
                master_fd=master_fd,
                env={k: proc_env[k] for k in ("TERM", "COLORTERM", "COLUMNS", "LINES", "AADS_TERMINAL_SESSION_ID")},
            )
            self._sessions[session_id] = session
            self._attach_reader(session)
            session.wait_task = asyncio.create_task(self._wait_for_exit(session_id))
            self._emit_event(
                session,
                "status",
                status="running",
                pid=proc.pid,
                cwd=session.cwd,
                shell=session.shell,
                backend_mode=session.backend_mode,
            )
            if backend_mode == "pipe":
                self._emit_event(
                    session,
                    "warning",
                    code="PTY_UNAVAILABLE_PIPE_FALLBACK",
                    message="PTY unavailable in runtime; using pipe fallback. Full-screen interactive tools may not work.",
                )
            logger.info(
                "terminal_session_created session=%s user=%s pid=%s cwd=%s mode=%s",
                session_id,
                user_id,
                proc.pid,
                normalized_cwd,
                backend_mode,
            )
            return session.snapshot()

    def list_sessions(self, user_id: str) -> List[TerminalSessionOut]:
        self._prune_closed_sessions()
        sessions = [s.snapshot() for s in self._sessions.values() if s.user_id == user_id]
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def get_session(self, session_id: str, user_id: str) -> TerminalSessionOut:
        return self._require_session(session_id, user_id).snapshot()

    async def write_input(self, session_id: str, user_id: str, data: str) -> int:
        session = self._require_session(session_id, user_id)
        if session.proc.returncode is not None or session.status != "running":
            raise ValueError("terminal session is not running")
        encoded = data.encode("utf-8", errors="replace")
        if session.backend_mode == "pty":
            if session.master_fd is None:
                raise ValueError("terminal PTY is unavailable")
            written = os.write(session.master_fd, encoded)
        else:
            if session.proc.stdin is None:
                raise ValueError("terminal stdin pipe is unavailable")
            session.proc.stdin.write(encoded)
            await session.proc.stdin.drain()
            written = len(encoded)
        session.updated_at = _utcnow()
        self._remember_commands(session, data)
        return written

    async def execute_command(self, session_id: str, user_id: str, command: str) -> int:
        payload = command.rstrip("\n") + "\n"
        return await self.write_input(session_id, user_id, payload)

    async def resize(self, session_id: str, user_id: str, cols: int, rows: int) -> TerminalSessionOut:
        session = self._require_session(session_id, user_id)
        if session.backend_mode == "pty" and session.master_fd is not None:
            _set_winsize(session.master_fd, rows, cols)
        session.cols = cols
        session.rows = rows
        session.updated_at = _utcnow()
        if session.proc.returncode is None and session.backend_mode == "pty":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(session.proc.pid), signal.SIGWINCH)
        self._emit_event(session, "status", status=session.status, cols=cols, rows=rows)
        return session.snapshot()

    async def close_session(self, session_id: str, user_id: str) -> TerminalSessionOut:
        session = self._require_session(session_id, user_id)
        if session.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(session.proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(session.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(session.proc.pid), signal.SIGKILL)
                await session.proc.wait()
        return session.snapshot()

    def subscribe(self, session_id: str, user_id: str) -> asyncio.Queue:
        session = self._require_session(session_id, user_id)
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        session.subscribers.append(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        with contextlib.suppress(ValueError):
            session.subscribers.remove(q)

    def buffered_events(self, session_id: str, user_id: str, since_seq: Optional[int] = None) -> List[dict]:
        session = self._require_session(session_id, user_id)
        if since_seq is None:
            return list(session.buffer)
        return [event for event in session.buffer if event["seq"] > since_seq]

    def _require_session(self, session_id: str, user_id: str) -> TerminalSession:
        session = self._sessions.get(session_id)
        if not session or session.user_id != user_id:
            raise KeyError(session_id)
        return session

    def _attach_reader(self, session: TerminalSession) -> None:
        if session.backend_mode == "pty":
            if session.master_fd is None:
                return
            loop = asyncio.get_running_loop()
            loop.add_reader(session.master_fd, self._read_from_master, session.session_id)
            return
        if session.proc.stdout is not None:
            session.reader_tasks.append(
                asyncio.create_task(self._read_pipe_stream(session.session_id, session.proc.stdout, session.decoder, "stdout"))
            )
        if session.proc.stderr is not None:
            session.reader_tasks.append(
                asyncio.create_task(self._read_pipe_stream(session.session_id, session.proc.stderr, session.stderr_decoder, "stderr"))
            )

    def _detach_reader(self, session: TerminalSession) -> None:
        if session.backend_mode == "pty":
            if session.master_fd is None:
                return
            loop = asyncio.get_running_loop()
            with contextlib.suppress(Exception):
                loop.remove_reader(session.master_fd)
            return
        for task in session.reader_tasks:
            if not task.done():
                task.cancel()

    def _read_from_master(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if not session or session.master_fd is None:
            return
        while True:
            try:
                chunk = os.read(session.master_fd, 4096)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    self._detach_reader(session)
                    return
                logger.warning("terminal_read_error session=%s err=%s", session_id, exc)
                return
            if not chunk:
                self._detach_reader(session)
                return
            session.output_bytes += len(chunk)
            session.updated_at = _utcnow()
            text = session.decoder.decode(chunk)
            if text:
                self._emit_event(session, "output", data=_redact_sensitive_text(text))

    async def _read_pipe_stream(
        self,
        session_id: str,
        stream: asyncio.StreamReader,
        decoder: codecs.IncrementalDecoder,
        stream_name: str,
    ) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                session.output_bytes += len(chunk)
                session.updated_at = _utcnow()
                text = decoder.decode(chunk)
                if text:
                    self._emit_event(
                        session,
                        "output",
                        data=_redact_sensitive_text(text),
                        stream=stream_name,
                    )
        except asyncio.CancelledError:
            return
        finally:
            with contextlib.suppress(Exception):
                tail = decoder.decode(b"", final=True)
                if tail:
                    self._emit_event(
                        session,
                        "output",
                        data=_redact_sensitive_text(tail),
                        stream=stream_name,
                    )

    async def _wait_for_exit(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        returncode = await session.proc.wait()
        session.returncode = returncode
        session.status = "exited"
        session.closed_at = _utcnow()
        session.updated_at = session.closed_at
        self._detach_reader(session)
        with contextlib.suppress(Exception):
            tail = session.decoder.decode(b"", final=True)
            if tail:
                self._emit_event(session, "output", data=_redact_sensitive_text(tail))
        if session.master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(session.master_fd)
        self._emit_event(session, "exit", status="exited", returncode=returncode)
        logger.info("terminal_session_exited session=%s rc=%s", session_id, returncode)

    def _emit_event(self, session: TerminalSession, event_type: str, **payload: object) -> dict:
        session.last_seq += 1
        event = {
            "session_id": session.session_id,
            "seq": session.last_seq,
            "type": event_type,
            "timestamp": _utcnow().isoformat(),
            **payload,
        }
        session.buffer.append(event)
        for q in list(session.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("terminal_subscriber_queue_full session=%s", session.session_id)
        return event

    def _prune_closed_sessions(self) -> None:
        now = _utcnow()
        expired: List[str] = []
        for session_id, session in self._sessions.items():
            if session.closed_at and now - session.closed_at > CLOSED_SESSION_TTL:
                expired.append(session_id)
        for session_id in expired:
            session = self._sessions.pop(session_id, None)
            if not session:
                continue
            if session.wait_task and not session.wait_task.done():
                session.wait_task.cancel()

    def _remember_commands(self, session: TerminalSession, data: str) -> None:
        normalized = data.replace("\r", "")
        for line in normalized.split("\n"):
            command = line.strip()
            if command:
                session.recent_commands.append(command[:500])

    @staticmethod
    def _fallback_argv(argv: List[str]) -> List[str]:
        return [arg for arg in argv if arg not in {"-i", "--interactive"}]


terminal_runner = TerminalRunner()
