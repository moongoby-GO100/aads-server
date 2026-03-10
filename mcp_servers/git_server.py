"""
Git MCP 서버 — 포트 8766.
도구: git_log, git_status, git_diff, git_show, git_branches, git_file_history
      + git_add, git_commit, git_push, git_create_branch (AADS-190 쓰기 도구)
허용된 repo 경로만 접근. force push 차단.
"""
import os
import subprocess
from pathlib import Path
from mcp.server.fastmcp import FastMCP

ALLOWED_REPO_ROOT = Path(os.getenv("MCP_GIT_ROOT", "/tmp/aads_workspace"))
ALLOWED_REPO_ROOT.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    "aads-git",
    host="0.0.0.0",
    port=8766,
)


def _run_git(cmd: list[str], cwd: str | None = None) -> str:
    """git 명령 실행. 실패 시 stderr 포함 예외."""
    repo_path = Path(cwd) if cwd else ALLOWED_REPO_ROOT
    # 허용된 루트 하위 경로만 허용
    if not str(repo_path.resolve()).startswith(str(ALLOWED_REPO_ROOT.resolve())):
        raise ValueError(f"허용되지 않은 경로: {cwd!r}")
    try:
        result = subprocess.run(
            ["git"] + cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return f"[git 오류 {result.returncode}]\n{result.stderr.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "[git 타임아웃: 30초 초과]"
    except Exception as e:
        return f"[git 실행 실패: {e}]"


@mcp.tool()
def git_log(repo_path: str = ".", max_count: int = 10, oneline: bool = True) -> str:
    """git 커밋 로그 조회.

    Args:
        repo_path: 저장소 경로 (ALLOWED_REPO_ROOT 기준 상대 경로)
        max_count: 최대 커밋 수 (기본 10)
        oneline: True이면 한 줄 형식
    Returns:
        커밋 로그 텍스트
    """
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    flags = [f"--max-count={max_count}"]
    if oneline:
        flags.append("--oneline")
    return _run_git(["log"] + flags, cwd=cwd)


@mcp.tool()
def git_status(repo_path: str = ".") -> str:
    """git 작업 트리 상태 조회.

    Args:
        repo_path: 저장소 경로
    Returns:
        git status 출력
    """
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    return _run_git(["status", "--short"], cwd=cwd)


@mcp.tool()
def git_diff(repo_path: str = ".", target: str = "HEAD") -> str:
    """git diff 조회.

    Args:
        repo_path: 저장소 경로
        target: diff 대상 (기본: HEAD)
    Returns:
        diff 텍스트
    """
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    output = _run_git(["diff", target], cwd=cwd)
    # 너무 길면 앞 4000자만
    return output[:4000] + ("...(truncated)" if len(output) > 4000 else "")


@mcp.tool()
def git_show(repo_path: str = ".", commit: str = "HEAD") -> str:
    """특정 커밋 상세 정보.

    Args:
        repo_path: 저장소 경로
        commit: 커밋 해시 또는 레퍼런스 (기본: HEAD)
    Returns:
        커밋 상세 정보
    """
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    output = _run_git(["show", "--stat", commit], cwd=cwd)
    return output[:3000] + ("...(truncated)" if len(output) > 3000 else "")


@mcp.tool()
def git_branches(repo_path: str = ".") -> list[str]:
    """브랜치 목록 조회.

    Args:
        repo_path: 저장소 경로
    Returns:
        브랜치 이름 목록
    """
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    output = _run_git(["branch", "-a"], cwd=cwd)
    if output.startswith("[git"):
        return []
    return [b.strip().lstrip("* ") for b in output.splitlines() if b.strip()]


@mcp.tool()
def git_file_history(repo_path: str = ".", file_path: str = "", max_count: int = 5) -> str:
    """특정 파일의 커밋 이력 조회.

    Args:
        repo_path: 저장소 경로
        file_path: 조회할 파일 경로
        max_count: 최대 커밋 수
    Returns:
        파일 커밋 이력
    """
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    cmd = ["log", f"--max-count={max_count}", "--oneline"]
    if file_path:
        cmd += ["--", file_path]
    return _run_git(cmd, cwd=cwd)


# ─── AADS-190: Git 쓰기 도구 ─────────────────────────────────────────────────


@mcp.tool()
def git_add(repo_path: str = ".", files: str = ".") -> str:
    """git add — 파일 스테이징.

    Args:
        repo_path: 저장소 경로 (ALLOWED_REPO_ROOT 기준 상대 경로)
        files: 추가할 파일 (기본: 전체). 공백으로 여러 파일 구분
    Returns:
        완료 메시지
    """
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    file_list = files.split() if files.strip() != "." else ["."]
    return _run_git(["add"] + file_list, cwd=cwd)


@mcp.tool()
def git_commit(repo_path: str = ".", message: str = "") -> str:
    """git commit — 커밋 생성.

    Args:
        repo_path: 저장소 경로
        message: 커밋 메시지 (필수)
    Returns:
        커밋 결과
    """
    if not message or not message.strip():
        return "[git 오류] 커밋 메시지 필수"
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    return _run_git(["commit", "-m", message], cwd=cwd)


@mcp.tool()
def git_push(repo_path: str = ".", remote: str = "origin", branch: str = "") -> str:
    """git push — 원격 저장소에 푸시 (force push 차단).

    Args:
        repo_path: 저장소 경로
        remote: 원격 이름 (기본: origin)
        branch: 브랜치명 (비어있으면 현재 브랜치)
    Returns:
        푸시 결과
    """
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    cmd = ["push", remote]
    if branch:
        cmd.append(branch)
    return _run_git(cmd, cwd=cwd)


@mcp.tool()
def git_create_branch(repo_path: str = ".", branch_name: str = "") -> str:
    """새 브랜치 생성 및 체크아웃.

    Args:
        repo_path: 저장소 경로
        branch_name: 새 브랜치 이름 (필수)
    Returns:
        생성 결과
    """
    if not branch_name or not branch_name.strip():
        return "[git 오류] 브랜치 이름 필수"
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    return _run_git(["checkout", "-b", branch_name], cwd=cwd)


@mcp.tool()
def git_checkout(repo_path: str = ".", branch: str = "") -> str:
    """기존 브랜치로 체크아웃.

    Args:
        repo_path: 저장소 경로
        branch: 체크아웃할 브랜치명 (필수)
    Returns:
        체크아웃 결과
    """
    if not branch or not branch.strip():
        return "[git 오류] 브랜치 이름 필수"
    cwd = str(ALLOWED_REPO_ROOT / repo_path)
    return _run_git(["checkout", branch], cwd=cwd)


if __name__ == "__main__":
    mcp.run(transport="sse")
