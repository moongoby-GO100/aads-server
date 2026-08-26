from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).resolve().parents[2] / "app/services/bank_collection_lock.py"
_SPEC = importlib.util.spec_from_file_location("bank_collection_lock_under_test", _PATH)
lock = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(lock)


def test_stale_lock_file_does_not_block_collection(tmp_path):
    path = tmp_path / ".bank_auto_collect.lock"
    path.write_text("dead-pid", encoding="ascii")

    fd = lock.try_acquire_bank_lock(path)
    assert fd is not None
    lock.release_bank_lock(fd)
    assert lock.bank_lock_is_active(path) is False


def test_live_bank_lock_is_visible_to_delivery_guard(tmp_path):
    path = tmp_path / ".bank_auto_collect.lock"
    fd = lock.try_acquire_bank_lock(path)
    assert fd is not None
    try:
        assert lock.bank_lock_is_active(path) is True
    finally:
        lock.release_bank_lock(fd)
