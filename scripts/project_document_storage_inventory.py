#!/usr/bin/env python3
"""All-server document storage inventory. Read-only: no DB connection, no remote write.

collect  - run ON a server (stdlib only, python>=3.8). Emits JSONL: hashes and metadata, never bodies.
merge    - join raw JSONL with the committed goal_documents verdict CSV -> inventory CSV + summary.
run      - collect from every server (local, or over ssh with this script piped to `python3 -`) + merge.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DOC_EXTS = (".md", ".html")
MAX_SCAN_BYTES = 2 * 1024 * 1024
TITLE_BYTES = 8192
GIT_TIMEOUT = 60
WORKERS = 8
BATCH = 256
DISCOVERY_BASES = ("/root", "/srv", "/data", "/opt", "/home")
DISCOVERY_MAX_DEPTH = 4
DISCOVERY_NAMES = frozenset({"docs", "reports", "report", "prd", "plan", "plans"})
EXCLUDE_DIRS = frozenset({"node_modules", ".git", ".venv", "venv", ".venvs", "__pycache__", "site-packages"})
MAIN_SERVER = "contabo116"
MAIN_REPO = "aads-server"

# Same pattern as app/api/canonical_documents.py SECRET (kept in sync by a unit test).
SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16})|"
    r"(?i:(?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|secret[_-]?key|private[_-]?key|database[_-]?url)['\"]?\s*[:=]\s*['\"]?[^\s'\"]+)"
)

KNOWN_ROOTS = {
    "contabo116": [
        ("aads-server/docs", "/root/aads/aads-server/docs"),
        ("aads-server/reports", "/root/aads/aads-server/reports"),
        ("aads-server/app-static-reports", "/root/aads/aads-server/app/static/reports"),
        ("aads-dashboard/docs", "/root/aads/aads-dashboard/docs"),
        ("aads-docs", "/root/aads/aads-docs"),
        ("_remote_docs", "/root/aads/_remote_docs"),
    ],
    "contabo14": [
        ("kis-autotrade-v4/docs", "/root/kis-autotrade-v4/docs"),
        ("kis-autotrade-v4/reports", "/root/kis-autotrade-v4/reports"),
    ],
    "cafe24_114": [
        ("go100/docs", "/root/go100/docs"),
        ("newtalk-v2-srv/docs", "/srv/newtalk-v2/docs"),
        ("newtalk-v2-api-repo/docs", "/root/newtalk-v2-api-repo/docs"),
        ("newtalk-v2/docs", "/root/newtalk-v2/docs"),
        ("shortflow/docs", "/data/shortflow/docs"),
        ("nas-image-auto/docs", "/root/nas-image-auto/docs"),
        ("server116/docs", "/root/server116/docs"),
        ("blog-automation/docs", "/root/blog-automation/docs"),
        ("data-project-docs", "/data/project-docs"),
        ("project-docs-repo", "/root/project-docs-repo"),
    ],
}
SSH_TARGETS = {"contabo14": "contabo14", "cafe24_114": "server-114", "jinah244": "jinah244"}
DEFAULT_SERVERS = ("contabo116", "contabo14", "cafe24_114", "jinah244")
DEFAULT_UNCOLLECTED = {}

KIND_RULES = (
    ("prd", re.compile(r"(?<![a-z])prd(?![a-z])|product requirements?|요구\s*사항")),
    ("contract", re.compile(r"(?<![a-z])contracts?(?![a-z])|계약|규약|협약")),
    ("spec", re.compile(r"(?<![a-z])spec(?:s|ification|ifications)?(?![a-z])|명세|사양|스펙")),
    ("design", re.compile(
        r"(?<![a-z])(?:design|designs|architecture|adr|layout|lay out|blueprint)(?![a-z])|설계|아키텍처|디자인")),
    ("plan", re.compile(r"(?<![a-z])(?:plan|plans|planning|roadmap|proposal)(?![a-z])|기획|계획|로드맵|제안")),
)

CSV_COLUMNS = (
    "server", "root_alias", "root_kind", "rel_path", "path_sha256", "ext", "size_bytes", "mtime_utc",
    "sha256", "read_status", "git_repo", "git_state", "git_head", "kind_estimate", "kind_basis",
    "kind_is_estimate", "planning_design_prd_estimate", "secret_scan", "goal_documents_registered",
    "registered_basis", "registered_goal_doc_ids", "copy_of_original", "snapshot_duplicate", "copy_of",
    "sha256_group_size",
)
COPY_COLUMNS = (
    "server", "root_alias", "root_kind", "copy_kind", "copy_of", "files", "size_bytes_total",
    "goal_documents_registered_files",
)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def path_sha256(abs_path):
    """Same definition as the verdict CSV: sha256 of the path string."""
    return hashlib.sha256(os.fsencode(abs_path)).hexdigest()


def normalize_doc_path(raw):
    """Canonical form of a doc_path for comparison; None when it is not a usable file path."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("file://"):
        text = text[len("file://"):]
    if not text or "\x00" in text or "://" in text:
        return None
    text = text.replace("\\", "/")
    text = re.sub(r"^/{2,}", "/", text)
    text = posixpath.normpath(text)
    if text in (".", "") or text == ".." or text.startswith("../"):
        return None
    return text


def extract_title(text, ext):
    head = text[:TITLE_BYTES]
    if ext == ".html":
        match = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
        if not match:
            match = re.search(r"<h1[^>]*>(.*?)</h1>", head, re.I | re.S)
        return re.sub(r"<[^>]+>", "", match.group(1)).strip()[:200] if match else ""
    lines = head.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.lower().startswith("title:"):
                return line.split(":", 1)[1].strip().strip("'\"")[:200]
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return ""


def _kind_of(text):
    norm = re.sub(r"[^0-9a-z가-힣]+", " ", text.lower())
    for kind, pattern in KIND_RULES:
        if pattern.search(norm):
            return kind
    return None


def estimate_kind(rel_path, title=""):
    """(kind, basis). Always an estimate: filename first, then title, then directory names."""
    parts = [p for p in str(rel_path).split("/") if p]
    if not parts:
        return "other", "none"
    stem = parts[-1].rsplit(".", 1)[0]
    for basis, text in (("filename", stem), ("title", title or ""), ("dirname", " ".join(parts[:-1]))):
        kind = _kind_of(text) if text else None
        if kind:
            return kind, basis
    return "other", "none"


def classify_root_kind(abs_root):
    """original | release | worktree | mirror | backup, from path components only."""
    parts = Path(abs_root).parts
    lows = [part.lower() for part in parts]
    if any(re.search(r"(?:^|[-_])releases?(?:[-_]|$)", low) for low in lows):
        return "release"
    for low in lows:
        if re.match(r"^wt-", low) or re.match(r"^go100-.+", low) or "worktree" in low:
            return "worktree"
    if "_remote_docs" in parts:
        return "mirror"
    for low in lows:
        if (re.match(r"^_?(?:backups?|archive)(?:_.*)?$", low) or "dirty_backup" in low
                or re.search(r"(?:^|[-_.])bak(?:[-_.]|$)", low)):
            return "backup"
    return "original"


def find_git_top(path):
    """(toplevel, is_worktree) for the nearest ancestor with .git; a .git FILE means a git worktree."""
    current = os.path.abspath(path)
    while True:
        if os.path.dirname(current) == current:
            # A stray /.git (seen on jinah244) is not the document's repository; it would mark every file untracked.
            return None, False
        dot_git = os.path.join(current, ".git")
        if os.path.isdir(dot_git) and os.path.exists(os.path.join(dot_git, "HEAD")):
            return current, False
        if os.path.isfile(dot_git):
            try:
                with open(dot_git, "rb") as stream:
                    if stream.read(64).startswith(b"gitdir:"):
                        return current, True
            except OSError:
                pass
        current = os.path.dirname(current)


def _git(top, args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    proc = subprocess.run(["git", "-c", "safe.directory=*", "-C", top] + args, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=GIT_TIMEOUT, env=env)
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = (lines[0] if lines else "").replace(top, "<repo>")[:120]
        raise RuntimeError("git %s exit %d: %s" % (args[0], proc.returncode, detail))
    return proc.stdout


def _nul_set(raw):
    return set(os.fsdecode(item) for item in raw.split(b"\x00") if item)


def load_git(top, root):
    """Read-only git facts for files under root: (info, "") or (None, reason)."""
    prefix = os.path.relpath(root, top)
    try:
        head = _git(top, ["rev-parse", "--short=8", "HEAD"]).decode().strip()
        committed = _nul_set(_git(top, ["ls-tree", "-r", "-z", "--name-only", "HEAD", "--", prefix]))
        index = _nul_set(_git(top, ["ls-files", "-z", "--", prefix]))
        changed = _nul_set(_git(top, ["diff", "--no-ext-diff", "--name-only", "-z", "HEAD", "--", prefix]))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return None, str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    return {"head": head, "committed": committed, "index": index, "changed": changed}, ""


def git_state_of(info, top, file_path):
    if top is None:
        return "no_git"
    if info is None:
        return "git_error"
    key = os.path.relpath(file_path, top).replace(os.sep, "/")
    if key in info["committed"]:
        return "committed_modified" if key in info["changed"] else "committed_clean"
    return "staged_uncommitted" if key in info["index"] else "untracked"


def discover_roots(bases, deadline, max_depth=DISCOVERY_MAX_DEPTH, names=DISCOVERY_NAMES):
    """Directories named docs/reports/prd/plan within max_depth of each base. Returns (paths, hit_deadline)."""
    found = []
    hit = False
    for base in bases:
        if not os.path.isdir(base) or os.path.islink(base):
            continue
        base = base.rstrip("/") or "/"
        for dirpath, dirnames, _files in os.walk(base):
            if time.monotonic() > deadline:
                hit = True
                break
            depth = 0 if dirpath == base else dirpath[len(base):].count("/")
            keep = []
            for name in sorted(dirnames):
                if name in EXCLUDE_DIRS or name.startswith("."):
                    continue
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                if name.lower() in names and depth + 1 <= max_depth:
                    found.append(full)
                elif depth + 1 < max_depth:
                    keep.append(name)
            dirnames[:] = keep
        if hit:
            break
    return sorted(set(found)), hit


def resolve_roots(known, discovered):
    """[(alias, path, origin)]. Known roots win; a discovered root inside any other root is dropped."""
    roots = [(alias, os.path.normpath(path), "known") for alias, path in known]
    taken = set(path for _a, path, _o in roots)
    for path in discovered:
        path = os.path.normpath(path)
        if path in taken:
            continue
        if any(path.startswith(other.rstrip("/") + "/") for other in taken):
            continue
        roots.append(("found:" + path.lstrip("/"), path, "discovered"))
        taken.add(path)
    return roots[:len(known)] + sorted(roots[len(known):], key=lambda item: item[1])


def read_document(path):
    """(size, mtime, sha256, text_or_None, status). text is None when not scanned."""
    try:
        with open(path, "rb") as stream:
            st = os.fstat(stream.fileno())
            head = stream.read(MAX_SCAN_BYTES + 1)
            if len(head) <= MAX_SCAN_BYTES:
                return len(head), st.st_mtime, hashlib.sha256(head).hexdigest(), head.decode("utf-8", "replace"), "ok"
            digest = hashlib.sha256(head)
            total = len(head)
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            return total, st.st_mtime, digest.hexdigest(), head[:TITLE_BYTES].decode("utf-8", "replace"), "ok_unscanned"
    except OSError:
        return None, None, None, None, "unreadable"


def _utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts is not None else ""


def walk_root(server, alias, root, skip_dirs, deadline, emit, pool=None):
    kind = classify_root_kind(root)
    top, is_worktree = find_git_top(root)
    if is_worktree and kind == "original":
        kind = "worktree"
    info, git_reason = load_git(top, root) if top else (None, "")
    stats = {"t": "root", "server": server, "root_alias": alias, "root_kind": kind, "status": "ok",
             "counted": 0, "skipped_symlink": 0, "unreadable": 0, "walk_errors": 0,
             "git_repo": os.path.basename(top) if top else "", "git_head": info["head"] if info else "",
             "git_worktree": bool(is_worktree), "git_error": git_reason}

    def process(item):
        full, rel, ext = item
        size, mtime, digest, text, status = read_document(full)
        title = extract_title(text, ext) if text else ""
        if status == "unreadable":
            secret_scan = "unreadable"
        elif status == "ok_unscanned":
            secret_scan = "skipped_too_large"
        else:
            secret_scan = "clean"
        if (text and SECRET.search(text)) or SECRET.search(rel):
            secret_scan = "hit"
        est_kind, basis = estimate_kind(rel, title)
        repo_rel = os.path.relpath(full, top).replace(os.sep, "/") if top else rel
        norm = normalize_doc_path(repo_rel) or repo_rel
        return {
            "t": "file", "server": server, "root_alias": alias, "root_kind": kind,
            "rel_path": "" if secret_scan == "hit" else rel, "path_sha256": path_sha256(full),
            "repo_rel_sha256": [sha256_text(norm), sha256_text("./" + norm)],
            "ext": ext, "size": size, "mtime": _utc(mtime), "sha256": digest or "",
            "read_status": status, "git_state": git_state_of(info, top, full),
            "kind": est_kind, "kind_basis": basis, "secret_scan": secret_scan,
        }

    pending = []

    def flush():
        batch = list(pending)
        del pending[:]
        for rec in (pool.map(process, batch) if pool else map(process, batch)):
            if rec["read_status"] == "unreadable":
                stats["unreadable"] += 1
            emit(rec)
            stats["counted"] += 1

    errors = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=errors.append):
        if time.monotonic() > deadline:
            stats["status"] = "deadline"
            break
        keep = []
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                stats["skipped_symlink"] += 1
            elif name not in EXCLUDE_DIRS and full not in skip_dirs:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext not in DOC_EXTS:
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                stats["skipped_symlink"] += 1
                continue
            pending.append((full, os.path.relpath(full, root).replace(os.sep, "/"), ext))
        if len(pending) >= BATCH:
            flush()
    flush()
    stats["walk_errors"] = len(errors)
    return stats


def collect(server, emit, deadline_seconds=600, known=None, bases=DISCOVERY_BASES):
    start = time.monotonic()
    deadline = start + deadline_seconds
    emit({"t": "meta", "server": server, "hostname": socket.gethostname(),
          "python": sys.version.split()[0], "collected_at": _utc(time.time())})
    known = KNOWN_ROOTS.get(server, []) if known is None else known
    discovered, disc_hit = discover_roots(bases, min(deadline, start + 120))
    roots = resolve_roots(known, discovered)
    all_paths = set(path for _a, path, _o in roots)
    seen_real = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        _collect_roots(server, roots, all_paths, seen_real, deadline, emit, pool)
    emit({"t": "end", "server": server, "discovery_deadline": disc_hit,
          "elapsed_seconds": round(time.monotonic() - start, 1)})


def _collect_roots(server, roots, all_paths, seen_real, deadline, emit, pool):
    for alias, path, origin in roots:
        if time.monotonic() > deadline:
            emit({"t": "root", "server": server, "root_alias": alias, "root_kind": classify_root_kind(path),
                  "status": "deadline", "counted": 0})
            continue
        if not os.path.isdir(path):
            emit({"t": "root", "server": server, "root_alias": alias, "root_kind": classify_root_kind(path),
                  "status": "missing", "counted": 0})
            continue
        real = os.path.realpath(path)
        if real in seen_real:
            emit({"t": "root", "server": server, "root_alias": alias, "root_kind": classify_root_kind(path),
                  "status": "alias_of:" + seen_real[real], "counted": 0})
            continue
        seen_real[real] = alias
        stats = walk_root(server, alias, path, all_paths - {path}, deadline, emit, pool)
        stats["origin"] = origin
        emit(stats)


def _emit_stdout(record):
    sys.stdout.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")


def fetch_server(server, deadline_seconds=600):
    """Raw records for one server: in-process for MAIN_SERVER, ssh + stdin script for the rest."""
    if server == MAIN_SERVER:
        records = []
        collect(server, records.append, deadline_seconds)
        return records
    target = SSH_TARGETS.get(server)
    if not target:
        return [{"t": "server", "server": server, "status": "not_collected", "reason": "no ssh target configured"}]
    script = Path(__file__).read_bytes()
    command = "timeout %d python3 - collect --server %s --deadline-seconds %d" % (
        deadline_seconds + 300, server, deadline_seconds)
    try:
        proc = subprocess.run(
            ["ssh", "-C", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
             target, command], input=script, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=deadline_seconds + 400)
    except (OSError, subprocess.SubprocessError) as exc:
        return [{"t": "server", "server": server, "status": "fetch_failed", "reason": type(exc).__name__}]
    records = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    if proc.returncode != 0 or not any(r.get("t") == "end" for r in records):
        reason = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        records.append({"t": "server", "server": server, "status": "fetch_failed",
                        "reason": "exit %d: %s" % (proc.returncode, (reason[-1] if reason else "")[:200])})
    return records


def load_goal_index(csv_path):
    """(ids_by_path_hash, total_rows) from the committed verdict CSV. No DB access."""
    index = defaultdict(list)
    total = 0
    with open(csv_path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            total += 1
            ident = row["id"]
            index[row["path_sha256"]].append(ident)
            public = normalize_doc_path(row.get("public_path") or "")
            if public:
                hashed = sha256_text(public)
                if ident not in index[hashed]:
                    index[hashed].append(ident)
    return index, total


def registration_of(rec, goal_index):
    """(basis, [goal_document ids]). Relative doc_paths only count for the live aads-server repo."""
    ids = goal_index.get(rec["path_sha256"])
    if ids:
        return "absolute_path", list(ids)
    if rec["server"] == MAIN_SERVER and rec.get("root_kind") == "original" and rec.get("git_repo") == MAIN_REPO:
        for digest in rec.get("repo_rel_sha256", []):
            if goal_index.get(digest):
                return "relative_path", list(goal_index[digest])
    return "none", []


def _clean(text):
    return str(text).encode("utf-8", "replace").decode("utf-8")


def build_rows(records, goal_index):
    roots = {}
    for rec in records:
        if rec.get("t") == "root":
            roots[(rec["server"], rec["root_alias"])] = rec
    rows = []
    for rec in records:
        if rec.get("t") != "file":
            continue
        root = roots.get((rec["server"], rec["root_alias"]), {})
        rec = dict(rec, git_repo=root.get("git_repo", ""))
        basis, ids = registration_of(rec, goal_index)
        rows.append({
            "server": rec["server"], "root_alias": rec["root_alias"], "root_kind": rec["root_kind"],
            "rel_path": _clean(rec["rel_path"]), "path_sha256": rec["path_sha256"], "ext": rec["ext"],
            "size_bytes": rec["size"], "mtime_utc": rec["mtime"], "sha256": rec["sha256"],
            "read_status": rec["read_status"], "git_repo": rec["git_repo"], "git_state": rec["git_state"],
            "git_head": root.get("git_head", ""), "kind_estimate": rec["kind"], "kind_basis": rec["kind_basis"],
            "kind_is_estimate": True, "planning_design_prd_estimate": rec["kind"] != "other",
            "secret_scan": rec["secret_scan"], "goal_documents_registered": basis != "none",
            "registered_basis": basis, "registered_goal_doc_ids": ";".join(sorted(ids, key=lambda x: (len(x), x))),
            "copy_of_original": False, "snapshot_duplicate": False, "copy_of": "", "sha256_group_size": 0,
        })
    rows.sort(key=lambda r: (r["server"], r["root_alias"], r["rel_path"], r["path_sha256"]))
    mark_copies(rows)
    return rows


def mark_copies(rows):
    """Copies are non-original rows with non-empty content already held elsewhere.

    copy_of_original: an original root (any server) has the same sha256.
    snapshot_duplicate: no original, but an earlier non-original row in a different root has it
    (release/worktree snapshots of each other). The earlier row stays as the representative.
    """
    groups = Counter(r["sha256"] for r in rows if r["sha256"])
    originals = defaultdict(list)
    for r in rows:
        if r["root_kind"] == "original" and r["sha256"] and r["size_bytes"]:
            originals[r["sha256"]].append((r["server"], r["root_alias"]))
    representative = {}
    for r in rows:
        r["sha256_group_size"] = groups.get(r["sha256"], 0)
        if r["root_kind"] == "original" or not r["sha256"] or not r["size_bytes"]:
            continue
        found = originals.get(r["sha256"])
        if found:
            best = sorted(found, key=lambda item: (item[0] != r["server"], item))[0]
            r["copy_of_original"] = True
            r["copy_of"] = "%s:%s" % best
            continue
        first = representative.setdefault(r["sha256"], (r["server"], r["root_alias"]))
        if first != (r["server"], r["root_alias"]):
            r["snapshot_duplicate"] = True
            r["copy_of"] = "%s:%s" % first


def is_collapsed(row):
    return bool(row["copy_of_original"] or row["snapshot_duplicate"])


def split_rows(rows):
    """(kept rows, aggregated copy rows). kept + sum(files) == len(rows)."""
    kept = []
    groups = {}
    for r in rows:
        if not is_collapsed(r):
            kept.append(r)
            continue
        kind = "of_original" if r["copy_of_original"] else "snapshot_duplicate"
        key = (r["server"], r["root_alias"], r["root_kind"], kind, r["copy_of"])
        entry = groups.setdefault(key, {"server": key[0], "root_alias": key[1], "root_kind": key[2],
                                        "copy_kind": key[3], "copy_of": key[4], "files": 0,
                                        "size_bytes_total": 0, "goal_documents_registered_files": 0})
        entry["files"] += 1
        entry["size_bytes_total"] += r["size_bytes"] or 0
        entry["goal_documents_registered_files"] += int(r["goal_documents_registered"])
    return kept, sorted(groups.values(), key=lambda e: (e["server"], e["root_alias"], e["copy_kind"], e["copy_of"]))


def summarize(records, rows, goal_index, goal_total):
    root_recs = [r for r in records if r.get("t") == "root"]
    per_root = {}
    for r in root_recs:
        per_root[(r["server"], r["root_alias"])] = {
            "server": r["server"], "root_alias": r["root_alias"], "root_kind": r["root_kind"],
            "status": r["status"], "measured": r.get("counted", 0), "files": 0, "planning_design_prd": 0,
            "unregistered": 0, "unregistered_unique": 0, "unregistered_planning_unique": 0, "copies": 0,
            "snapshot_dups": 0, "secret_hits": 0, "skipped_symlink": r.get("skipped_symlink", 0), "unreadable": r.get("unreadable", 0),
        }
    matched = set()
    for row in rows:
        entry = per_root.setdefault((row["server"], row["root_alias"]), {
            "server": row["server"], "root_alias": row["root_alias"], "root_kind": row["root_kind"],
            "status": "no_root_record", "measured": 0, "files": 0, "planning_design_prd": 0,
            "unregistered": 0, "unregistered_unique": 0, "unregistered_planning_unique": 0, "copies": 0,
            "snapshot_dups": 0, "secret_hits": 0, "skipped_symlink": 0, "unreadable": 0,
        })
        entry["files"] += 1
        entry["planning_design_prd"] += int(row["planning_design_prd_estimate"])
        entry["copies"] += int(row["copy_of_original"])
        entry["snapshot_dups"] += int(row["snapshot_duplicate"])
        entry["secret_hits"] += int(row["secret_scan"] == "hit")
        if row["goal_documents_registered"]:
            matched.update(row["registered_goal_doc_ids"].split(";"))
        else:
            entry["unregistered"] += 1
            if not is_collapsed(row):
                entry["unregistered_unique"] += 1
                entry["unregistered_planning_unique"] += int(row["planning_design_prd_estimate"])
    failures = []
    for r in records:
        if r.get("t") == "server":
            failures.append({"server": r["server"], "root_alias": "*", "status": r["status"], "reason": r["reason"]})
        elif r.get("t") == "root":
            if r["status"] != "ok":
                failures.append({"server": r["server"], "root_alias": r["root_alias"], "status": r["status"],
                                 "reason": {"missing": "directory does not exist on the server",
                                            "deadline": "collector deadline reached; counts are partial"}.get(
                                     r["status"], "see status")})
            elif r.get("unreadable"):
                failures.append({"server": r["server"], "root_alias": r["root_alias"], "status": "partial",
                                 "reason": "%d file(s) unreadable (row kept, no hash)" % r["unreadable"]})
            if r.get("git_error"):
                failures.append({"server": r["server"], "root_alias": r["root_alias"], "status": "git_error",
                                 "reason": "git_state=git_error for this root: %s" % r["git_error"]})
        elif r.get("t") == "end" and r.get("discovery_deadline"):
            failures.append({"server": r["server"], "root_alias": "*", "status": "discovery_deadline",
                             "reason": "root discovery stopped at its deadline; later bases not scanned"})
    for e in per_root.values():
        if e["status"] == "no_root_record":
            failures.append({"server": e["server"], "root_alias": e["root_alias"], "status": "no_root_record",
                             "reason": "file rows without a root summary (collection cut off); counts incomplete"})
    measured = sum(e["measured"] for e in per_root.values())
    by_server = defaultdict(lambda: Counter())
    for entry in per_root.values():
        for key in ("files", "planning_design_prd", "unregistered", "unregistered_unique",
                    "unregistered_planning_unique", "copies", "snapshot_dups"):
            by_server[entry["server"]][key] += entry[key]
    return {
        "total_rows": len(rows), "measured_total": measured, "measured_matches_rows": measured == len(rows),
        "goal_documents": {"verdict_rows": goal_total, "matched_distinct_ids": len(matched),
                           "not_found_on_any_server": goal_total - len(matched)},
        "by_root": sorted(per_root.values(), key=lambda e: (e["server"], e["root_alias"])),
        "by_server": {name: dict(counter) for name, counter in sorted(by_server.items())},
        "kind_estimate": dict(sorted(Counter(r["kind_estimate"] for r in rows).items())),
        "registered_basis": dict(sorted(Counter(r["registered_basis"] for r in rows).items())),
        "copy_by_root_kind": dict(sorted(Counter(r["root_kind"] for r in rows if r["copy_of_original"]).items())),
        "snapshot_dup_by_root_kind": dict(sorted(Counter(r["root_kind"] for r in rows if r["snapshot_duplicate"]).items())),
        "non_original_unique_representatives": sum(
            1 for r in rows if r["root_kind"] != "original" and not is_collapsed(r)),
        "secret_hits": sum(1 for r in rows if r["secret_scan"] == "hit"),
        "failures": failures,
    }


def _cell(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text and text[0] in "=+-@" else text


def rows_to_csv(rows, columns=CSV_COLUMNS):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row[name]) for name in columns])
    return buffer.getvalue()


def render_markdown(summary):
    lines = ["| 서버 | 루트 별칭 | 종류 | 상태 | 파일 수 | 기획·설계·PRD 추정 | 미등록 | 미등록(사본 제외) | 사본(원본 존재) | 스냅샷 중복 |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for e in summary["by_root"]:
        lines.append("| %s | `%s` | %s | %s | %d | %d | %d | %d | %d | %d |" % (
            e["server"], e["root_alias"], e["root_kind"], e["status"], e["files"], e["planning_design_prd"],
            e["unregistered"], e["unregistered_unique"], e["copies"], e["snapshot_dups"]))
    lines += ["", "| 서버 | 파일 수 | 기획·설계·PRD 추정 | 미등록 | 미등록(사본 제외) | 미등록 기획·설계·PRD(사본 제외) | 사본(원본 존재) | 스냅샷 중복 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    total = Counter()
    for name, c in summary["by_server"].items():
        lines.append("| %s | %d | %d | %d | %d | %d | %d | %d |" % (
            name, c["files"], c["planning_design_prd"], c["unregistered"], c["unregistered_unique"],
            c["unregistered_planning_unique"], c["copies"], c["snapshot_dups"]))
        total.update(c)
    lines.append("| **합계** | %d | %d | %d | %d | %d | %d | %d |" % (
        total["files"], total["planning_design_prd"], total["unregistered"], total["unregistered_unique"],
        total["unregistered_planning_unique"], total["copies"], total["snapshot_dups"]))
    return "\n".join(lines) + "\n"


def merge_records(raw_paths):
    records = []
    for path in raw_paths:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def merge(records, verdict_csv):
    goal_index, goal_total = load_goal_index(verdict_csv)
    rows = build_rows(records, goal_index)
    return rows, summarize(records, rows, goal_index, goal_total)


def _write_outputs(rows, summary, args):
    kept, copies = (rows, []) if args.detail_copies else split_rows(rows)
    collapsed = sum(entry["files"] for entry in copies)
    summary["csv_rows"] = len(kept)
    summary["collapsed_copy_rows"] = collapsed
    summary["csv_plus_collapsed_matches_measured"] = len(kept) + collapsed == summary["measured_total"]
    Path(args.output).write_text(rows_to_csv(kept), encoding="utf-8")
    if not args.detail_copies:
        copies_path = args.copies_output or str(Path(args.output).with_suffix("")) + "_copies_by_root.csv"
        Path(copies_path).write_text(rows_to_csv(copies, COPY_COLUMNS), encoding="utf-8")
    if args.summary_json:
        Path(args.summary_json).write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if args.summary_md:
        Path(args.summary_md).write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in (
        "total_rows", "measured_total", "measured_matches_rows", "csv_rows", "collapsed_copy_rows",
        "csv_plus_collapsed_matches_measured", "goal_documents", "by_server")},
        ensure_ascii=False, sort_keys=True))
    ok = summary["measured_matches_rows"] and summary["csv_plus_collapsed_matches_measured"]
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--server", required=True)
    p_collect.add_argument("--deadline-seconds", type=int, default=600)
    for name in ("merge", "run"):
        p = sub.add_parser(name)
        p.add_argument("--verdict-csv", required=True)
        p.add_argument("--output", required=True)
        p.add_argument("--summary-json")
        p.add_argument("--summary-md")
        p.add_argument("--copies-output", help="aggregated copy rows (default: <output>_copies_by_root.csv)")
        p.add_argument("--detail-copies", action="store_true",
                       help="write every file as a row instead of collapsing copies per root")
    sub.choices["merge"].add_argument("--raw", nargs="+", required=True)
    p_run = sub.choices["run"]
    p_run.add_argument("--servers", default=",".join(DEFAULT_SERVERS))
    p_run.add_argument("--deadline-seconds", type=int, default=600)
    p_run.add_argument("--raw-dir", help="keep raw JSONL here (default: temp dir, removed afterwards)")
    args = parser.parse_args(argv)
    if args.cmd == "collect":
        collect(args.server, _emit_stdout, args.deadline_seconds)
        sys.stdout.flush()
        return 0
    if args.cmd == "merge":
        records = merge_records(args.raw)
    else:
        raw_dir = Path(args.raw_dir) if args.raw_dir else Path(tempfile.mkdtemp(prefix="aads_storage_inv_"))
        raw_dir.mkdir(parents=True, exist_ok=True)
        records = []
        try:
            for server in [s for s in args.servers.split(",") if s]:
                part = fetch_server(server, args.deadline_seconds)
                (raw_dir / (server + ".jsonl")).write_text(
                    "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in part), encoding="utf-8")
                records.extend(part)
        finally:
            if not args.raw_dir:
                shutil.rmtree(raw_dir, ignore_errors=True)
        for server, reason in DEFAULT_UNCOLLECTED.items():
            records.append({"t": "server", "server": server, "status": "not_collected", "reason": reason})
    rows, summary = merge(records, args.verdict_csv)
    return _write_outputs(rows, summary, args)


if __name__ == "__main__":
    sys.exit(main())
