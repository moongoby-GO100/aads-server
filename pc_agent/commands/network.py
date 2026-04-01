"""PC Agent 네트워크 정보 수집 — WoL용 MAC 주소 자동 등록 (범용)."""
from __future__ import annotations

import logging
import socket
import subprocess
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_network_interfaces() -> List[Dict[str, str]]:
    """활성 네트워크 인터페이스의 MAC/IP 수집 (Windows/Linux 범용)."""
    interfaces = []

    # 방법 1: psutil
    try:
        import psutil
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        for iface_name, addr_list in addrs.items():
            if iface_name in stats and not stats[iface_name].isup:
                continue
            if "loopback" in iface_name.lower() or iface_name == "lo":
                continue
            mac = ""
            ipv4 = ""
            for addr in addr_list:
                if addr.family == psutil.AF_LINK:
                    mac = addr.address
                elif addr.family == socket.AF_INET:
                    if not addr.address.startswith("127."):
                        ipv4 = addr.address
            if mac and mac != "00:00:00:00:00:00" and ipv4:
                interfaces.append({"name": iface_name, "mac": mac, "ip": ipv4})
        return interfaces
    except ImportError:
        pass

    # 방법 2: ipconfig (Windows)
    try:
        result = subprocess.run(["ipconfig", "/all"], capture_output=True, text=True, timeout=10)
        blocks = re.split(r'\r?\n(?=\S)', result.stdout)
        for block in blocks:
            mac_match = re.search(r'Physical Address[.\s]*:\s*([\dA-Fa-f-]{17})', block)
            ip_match = re.search(r'IPv4 Address[.\s]*:\s*([\d.]+)', block)
            name_match = re.search(r'adapter\s+(.+?):', block)
            if mac_match and ip_match:
                mac = mac_match.group(1)
                ip_addr = ip_match.group(1).rstrip('(Preferred) ')
                name = name_match.group(1).strip() if name_match else "unknown"
                if mac != "00-00-00-00-00-00" and not ip_addr.startswith("127."):
                    interfaces.append({"name": name, "mac": mac.replace("-", ":"), "ip": ip_addr})
        return interfaces
    except Exception:
        pass

    # 방법 3: ip addr (Linux)
    try:
        result = subprocess.run(["ip", "addr"], capture_output=True, text=True, timeout=10)
        current_iface = ""
        current_mac = ""
        for line in result.stdout.split("\n"):
            iface_match = re.match(r'\d+:\s+(\S+):', line)
            if iface_match:
                current_iface = iface_match.group(1)
                current_mac = ""
            mac_match = re.search(r'link/ether\s+([\da-f:]{17})', line)
            if mac_match:
                current_mac = mac_match.group(1)
            ip_match = re.search(r'inet\s+([\d.]+)/', line)
            if ip_match and current_mac and not ip_match.group(1).startswith("127."):
                interfaces.append({"name": current_iface, "mac": current_mac, "ip": ip_match.group(1)})
        return interfaces
    except Exception:
        pass

    return interfaces


def get_primary_mac() -> Optional[Dict[str, str]]:
    """기본 네트워크 인터페이스의 MAC/IP 반환 (WoL 등록용)."""
    interfaces = _get_network_interfaces()
    if not interfaces:
        return None
    # 우선순위: 이더넷 > Wi-Fi > 기타
    for kw in ["ethernet", "이더넷", "eth", "en0", "lan"]:
        for iface in interfaces:
            if kw in iface["name"].lower():
                return iface
    for kw in ["wi-fi", "wifi", "wlan", "wireless"]:
        for iface in interfaces:
            if kw in iface["name"].lower():
                return iface
    return interfaces[0]


async def network_info(params: Dict[str, Any]) -> Dict[str, Any]:
    """PC 네트워크 인터페이스 정보 반환."""
    try:
        interfaces = _get_network_interfaces()
        primary = get_primary_mac()
        return {
            "status": "success",
            "data": {
                "hostname": socket.gethostname(),
                "primary": primary,
                "interfaces": interfaces,
                "count": len(interfaces),
            }
        }
    except Exception as e:
        logger.error("network_info error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def wol_register(params: Dict[str, Any]) -> Dict[str, Any]:
    """WoL용 MAC 주소 수동 등록 요청."""
    try:
        primary = get_primary_mac()
        if not primary:
            return {"status": "error", "data": {"error": "활성 네트워크 인터페이스를 찾을 수 없습니다."}}
        return {
            "status": "success",
            "data": {
                "mac_address": primary["mac"],
                "ip_address": primary["ip"],
                "interface": primary["name"],
                "broadcast_ip": params.get("broadcast_ip", "255.255.255.255"),
                "label": params.get("label", ""),
                "message": "서버에 등록 요청. agent.py 연결 시 자동 전송됩니다.",
            }
        }
    except Exception as e:
        logger.error("wol_register error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
