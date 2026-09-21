"""오류 사전 조회 — 앱 안에서 쓰는 쪽.

셸 쪽은 `scripts/error_book.py` 가 담당한다(러너·배포). 이 모듈은 컨테이너
안에서 DB 풀로 직접 조회한다. 같은 테이블(`ohvis_wiki_error_book`)을 본다.

2026-09-14, 같은 실패를 세 세션이 "러너 계정 문제" 로 보고했다. 실제 원인은
호스트/컨테이너 경로 불일치였다. 증상에서 원인으로 가는 길이 없으면 사람이
매번 처음부터 추적한다. 오류가 들어오는 자리마다 이 조회를 붙인다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 120.0
_cache: dict[str, Any] = {"at": 0.0, "entries": []}


async def _load_entries() -> list[dict]:
    """사전 항목. 2분 캐시 — 오류가 몰릴 때 DB 를 때리지 않는다."""
    now = time.monotonic()
    if (now - float(_cache["at"])) < _CACHE_TTL_SEC:
        return list(_cache["entries"])
    try:
        from app.core.db_pool import get_pool

        rows = await get_pool().fetch(
            "SELECT error_key, symptom, root_cause, prevention, recurrence_count, metadata "
            "FROM ohvis_wiki_error_book WHERE status = 'active'"
        )
    except Exception as exc:
        # 사전 조회 실패가 오류 처리 자체를 막으면 안 된다.
        logger.debug("error_book_load_failed: %s", str(exc)[:120])
        return list(_cache["entries"])

    entries: list[dict] = []
    for row in rows:
        meta = row["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        entries.append({
            "error_key": row["error_key"],
            "symptom": row["symptom"],
            "root_cause": row["root_cause"],
            "prevention": row["prevention"],
            "recurrence_count": row["recurrence_count"],
            "signatures": (meta or {}).get("signatures") or [],
            # 이번에 무엇을 고쳤는지. prevention("앞으로 이렇게 해라")과 다르다.
            # fix 가 없으면 재발했을 때 고친 것이 되돌아간 건지, 다른 경로가
            # 같은 버그를 밟은 건지 구분할 수 없다.
            "fix": (meta or {}).get("fix") or {},
        })
    _cache["at"] = now
    _cache["entries"] = entries
    return list(entries)


async def match_error(text: Any, *, limit: int = 2) -> list[dict]:
    """오류 텍스트에서 알려진 항목을 찾는다. 없으면 빈 목록.

    서명은 정규식이다. 오류 문구는 버전마다 달라지므로 완전 일치를 요구하지
    않는다. 정규식이 깨져 있으면 부분 문자열로 떨어진다 — 사전 한 줄이 잘못
    적혔다고 조회 전체가 죽으면 안 된다.
    """
    body = str(text or "")
    if not body.strip():
        return []
    hits: list[dict] = []
    for entry in await _load_entries():
        for sig in entry["signatures"]:
            try:
                matched = bool(re.search(sig, body, re.I | re.S))
            except re.error:
                matched = str(sig).lower() in body.lower()
            if matched:
                hits.append(entry)
                break
        if len(hits) >= limit:
            break
    return hits


async def record_candidate(
    text: Any,
    source: str = "",
    *,
    project: str = "AADS",
    metadata: dict[str, Any] | None = None,
) -> str:
    """알려지지 않은 오류를 후보로 남긴다.

    원인을 모르는 채 active 로 넣으면 사전이 오염된다. status='candidate' 로
    두어 "알려진 원인" 으로 행세하지 않게 하되, **증상이 어디에도 안 남는 것**은
    막는다. 사람이 원인을 채우면 active 로 올린다.

    서명은 변하는 값(숫자·해시)을 지운 형태다. 안 지우면 요청마다 새 항목이
    쌓인다.
    """
    body = str(text or "")
    cand = ""
    for line in body.splitlines():
        line = line.strip()
        if line and re.search(r"error|exception|failed|refused|timeout|denied", line, re.I):
            cand = line
            break
    if not cand:
        cand = next((l.strip() for l in body.splitlines() if l.strip()), "")
    if not cand:
        return ""
    cand = cand[:300]

    norm = re.sub(r"[0-9a-fA-F]{8,}", "\u00a7H\u00a7", cand[:160])
    norm = re.sub(r"\d+", "\u00a7N\u00a7", norm)
    sig = re.escape(norm)
    sig = sig.replace(re.escape("\u00a7H\u00a7"), "[0-9a-fA-F]+")
    sig = sig.replace(re.escape("\u00a7N\u00a7"), "[0-9]+")

    key = "auto." + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
    project_key = str(project or "AADS").strip().upper()[:32] or "AADS"
    candidate_meta = {"signatures": [sig], "source": source}
    candidate_meta.update(metadata or {})
    try:
        from app.core.db_pool import get_pool

        await get_pool().execute(
            """
            INSERT INTO ohvis_wiki_error_book
                (project, error_key, symptom, root_cause, prevention, status, metadata)
            VALUES ($1, $2, $3, '', '', 'candidate', $4::jsonb)
            ON CONFLICT (project, error_key) DO UPDATE
                SET recurrence_count = ohvis_wiki_error_book.recurrence_count + 1,
                    updated_at = NOW()
            """,
            project_key, key, cand,
            json.dumps(candidate_meta, ensure_ascii=False),
        )
    except Exception as exc:
        logger.debug("error_book_record_failed: %s", str(exc)[:120])
        return ""
    return key


async def ingest_error_event(
    *,
    error_type: str,
    source: str,
    message: str,
    server: str = "",
    stack_trace: str = "",
    project: str = "AADS",
    error_hash: str = "",
) -> dict[str, Any]:
    """운영 오류 한 건을 공용 오류 사전과 동기화한다.

    `error_log`는 발생 원장이고 오류 사전은 원인·예방 지식이다. 기존에는 두
    저장소가 분리되어 Watchdog/모델 오류가 `error_log`에만 남고 오류 사전에는
    자동 등록되지 않았다. 모든 writer가 이 함수를 호출하면 알려진 오류는 재발
    횟수를 올리고, 모르는 오류는 원인 미상 candidate로 남는다.

    오류 사전 장애가 원래 오류 기록을 막아서는 안 되므로 실패는 구조화해
    반환하고 예외를 밖으로 전파하지 않는다.
    """
    probe = " ".join(
        part.strip()
        for part in (error_type, source, server, message, stack_trace)
        if str(part or "").strip()
    )
    if not probe:
        return {"status": "skipped", "reason": "empty_error"}
    try:
        known = await match_error(probe)
        if known:
            keys = []
            for hit in known:
                key = str(hit.get("error_key") or "")
                if key:
                    await bump_recurrence(key)
                    keys.append(key)
            return {"status": "matched", "error_keys": keys}

        key = await record_candidate(
            probe,
            source=f"error_log:{source or error_type}",
            project=project,
            metadata={
                "error_hash": str(error_hash or ""),
                "error_type": str(error_type or ""),
                "server": str(server or ""),
                "ingest": "error_log",
            },
        )
        if not key:
            return {"status": "failed", "reason": "candidate_not_written"}
        return {"status": "candidate", "error_key": key}
    except Exception as exc:
        logger.warning("error_book_ingest_failed: %s", str(exc)[:160])
        return {"status": "failed", "reason": str(exc)[:160]}


async def bump_recurrence(error_key: str) -> None:
    """재발 횟수를 올린다. 실패해도 조용히 넘어간다."""
    try:
        from app.core.db_pool import get_pool

        await get_pool().execute(
            "UPDATE ohvis_wiki_error_book SET recurrence_count = recurrence_count + 1, "
            "updated_at = NOW() WHERE error_key = $1",
            str(error_key),
        )
    except Exception as exc:
        logger.debug("error_book_bump_failed: %s", str(exc)[:120])
