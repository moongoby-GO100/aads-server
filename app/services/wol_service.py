"""Wake-on-LAN 서비스 — 원격 PC 부팅 (범용)."""
from __future__ import annotations

import logging
import socket
from typing import Optional

logger = logging.getLogger(__name__)


def create_magic_packet(mac_address: str) -> bytes:
    """MAC 주소로 매직 패킷(102바이트) 생성."""
    mac = mac_address.replace(":", "").replace("-", "").replace(".", "").upper()
    if len(mac) != 12:
        raise ValueError(f"유효하지 않은 MAC 주소: {mac_address}")
    mac_bytes = bytes.fromhex(mac)
    return b'\xff' * 6 + mac_bytes * 16


def send_wol(mac_address: str, broadcast_ip: str = "255.255.255.255", port: int = 9) -> dict:
    """Wake-on-LAN 매직 패킷 전송."""
    try:
        packet = create_magic_packet(mac_address)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, (broadcast_ip, port))
        logger.info("WoL 매직 패킷 전송: MAC=%s, broadcast=%s:%d", mac_address, broadcast_ip, port)
        return {
            "status": "success",
            "mac_address": mac_address,
            "broadcast_ip": broadcast_ip,
            "port": port,
            "message": f"매직 패킷 전송 완료. PC 부팅까지 30초~2분 소요."
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.error("WoL 전송 실패: %s", e)
        return {"status": "error", "error": str(e)}


async def ensure_network_table() -> None:
    """네트워크 정보 테이블 자동 생성."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        if pool is None:
            return
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS kakao_pc_agent_network (
                    id SERIAL PRIMARY KEY,
                    agent_id VARCHAR(64) UNIQUE NOT NULL,
                    mac_address VARCHAR(20) NOT NULL,
                    ip_address VARCHAR(45) DEFAULT '',
                    broadcast_ip VARCHAR(45) DEFAULT '255.255.255.255',
                    label VARCHAR(100) DEFAULT '',
                    last_seen TIMESTAMPTZ DEFAULT NOW(),
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    except Exception as e:
        logger.error("네트워크 테이블 생성 실패: %s", e)


async def register_agent_network(agent_id: str, mac_address: str, ip_address: str = "",
                                  broadcast_ip: str = "255.255.255.255", label: str = "") -> dict:
    """PC Agent의 네트워크 정보 DB 등록/갱신 (UPSERT)."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        if pool is None:
            return {"status": "error", "error": "DB 연결 없음"}
        await ensure_network_table()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO kakao_pc_agent_network (agent_id, mac_address, ip_address, broadcast_ip, label, last_seen)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (agent_id) DO UPDATE SET
                    mac_address = EXCLUDED.mac_address,
                    ip_address = EXCLUDED.ip_address,
                    broadcast_ip = COALESCE(NULLIF(EXCLUDED.broadcast_ip, ''), kakao_pc_agent_network.broadcast_ip),
                    label = COALESCE(NULLIF(EXCLUDED.label, ''), kakao_pc_agent_network.label),
                    last_seen = NOW()
            """, agent_id, mac_address, ip_address, broadcast_ip, label)
        logger.info("Agent 네트워크 등록: %s MAC=%s IP=%s", agent_id, mac_address, ip_address)
        return {"status": "success", "agent_id": agent_id, "mac_address": mac_address}
    except Exception as e:
        logger.error("register_agent_network 실패: %s", e)
        return {"status": "error", "error": str(e)}


async def wake_agent(agent_id: str) -> dict:
    """DB에서 agent_id로 MAC 주소 조회 후 WoL 매직 패킷 전송."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        if pool is None:
            return {"status": "error", "error": "DB 연결 없음"}
        await ensure_network_table()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT mac_address, broadcast_ip, label FROM kakao_pc_agent_network WHERE agent_id = $1",
                agent_id
            )
        if not row:
            return {"status": "error", "error": f"agent_id '{agent_id}'의 네트워크 정보 없음. PC Agent가 한번이라도 연결된 적이 있어야 합니다."}
        mac = row["mac_address"]
        broadcast = row["broadcast_ip"] or "255.255.255.255"
        label = row["label"] or agent_id[:8]
        result = send_wol(mac, broadcast)
        result["agent_id"] = agent_id
        result["label"] = label
        return result
    except Exception as e:
        logger.error("wake_agent 실패: %s", e)
        return {"status": "error", "error": str(e)}


async def list_agents_network() -> dict:
    """등록된 모든 에이전트의 네트워크 정보 조회."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        if pool is None:
            return {"status": "error", "error": "DB 연결 없음"}
        await ensure_network_table()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT agent_id, mac_address, ip_address, broadcast_ip, label, last_seen FROM kakao_pc_agent_network ORDER BY last_seen DESC"
            )
        agents = [
            {
                "agent_id": r["agent_id"],
                "mac_address": r["mac_address"],
                "ip_address": r["ip_address"],
                "broadcast_ip": r["broadcast_ip"],
                "label": r["label"],
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            }
            for r in rows
        ]
        return {"status": "success", "agents": agents, "count": len(agents)}
    except Exception as e:
        return {"status": "error", "error": str(e)}
