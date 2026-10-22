#!/usr/bin/env python3
"""AAG 스캔 소스를 origin/<branch> 에 고정된 전용 clean 클론으로 맞춘다.

왜 있는가. 2026-10-22 AADS 스캔이 96회 연속 source_behind 로 거부됐다. 스캐너가
공유 호스트 체크아웃(/root/aads/aads-server)을 읽었는데, 그 체크아웃은 origin/main
보다 442 커밋 뒤처졌고 타 세션의 staged 변경이 얹혀 있었다. 스캔 소스를 "누군가
작업 중인 곳"에 두는 한 resolved head 와 기대 head 는 영영 맞지 않는다.

이 스크립트가 만지는 것은 --dest 하나뿐이다. 호스트 체크아웃은 origin URL 을
읽기 전용(`git config --get`)으로 한 번 조회할 뿐 pull/reset/checkout/fetch 하지 않는다.

동작
  1. dest 가 없으면 독립 클론(공유 object 없음)을 만든다.
  2. 있으면 ① dest 가 자기 소유 저장소인지 ② origin 이 같은지 ③ 워킹트리가 깨끗한지
     확인한다. 하나라도 어긋나면 거부한다 — 스캔 소스가 오염된 채 조용히 도는 것보다
     멈추는 편이 낫다.
  3. origin/<branch> 만 fetch 해서 detached 로 그 커밋에 맞춘다.

종료코드: 0=맞춤 완료 / 1=오류(clone·fetch 실패 등) / 3=거부(dirty·origin 불일치 등) /
7=SKIP(다른 실행이 lock 보유).
표준출력 마지막 줄: `AAG_SOURCE root=… head=… expected=… state=cloned|updated|current`
거부 시: `AAG_SOURCE_REJECTED reason=… root=…`
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REJECTED = 3
EXIT_SKIPPED = 7

FETCH_TIMEOUT_SEC = 300
CLONE_TIMEOUT_SEC = 900
LOCAL_TIMEOUT_SEC = 60

GitResult = Tuple[int, str, str]
GitRunner = Callable[[Sequence[str], Optional[Path], int], GitResult]


class SourceRejected(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class SourceError(Exception):
    pass


def mask_url(text: str) -> str:
    return re.sub(r"(//)[^/@\s]+@", r"\1***@", text)


def run_git(args: Sequence[str], cwd: Optional[Path], timeout: int) -> GitResult:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    cmd = ["git"] + (["-C", str(cwd)] if cwd else []) + list(args)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", "%s: %s" % (type(exc).__name__, exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def read_origin_url(repo: Path, git: GitRunner = run_git) -> str:
    """읽기 전용 조회. 대상 저장소의 어떤 상태도 바꾸지 않는다."""
    rc, out, _ = git(["config", "--get", "remote.origin.url"], repo, LOCAL_TIMEOUT_SEC)
    return out if rc == 0 else ""


def prepare_source(
    dest: Path, origin_url: str, branch: str = "main", git: GitRunner = run_git,
    clone_filter: str = "",
) -> dict:
    dest = Path(dest)
    remote_ref = "refs/remotes/origin/%s" % branch
    state = "current"

    if not (dest / ".git").exists():
        if dest.exists() and any(dest.iterdir()):
            raise SourceRejected("dest_not_empty", "dest 가 비어 있지 않은 비-저장소 디렉터리다")
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.parent / (dest.name + ".cloning")
        shutil.rmtree(str(partial), ignore_errors=True)
        rc, _, err = git(
            ["clone", "--quiet", "--no-checkout", "--single-branch", "--branch", branch]
            + (["--filter=%s" % clone_filter] if clone_filter else [])
            + ["--", origin_url, str(partial)],
            None, CLONE_TIMEOUT_SEC,
        )
        if rc != 0:
            shutil.rmtree(str(partial), ignore_errors=True)
            raise SourceError("clone_failed: %s" % mask_url(err[-300:]))
        os.replace(str(partial), str(dest))
        state = "cloned"
    else:
        rc, top, _ = git(["rev-parse", "--show-toplevel"], dest, LOCAL_TIMEOUT_SEC)
        if rc != 0 or Path(top).resolve() != dest.resolve():
            raise SourceRejected("not_own_repo", "dest 가 독립 저장소의 최상위가 아니다")
        if read_origin_url(dest, git) != origin_url:
            raise SourceRejected("origin_mismatch", "dest 의 origin 이 기대한 저장소와 다르다")
        rc, status, err = git(
            ["status", "--porcelain=v1", "--untracked-files=all"], dest, LOCAL_TIMEOUT_SEC
        )
        if rc != 0:
            raise SourceError("status_failed: %s" % err[-300:])
        if status:
            first = status.splitlines()[0]
            raise SourceRejected(
                "dirty_source",
                "전용 스캔 소스에 변경이 있다(%d줄, 예: %s). 사람이 만든 변경이면 보존하고 "
                "확인한 뒤 dest 를 치워 재생성하라." % (len(status.splitlines()), first),
            )

    rc, _, err = git(
        ["fetch", "--quiet", "--prune", "--no-tags", "origin",
         "+refs/heads/%s:%s" % (branch, remote_ref)],
        dest, FETCH_TIMEOUT_SEC,
    )
    if rc != 0:
        raise SourceError("fetch_failed: %s" % mask_url(err[-300:]))

    rc, expected, err = git(["rev-parse", "--verify", remote_ref + "^{commit}"], dest, LOCAL_TIMEOUT_SEC)
    if rc != 0 or not expected:
        raise SourceError("expected_head_unresolved: %s" % err[-200:])
    rc, head, _ = git(["rev-parse", "--verify", "HEAD^{commit}"], dest, LOCAL_TIMEOUT_SEC)
    head = head if rc == 0 else ""
    attached = git(["symbolic-ref", "-q", "HEAD"], dest, LOCAL_TIMEOUT_SEC)[0] == 0

    if state == "cloned" or head != expected or attached:
        rc, _, err = git(["switch", "--quiet", "--detach", expected], dest, LOCAL_TIMEOUT_SEC)
        if rc != 0:
            raise SourceError("switch_failed: %s" % err[-300:])
        if state != "cloned":
            state = "updated"

    rc, head, _ = git(["rev-parse", "--verify", "HEAD^{commit}"], dest, LOCAL_TIMEOUT_SEC)
    if rc != 0 or head != expected:
        raise SourceError("head_mismatch_after_switch: %s != %s" % (head, expected))
    rc, status, _ = git(["status", "--porcelain=v1", "--untracked-files=all"], dest, LOCAL_TIMEOUT_SEC)
    if rc != 0 or status:
        raise SourceRejected("dirty_after_switch", "맞춘 직후에도 워킹트리가 깨끗하지 않다")
    return {"root": str(dest), "head": head, "expected": expected, "state": state}


def main(argv: Optional[Sequence[str]] = None, git: GitRunner = run_git) -> int:
    ap = argparse.ArgumentParser(description="AAG 스캔용 origin 전용 clean 클론 준비")
    ap.add_argument("--dest", required=True, help="전용 클론 경로 (호스트 체크아웃이 아니어야 한다)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--origin-url", help="origin URL")
    src.add_argument("--origin-from", help="origin URL 을 읽기 전용으로 조회할 기존 저장소")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--clone-filter", default="",
                    help="최초 clone 에만 적용하는 partial clone 필터(예: blob:none). "
                         "호스트 .git 이 9.5GB 라 전체 이력 사본을 피하려는 용도")
    args = ap.parse_args(argv)

    dest = Path(args.dest)
    origin_url = args.origin_url or read_origin_url(Path(args.origin_from), git)
    if not origin_url:
        print("ERROR: origin URL 을 알 수 없다 (--origin-from=%s)" % args.origin_from, file=sys.stderr)
        return EXIT_ERROR
    if args.origin_from and dest.resolve() == Path(args.origin_from).resolve():
        print("AAG_SOURCE_REJECTED reason=dest_is_origin_source root=%s" % dest)
        return EXIT_REJECTED

    dest.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(str(dest.parent / (dest.name + ".lock")), "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("AAG_SOURCE_SKIPPED reason=already_running root=%s" % dest)
        return EXIT_SKIPPED
    try:
        info = prepare_source(dest, origin_url, args.branch, git, args.clone_filter)
    except SourceRejected as exc:
        print("AAG_SOURCE_REJECTED reason=%s root=%s" % (exc.reason, dest))
        print("detail: %s" % mask_url(exc.detail), file=sys.stderr)
        return EXIT_REJECTED
    except SourceError as exc:
        print("AAG_SOURCE_ERROR root=%s" % dest)
        print("detail: %s" % mask_url(str(exc)), file=sys.stderr)
        return EXIT_ERROR
    finally:
        lock_fh.close()
    print("AAG_SOURCE root=%(root)s head=%(head)s expected=%(expected)s state=%(state)s" % info)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
