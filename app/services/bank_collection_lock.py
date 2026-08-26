"""Process-wide exclusion guard for bank PC-Agent collection."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


def default_bank_lock_path(root_dir: str | Path | None = None) -> str:
    root = Path(root_dir or Path(__file__).resolve().parents[2])
    return os.getenv(
        "YEOLJEONG_BANK_AUTO_COLLECT_LOCK_PATH",
        str(root / "app" / "data" / "yeoljeong_finance" / ".bank_auto_collect.lock"),
    )


def try_acquire_bank_lock(lock_path: str | Path) -> int | None:
    """Acquire an fd lock; an old lock file without a live fd is harmless."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("ascii"))
    return fd


def release_bank_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def bank_lock_is_active(lock_path: str | Path) -> bool:
    """Return true only when another live process currently owns the fd lock."""
    fd = try_acquire_bank_lock(lock_path)
    if fd is None:
        return True
    release_bank_lock(fd)
    return False
