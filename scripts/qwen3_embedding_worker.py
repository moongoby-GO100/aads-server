#!/usr/bin/env python3
"""Canonical Qwen3 document embedding worker (never schedule on contabo14)."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.db_pool import get_pool
from app.services.doc_index import (
    QWEN_DIMENSION, QWEN_DOCUMENT_INSTRUCTION, QWEN_INSTRUCTION_VERSION,
    QWEN_MODEL_ID,
)

OLLAMA_URL = os.getenv("QWEN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LEASE_SECONDS = int(os.getenv("QWEN_LEASE_SECONDS", "90"))
HEARTBEAT_SECONDS = 15
MAX_CONCURRENCY = int(os.getenv("QWEN_MAX_CONCURRENCY", "3"))


def allowed_host(hostname: str) -> bool:
    return "contabo14" not in hostname.lower()


def require_loopback(url: str) -> None:
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("QWEN_OLLAMA_URL must remain loopback-only")


def desired_concurrency(load1: float, nproc: int, available_mb: int, latency_s: float) -> int:
    if available_mb < 1024 or load1 >= nproc * 1.25 or latency_s >= 45:
        return 0
    if available_mb < 2048 or load1 >= nproc * .9 or latency_s >= 20:
        return 1
    return max(1, min(MAX_CONCURRENCY, nproc // 3))


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
        except asyncio.TimeoutError:
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
        text = f"{chunk['title']}\n{chunk['heading']}\n{chunk['content']}"
        content_sha = hashlib.sha256(text.encode()).hexdigest()
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
               dimension=$2, content_sha256=$3, state='ready', lease_owner=NULL,
               lease_expires_at=NULL, heartbeat_at=NULL, error=NULL, updated_at=now()
               WHERE id=$4 AND lease_owner=$5 AND state='processing'""",
            str(vector), QWEN_DIMENSION, content_sha, row["id"], owner,
        )
        if result != "UPDATE 1":
            raise RuntimeError("lease lost")
    except Exception as exc:
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
    require_loopback(OLLAMA_URL)
    owner = f"{host}:{os.getpid()}"
    pool = get_pool()
    latency = 0.0
    async with httpx.AsyncClient(timeout=120.0) as client:
        while True:
            await sync_queue(pool)
            load1 = os.getloadavg()[0]
            mem_mb = int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1048576)
            concurrency = desired_concurrency(load1, os.cpu_count() or 1, mem_mb, latency)
            if concurrency == 0:
                if once: return
                await asyncio.sleep(30)
                continue
            async with pool.acquire() as conn:
                rows = await claim(conn, owner, concurrency)
            if not rows:
                if once: return
                await asyncio.sleep(10)
                continue
            timings = await asyncio.gather(*(process_one(pool, client, r, owner) for r in rows))
            latency = sum(timings) / len(timings)
            if once: return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    asyncio.run(run(parser.parse_args().once))
