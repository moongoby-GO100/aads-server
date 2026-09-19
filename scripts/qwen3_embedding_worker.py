#!/usr/bin/env python3
"""Canonical Qwen3 document embedding worker (never schedule on contabo14)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx

QWEN_MODEL_ID = "qwen3-embedding:0.6b"
QWEN_DIMENSION = 1024
QWEN_INSTRUCTION_VERSION = "qwen3-doc-v1"
QWEN_DOCUMENT_INSTRUCTION = "Represent this English document for retrieval: "

OLLAMA_URL = os.getenv("QWEN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
DATABASE_URL = os.getenv("QWEN_DATABASE_URL", "")
WORKER_SITE = os.getenv("QWEN_WORKER_SITE", "").strip().lower()
LEASE_SECONDS = int(os.getenv("QWEN_LEASE_SECONDS", "90"))
HEARTBEAT_SECONDS = 15
MAX_CONCURRENCY = int(os.getenv("QWEN_MAX_CONCURRENCY", "3"))
ALLOWED_SITES = {"cafe24_114", "jinah244"}


def log_event(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "ts": time.time(), **fields}, ensure_ascii=False), flush=True)


def allowed_host(hostname: str) -> bool:
    return "contabo14" not in hostname.lower()


def require_loopback(url: str) -> None:
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("QWEN_OLLAMA_URL must remain loopback-only")


def require_worker_site(site: str) -> None:
    if site not in ALLOWED_SITES:
        raise SystemExit(
            "QWEN_WORKER_SITE must be one of cafe24_114 or jinah244; "
            "contabo14 and unspecified hosts are forbidden"
        )


def require_safe_database_url(url: str) -> None:
    """Require a loopback tunnel or TLS for the central PostgreSQL link."""
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise SystemExit("QWEN_DATABASE_URL must be a PostgreSQL URL")
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return
    query = parsed.query.lower()
    if "sslmode=require" not in query and "sslmode=verify-" not in query:
        raise SystemExit("remote QWEN_DATABASE_URL must enforce TLS")


def canonical_chunk_text(chunk: Any) -> str:
    """Match the SQL coalesce() hashing contract exactly, including NULLs."""
    return "\n".join(str(chunk[field] or "") for field in ("title", "heading", "content"))


async def create_pool() -> Any:
    """Create a tiny standalone pool so remote hosts need no AADS checkout."""
    require_safe_database_url(DATABASE_URL)
    import asyncpg

    return await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=max(2, MAX_CONCURRENCY + 1),
        timeout=10,
        command_timeout=30,
    )


def desired_concurrency(load1: float, nproc: int, available_mb: int, latency_s: float) -> int:
    if available_mb < 1024 or load1 >= nproc * 1.25 or latency_s >= 45:
        return 0
    if available_mb < 2048 or load1 >= nproc * .9 or latency_s >= 20:
        return 1
    return max(1, min(MAX_CONCURRENCY, nproc // 3))


def bounded_concurrency(current: int, target: int) -> int:
    """Scale down immediately and recover one slot at a time."""
    return min(current + 1, target) if target > current else target


async def ollama_health_latency(client: httpx.AsyncClient) -> float:
    started = time.monotonic()
    try:
        response = await client.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        response.raise_for_status()
        return time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 - any health failure must isolate the worker
        log_event("qwen3_worker_ollama_unhealthy", error=str(exc)[:300])
        return 60.0


async def claim(conn: Any, owner: str, limit: int) -> list[Any]:
    """Atomic SKIP LOCKED claim; expired processing leases are reclaimable."""
    return await conn.fetch(
        """
        WITH picked AS (
          SELECT q.id FROM doc_chunk_embeddings_qwen3 q
          WHERE (q.state IN ('pending','error') OR
                 (q.state='processing' AND q.lease_expires_at < now()))
          ORDER BY q.updated_at FOR UPDATE SKIP LOCKED LIMIT $1
        )
        UPDATE doc_chunk_embeddings_qwen3 q
        SET state='processing', lease_owner=$2, worker=$2, heartbeat_at=now(),
            lease_expires_at=now()+($3::int * interval '1 second'), error=NULL,
            updated_at=now()
        FROM picked WHERE q.id=picked.id
        RETURNING q.id, q.chunk_id
        """, limit, owner, LEASE_SECONDS,
    )


async def sync_queue(pool: Any) -> None:
    """Idempotently enqueue new chunks and invalidate changed ready embeddings."""
    await pool.execute(
        """
        INSERT INTO doc_chunk_embeddings_qwen3
          (chunk_id, model_id, dimension, instruction_version, content_sha256)
        SELECT id, $1, $2, $3, encode(digest(
          coalesce(title,'')||E'\n'||coalesce(heading,'')||E'\n'||coalesce(content,''),
          'sha256'), 'hex')
        FROM doc_chunks
        ON CONFLICT (chunk_id, model_id, instruction_version) DO UPDATE
        SET content_sha256=EXCLUDED.content_sha256, embedding=NULL, state='pending',
            lease_owner=NULL, lease_expires_at=NULL, error=NULL, updated_at=now()
        WHERE doc_chunk_embeddings_qwen3.content_sha256 <> EXCLUDED.content_sha256
        """, QWEN_MODEL_ID, QWEN_DIMENSION, QWEN_INSTRUCTION_VERSION,
    )


async def heartbeat(pool: Any, row_id: int, owner: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except TimeoutError:
            await pool.execute(
                "UPDATE doc_chunk_embeddings_qwen3 SET heartbeat_at=now(), "
                "lease_expires_at=now()+($3::int*interval '1 second'), updated_at=now() "
                "WHERE id=$1 AND lease_owner=$2 AND state='processing'",
                row_id, owner, LEASE_SECONDS,
            )


async def process_one(pool: Any, client: httpx.AsyncClient, row: Any, owner: str) -> float:
    stop = asyncio.Event()
    beat = asyncio.create_task(heartbeat(pool, row["id"], owner, stop))
    started = time.monotonic()
    try:
        chunk = await pool.fetchrow(
            "SELECT title,heading,content FROM doc_chunks WHERE id=$1", row["chunk_id"]
        )
        if chunk is None:
            raise RuntimeError("chunk deleted after claim")
        text = canonical_chunk_text(chunk)
        response = await client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": QWEN_MODEL_ID, "input": QWEN_DOCUMENT_INSTRUCTION + text[:4000]},
        )
        response.raise_for_status()
        vector = (response.json().get("embeddings") or [[]])[0]
        if len(vector) != QWEN_DIMENSION:
            raise ValueError(f"dimension {len(vector)}")
        result = await pool.execute(
            """UPDATE doc_chunk_embeddings_qwen3 SET embedding=$1::vector,
               dimension=$2, content_sha256=encode(digest($3, 'sha256'), 'hex'),
               state='ready', lease_owner=NULL,
               lease_expires_at=NULL, heartbeat_at=NULL, error=NULL, updated_at=now()
               WHERE id=$4 AND lease_owner=$5 AND state='processing'""",
            str(vector), QWEN_DIMENSION, text, row["id"], owner,
        )
        if result != "UPDATE 1":
            raise RuntimeError("lease lost")
    except Exception as exc:  # noqa: BLE001 - persist per-item failures and continue safely
        log_event("qwen3_worker_item_error", row_id=row["id"], error=str(exc)[:300])
        await pool.execute(
            "UPDATE doc_chunk_embeddings_qwen3 SET state='error', error=$1, "
            "lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
            "WHERE id=$2 AND lease_owner=$3", str(exc)[:1000], row["id"], owner,
        )
    finally:
        stop.set()
        await beat
    return time.monotonic() - started


async def run(once: bool = False) -> None:
    host = socket.gethostname()
    if not allowed_host(host):
        raise SystemExit("contabo14 is explicitly forbidden for Qwen3 embedding")
    require_worker_site(WORKER_SITE)
    require_loopback(OLLAMA_URL)
    owner = f"{WORKER_SITE}:{host}:{os.getpid()}"
    pool = await create_pool()
    latency = 0.0
    current_concurrency = 1
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=5.0)) as client:
            while True:
                await sync_queue(pool)
                load1 = os.getloadavg()[0]
                mem_mb = int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1048576)
                target = desired_concurrency(load1, os.cpu_count() or 1, mem_mb, latency)
                current_concurrency = bounded_concurrency(current_concurrency, target)
                log_event(
                    "qwen3_worker_capacity",
                    site=WORKER_SITE,
                    load1=round(load1, 2),
                    available_mb=mem_mb,
                    ollama_latency_s=round(latency, 3),
                    target=target,
                    concurrency=current_concurrency,
                )
                if current_concurrency == 0:
                    if once:
                        return
                    await asyncio.sleep(30)
                    latency = await ollama_health_latency(client)
                    continue
                async with pool.acquire() as conn:
                    rows = await claim(conn, owner, current_concurrency)
                if not rows:
                    if once:
                        return
                    await asyncio.sleep(10)
                    continue
                timings = await asyncio.gather(*(process_one(pool, client, r, owner) for r in rows))
                latency = sum(timings) / len(timings)
                log_event(
                    "qwen3_worker_batch",
                    site=WORKER_SITE,
                    rows=len(rows),
                    average_latency_s=round(latency, 3),
                )
                if once:
                    return
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    asyncio.run(run(parser.parse_args().once))
