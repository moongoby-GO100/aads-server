"""
Filesystem MCP 서버 — 포트 8765.
도구: read_file, write_file, list_directory, create_directory, delete_file
sandboxed_root 디렉터리(/tmp/aads_workspace) 내에서만 동작 (경로 이탈 차단).
"""
import os
import shutil
from pathlib import Path
from mcp.server.fastmcp import FastMCP

SANDBOXED_ROOT = Path(os.getenv("MCP_FS_ROOT", "/tmp/aads_workspace"))
SANDBOXED_ROOT.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    "aads-filesystem",
    host="0.0.0.0",
    port=8765,
)


def _safe_path(relative: str) -> Path:
    """경로 이탈 방지: sandboxed root 내로 제한."""
    target = (SANDBOXED_ROOT / relative).resolve()
    if not str(target).startswith(str(SANDBOXED_ROOT.resolve())):
        raise ValueError(f"경로 이탈 차단: {relative!r}")
    return target


@mcp.tool()
def read_file(path: str) -> str:
    """파일 내용 읽기.

    Args:
        path: sandboxed root 기준 상대 경로
    Returns:
        파일 텍스트 내용
    """
    target = _safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"파일 없음: {path}")
    return target.read_text(encoding="utf-8")


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """파일 쓰기 (생성 또는 덮어쓰기).

    Args:
        path: sandboxed root 기준 상대 경로
        content: 파일 내용
    Returns:
        완료 메시지
    """
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"파일 저장 완료: {path} ({len(content)} bytes)"


@mcp.tool()
def list_directory(path: str = ".") -> list[str]:
    """디렉터리 내 파일·폴더 목록.

    Args:
        path: sandboxed root 기준 상대 경로 (기본: 루트)
    Returns:
        항목 이름 목록
    """
    target = _safe_path(path)
    if not target.is_dir():
        raise NotADirectoryError(f"디렉터리 없음: {path}")
    entries = []
    for entry in sorted(target.iterdir()):
        suffix = "/" if entry.is_dir() else ""
        entries.append(entry.name + suffix)
    return entries


@mcp.tool()
def create_directory(path: str) -> str:
    """디렉터리 생성 (부모 포함).

    Args:
        path: sandboxed root 기준 상대 경로
    Returns:
        완료 메시지
    """
    target = _safe_path(path)
    target.mkdir(parents=True, exist_ok=True)
    return f"디렉터리 생성 완료: {path}"


@mcp.tool()
def delete_file(path: str) -> str:
    """파일 또는 디렉터리 삭제.

    Args:
        path: sandboxed root 기준 상대 경로
    Returns:
        완료 메시지
    """
    target = _safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"대상 없음: {path}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return f"삭제 완료: {path}"


@mcp.tool()
def file_info(path: str) -> dict:
    """파일 메타정보 조회.

    Args:
        path: sandboxed root 기준 상대 경로
    Returns:
        size, is_file, is_dir, modified_at 딕셔너리
    """
    target = _safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"파일/디렉터리 없음: {path}")
    stat = target.stat()
    return {
        "path": path,
        "size": stat.st_size,
        "is_file": target.is_file(),
        "is_dir": target.is_dir(),
        "modified_at": stat.st_mtime,
    }


if __name__ == "__main__":
    mcp.run(transport="sse")
