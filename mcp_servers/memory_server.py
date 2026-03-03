"""
Memory MCP 서버 — 포트 8767.
도구: memory_store, memory_retrieve, memory_delete, memory_list, memory_search
인메모리 key-value 저장소. namespace 지원. 서버 재시작 시 초기화됨.
"""
import time
from typing import Any
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "aads-memory",
    host="0.0.0.0",
    port=8767,
)

# 인메모리 저장소: namespace → {key: {value, created_at, tags}}
_store: dict[str, dict[str, dict[str, Any]]] = {}


def _get_ns(namespace: str) -> dict[str, dict[str, Any]]:
    """namespace 가져오기 (없으면 생성)."""
    if namespace not in _store:
        _store[namespace] = {}
    return _store[namespace]


@mcp.tool()
def memory_store(key: str, value: str, namespace: str = "default", tags: list[str] | None = None) -> str:
    """키-값 저장.

    Args:
        key: 저장 키
        value: 저장할 값 (문자열)
        namespace: 네임스페이스 (기본: default)
        tags: 검색용 태그 목록
    Returns:
        완료 메시지
    """
    ns = _get_ns(namespace)
    ns[key] = {
        "value": value,
        "created_at": time.time(),
        "updated_at": time.time(),
        "tags": tags or [],
    }
    return f"저장 완료: {namespace}/{key}"


@mcp.tool()
def memory_retrieve(key: str, namespace: str = "default") -> str:
    """키로 값 조회.

    Args:
        key: 조회 키
        namespace: 네임스페이스
    Returns:
        저장된 값 (없으면 오류 메시지)
    """
    ns = _get_ns(namespace)
    if key not in ns:
        return f"[없음] {namespace}/{key}"
    entry = ns[key]
    return entry["value"]


@mcp.tool()
def memory_delete(key: str, namespace: str = "default") -> str:
    """키 삭제.

    Args:
        key: 삭제할 키
        namespace: 네임스페이스
    Returns:
        완료 메시지
    """
    ns = _get_ns(namespace)
    if key not in ns:
        return f"[없음] {namespace}/{key}"
    del ns[key]
    return f"삭제 완료: {namespace}/{key}"


@mcp.tool()
def memory_list(namespace: str = "default") -> list[str]:
    """네임스페이스의 모든 키 목록.

    Args:
        namespace: 네임스페이스
    Returns:
        키 목록
    """
    ns = _get_ns(namespace)
    return sorted(ns.keys())


@mcp.tool()
def memory_search(query: str, namespace: str = "default") -> list[dict]:
    """키 또는 값에서 검색.

    Args:
        query: 검색 문자열
        namespace: 네임스페이스
    Returns:
        매칭 항목 목록 [{key, value_preview, tags}]
    """
    ns = _get_ns(namespace)
    results = []
    query_lower = query.lower()
    for key, entry in ns.items():
        value = entry["value"]
        tags = entry.get("tags", [])
        if (
            query_lower in key.lower()
            or query_lower in value.lower()
            or any(query_lower in t.lower() for t in tags)
        ):
            results.append({
                "key": key,
                "value_preview": value[:200],
                "tags": tags,
            })
    return results


@mcp.tool()
def memory_namespaces() -> list[str]:
    """전체 네임스페이스 목록 조회.

    Returns:
        네임스페이스 목록
    """
    return sorted(_store.keys())


@mcp.tool()
def memory_clear(namespace: str = "default") -> str:
    """네임스페이스 전체 초기화.

    Args:
        namespace: 초기화할 네임스페이스
    Returns:
        완료 메시지 (삭제 건수 포함)
    """
    ns = _get_ns(namespace)
    count = len(ns)
    _store[namespace] = {}
    return f"초기화 완료: {namespace} ({count}건 삭제)"


if __name__ == "__main__":
    mcp.run(transport="sse")
