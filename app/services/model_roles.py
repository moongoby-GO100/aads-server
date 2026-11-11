"""모델 역할(기본값·폴백 순서·별칭·허용 목록) DB 조회 계층.

원본은 DB 표 ``llm_model_roles`` 다. 코드에 있는 값(``model_roles_seed``)은
마이그레이션 시드와 같은 내용이며 **DB 조회가 실패했을 때만** 쓴다.

읽기 순서 (fail-safe)
  1. DB 조회에 성공한 최신 스냅샷 — 비어 있어도 그대로 신뢰한다(코드 목록으로 우회 금지).
  2. DB 조회가 한 번도 성공하지 못했을 때: 코드 시드.
  마지막 성공 스냅샷은 이후 조회가 실패해도 유지된다.

호출부는 동기 코드가 대부분이라 읽기는 메모리 스냅샷에서 즉시 하고,
갱신은 비동기로 한다(``refresh_roles``; TTL 만료 시 실행 중인 루프에 자동 예약).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Mapping, Sequence, Set
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from app.services.model_roles_seed import ROLE_SEED

logger = logging.getLogger(__name__)

ROLES_TTL_SECONDS = 120.0

# 마지막으로 DB 조회에 성공한 스냅샷: {role_key: [(item_key, model_id, priority, attrs), ...]}
_snapshot: Optional[Dict[str, List[Tuple[str, str, int, Dict[str, Any]]]]] = None
_snapshot_expires = 0.0
_refresh_task: Optional["asyncio.Task[Any]"] = None
_first_load_lock = threading.Lock()
_first_load_retry_at = 0.0
_FIRST_LOAD_TIMEOUT = 3.0
_FIRST_LOAD_RETRY_SECONDS = 15.0
# 첫 적재를 시도했고 실패했음(→ 코드 시드 사용). 비동기 적재가 다시 성공하면 해제된다.
_first_load_failed = False
_async_load_lock: Optional["asyncio.Lock"] = None


class ModelRolesNotLoadedError(RuntimeError):
    """이벤트 루프 스레드에서 스냅샷 없이 동기 API 를 불렀다.

    동기 조회는 DB 를 기다리며 스레드를 막으므로 루프 스레드에서는 쓸 수 없다.
    ``await role_scalar_async(...)`` 등을 쓰거나 시작 시 ``await ensure_loaded()`` 를 먼저 부른다.
    """

_SELECT_SQL = (
    "SELECT role_key, item_key, model_id, priority, attrs "
    "FROM llm_model_roles WHERE enabled = TRUE ORDER BY role_key, priority, item_key, model_id"
)


def _seed_rows(role: str) -> List[Tuple[str, str, int, Dict[str, Any]]]:
    return [(ik, mid, pr, dict(at)) for ik, mid, pr, at in ROLE_SEED.get(role, ())]


def _rows(role: str) -> List[Tuple[str, str, int, Dict[str, Any]]]:
    if _snapshot is None and not _first_load_failed:
        # 첫 접근: DB 조회를 먼저 시도한다. 실패가 확인된 경우에만 코드 시드를 쓴다.
        _raise_if_on_event_loop()
        _load_first_snapshot_blocking()
    elif _snapshot is not None:
        _maybe_schedule_refresh()
    if _snapshot is None:  # DB 조회 실패가 확인됨 → 코드 시드
        return _seed_rows(role)
    return list(_snapshot.get(role, ()))  # 성공 + 비어 있음 = 비어 있음


def _raise_if_on_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ModelRolesNotLoadedError(
        "model_roles 스냅샷이 아직 없는데 이벤트 루프 스레드에서 동기 API 를 호출했다 — "
        "role_*_async() 를 await 하거나 시작 시 ensure_loaded() 를 먼저 호출하라"
    )


def _maybe_schedule_refresh() -> None:
    global _refresh_task
    if time.monotonic() < _snapshot_expires:
        return
    if _refresh_task is not None and not _refresh_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _refresh_task = loop.create_task(refresh_roles())


def _build_snapshot(fetched: Any) -> Dict[str, List[Tuple[str, str, int, Dict[str, Any]]]]:
    snap: Dict[str, List[Tuple[str, str, int, Dict[str, Any]]]] = {}
    for r in fetched:
        attrs = r["attrs"]
        if isinstance(attrs, str):
            attrs = json.loads(attrs or "{}")
        snap.setdefault(r["role_key"], []).append(
            (r["item_key"] or "", r["model_id"] or "", int(r["priority"] or 0), dict(attrs or {}))
        )
    return snap


async def _fetch_with_own_connection() -> Any:
    """전용 연결로 한 번 조회한다(풀은 메인 루프 소유라 다른 스레드 루프에서 못 쓴다)."""
    import asyncpg

    from app.core.db_pool import _db_url

    dsn = _db_url()
    if not dsn:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다")
    conn = await asyncpg.connect(dsn, timeout=_FIRST_LOAD_TIMEOUT)
    try:
        return await asyncio.wait_for(conn.fetch(_SELECT_SQL), timeout=_FIRST_LOAD_TIMEOUT)
    finally:
        await conn.close()


def _load_first_snapshot_blocking() -> None:
    """스냅샷이 아직 없을 때 동기로 DB 를 조회한다(실행 중인 루프가 있어도 별도 스레드 사용).

    실패하면 _snapshot 은 None 으로 남고(→ 코드 시드), 재시도 폭주를 막기 위해
    ``_FIRST_LOAD_RETRY_SECONDS`` 동안 다시 시도하지 않는다.
    """
    global _snapshot, _snapshot_expires, _first_load_retry_at, _first_load_failed
    if time.monotonic() < _first_load_retry_at:
        return
    with _first_load_lock:
        if _snapshot is not None or time.monotonic() < _first_load_retry_at:
            return
        box: Dict[str, Any] = {}

        def _worker() -> None:
            try:
                box["rows"] = asyncio.run(_fetch_with_own_connection())
            except Exception as exc:
                box["err"] = exc

        th = threading.Thread(target=_worker, name="model-roles-first-load", daemon=True)
        th.start()
        th.join(_FIRST_LOAD_TIMEOUT * 2 + 1)
        if "rows" in box:
            try:
                _snapshot = _build_snapshot(box["rows"])
                _snapshot_expires = time.monotonic() + ROLES_TTL_SECONDS
                return
            except Exception as exc:
                box["err"] = exc
        logger.warning("model_roles_first_load_failed: %s", box.get("err", "timeout"))
        _first_load_retry_at = time.monotonic() + _FIRST_LOAD_RETRY_SECONDS
        _first_load_failed = True


async def ensure_loaded() -> None:
    """스냅샷이 없으면 이벤트 루프를 막지 않고 DB 에서 첫 적재를 한다(startup 에서 1회 호출 가능).

    실패하면 코드 시드를 쓰도록 표시하고, ``_FIRST_LOAD_RETRY_SECONDS`` 뒤 다음 호출에서 재시도한다.
    """
    global _snapshot, _snapshot_expires, _first_load_retry_at, _first_load_failed, _async_load_lock
    if _snapshot is not None:
        _maybe_schedule_refresh()
        return
    if time.monotonic() < _first_load_retry_at:
        return
    if _async_load_lock is None:
        _async_load_lock = asyncio.Lock()
    async with _async_load_lock:
        if _snapshot is not None or time.monotonic() < _first_load_retry_at:
            return
        try:
            fetched = await _fetch_with_own_connection()
            _snapshot = _build_snapshot(fetched)
        except Exception as exc:
            logger.warning("model_roles_first_load_failed: %s", exc)
            _first_load_retry_at = time.monotonic() + _FIRST_LOAD_RETRY_SECONDS
            _first_load_failed = True
            return
        _snapshot_expires = time.monotonic() + ROLES_TTL_SECONDS
        _first_load_failed = False


async def refresh_roles(force: bool = False) -> bool:
    """DB 에서 역할 표를 다시 읽는다. 성공 여부를 돌려준다(실패 시 기존 스냅샷 유지)."""
    global _snapshot, _snapshot_expires, _first_load_failed
    if not force and time.monotonic() < _snapshot_expires:
        return True
    # 실패해도 재시도 폭주를 막기 위해 짧게 만료를 민다.
    _snapshot_expires = time.monotonic() + 15.0
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            fetched = await conn.fetch(_SELECT_SQL)
    except Exception as exc:  # DB 장애: 마지막 성공 스냅샷(없으면 시드) 유지
        logger.warning("model_roles_refresh_failed: %s", exc)
        if _snapshot is None:  # 첫 적재 실패 확인 → 동기 조회는 코드 시드를 쓴다
            _first_load_failed = True
        return False
    _first_load_failed = False
    _snapshot = _build_snapshot(fetched)
    _snapshot_expires = time.monotonic() + ROLES_TTL_SECONDS
    return True


def invalidate_roles_cache() -> None:
    global _snapshot_expires
    _snapshot_expires = 0.0


def reset_for_tests() -> None:
    global _snapshot, _snapshot_expires, _refresh_task, _first_load_retry_at
    global _first_load_failed, _async_load_lock
    _snapshot, _snapshot_expires, _refresh_task = None, 0.0, None
    _first_load_retry_at = 0.0
    _first_load_failed = False
    _async_load_lock = None


# ── 단순 접근자 ────────────────────────────────────────────────────────────
def role_list(role: str) -> List[str]:
    return [mid for ik, mid, _p, _a in _rows(role) if ik == ""]


def role_scalar(role: str, default: str = "") -> str:
    values = role_list(role)
    return values[0] if values else default


async def role_list_async(role: str) -> List[str]:
    """async 경로용. 첫 적재를 루프를 막지 않고 끝낸 뒤 읽는다."""
    await ensure_loaded()
    return role_list(role)


async def role_scalar_async(role: str, default: str = "") -> str:
    """async 경로용 ``role_scalar``."""
    await ensure_loaded()
    return role_scalar(role, default)


def role_map(role: str) -> Dict[str, str]:
    return {ik: mid for ik, mid, _p, _a in _rows(role) if ik != ""}


def role_map_list(role: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for ik, mid, _p, _a in _rows(role):
        if ik != "":
            out.setdefault(ik, []).append(mid)
    return out


def role_value_map(role: str) -> Dict[str, Any]:
    """attrs['v'] 를 값으로 쓰는 표(가격·max_tokens·rank). 리스트 값은 tuple 로 돌려준다."""
    out: Dict[str, Any] = {}
    for ik, _mid, _p, at in _rows(role):
        if ik != "":
            v = at.get("v")
            out[ik] = tuple(v) if isinstance(v, list) else v
    return out


# ── 지연 평가 컨테이너: 기존 모듈 상수(set/dict/list)를 그대로 대체한다 ───────
# 읽을 때마다 현재 스냅샷으로 계산하므로 import 시점 값이 굳지 않는다.
class LiveSet(Set):
    def __init__(self, builder: Callable[[], Any], label: str = "") -> None:
        self._builder = builder
        self._label = label

    def _data(self) -> frozenset:
        return frozenset(self._builder())

    def __contains__(self, x: object) -> bool:
        return x in self._data()

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    @classmethod
    def _from_iterable(cls, it):  # 집합 연산 결과는 일반 set
        return set(it)

    def __ror__(self, other):
        return set(other) | self._data()

    def __repr__(self) -> str:
        return f"LiveSet({self._label!r}, {sorted(self._data())!r})"


class LiveList(Sequence):
    def __init__(self, role: str) -> None:
        self._role = role

    def __getitem__(self, i):
        return role_list(self._role)[i]

    def __len__(self) -> int:
        return len(role_list(self._role))

    def __repr__(self) -> str:
        return f"LiveList({self._role!r}, {role_list(self._role)!r})"


class LiveMap(Mapping):
    def __init__(self, builder: Callable[[], Dict[str, Any]], label: str = "") -> None:
        self._builder = builder
        self._label = label

    def __getitem__(self, k):
        return self._builder()[k]

    def get(self, k, default=None):
        return self._builder().get(k, default)

    def __contains__(self, k: object) -> bool:
        return k in self._builder()

    def __iter__(self):
        return iter(self._builder())

    def __len__(self) -> int:
        return len(self._builder())

    def __repr__(self) -> str:
        return f"LiveMap({self._label!r}, {self._builder()!r})"


def live_set(role: str) -> LiveSet:
    return LiveSet(lambda: role_list(role), role)


def live_list(role: str) -> LiveList:
    return LiveList(role)


def live_map(role: str) -> LiveMap:
    return LiveMap(lambda: role_map(role), role)


def live_map_list(role: str) -> LiveMap:
    return LiveMap(lambda: role_map_list(role), role)


def live_value_map(role: str) -> LiveMap:
    return LiveMap(lambda: role_value_map(role), role)


def live_union(*parts: Any) -> LiveSet:
    """여러 집합(LiveSet 포함)의 합집합. 읽을 때마다 다시 계산한다."""
    return LiveSet(lambda: set().union(*[set(p) for p in parts]), "union")
