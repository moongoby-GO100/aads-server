import ast
import csv
import hashlib
import importlib.util
import io
import json
import re
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/project_document_storage_inventory.py"
SPEC = importlib.util.spec_from_file_location("project_document_storage_inventory", SCRIPT)
inv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inv)


def collect_records(server, root, alias="r", **kwargs):
    records = []
    inv.collect(server, records.append, known=[(alias, str(root))], bases=(), **kwargs)
    return records


def files(records):
    return [r for r in records if r["t"] == "file"]


@pytest.mark.parametrize("raw,expected", [
    ("docs/a.md", "docs/a.md"),
    ("./docs/a.md", "docs/a.md"),
    ("docs//sub/../a.md", "docs/a.md"),
    ("  docs/a.md  ", "docs/a.md"),
    ("docs/a.md/", "docs/a.md"),
    ("//root/aads/docs/a.md", "/root/aads/docs/a.md"),
    ("/root/aads/./docs/a.md", "/root/aads/docs/a.md"),
    ("file:///root/aads/docs/a.md", "/root/aads/docs/a.md"),
    ("docs\\a.md", "docs/a.md"),
    ("../outside.md", None),
    ("https://example.com/a.md", None),
    ("", None),
    (".", None),
    (None, None),
    ("a\x00b", None),
])
def test_normalize_doc_path(raw, expected):
    assert inv.normalize_doc_path(raw) == expected


@pytest.mark.parametrize("rel,title,expected", [
    ("AADS-PRD-12_login.md", "", ("prd", "filename")),
    ("product_requirements.md", "", ("prd", "filename")),
    ("요구사항_정리.md", "", ("prd", "filename")),
    ("api-contract-v2.md", "", ("contract", "filename")),
    ("system_spec.md", "", ("spec", "filename")),
    ("AADS-LAYOUT-03_chat.md", "", ("design", "filename")),
    ("architecture.html", "", ("design", "filename")),
    ("설계서.md", "", ("design", "filename")),
    ("release-plan.md", "", ("plan", "filename")),
    ("로드맵.md", "", ("plan", "filename")),
    ("notes.md", "Chat Architecture Overview", ("design", "title")),
    ("prd/notes.md", "", ("prd", "dirname")),
    ("planet.md", "", ("other", "none")),
    ("handover.md", "", ("other", "none")),
    ("", "", ("other", "none")),
])
def test_estimate_kind(rel, title, expected):
    assert inv.estimate_kind(rel, title) == expected


def test_estimate_kind_filename_beats_title_and_prd_beats_plan():
    assert inv.estimate_kind("prd-plan.md", "Design")[0] == "prd"
    assert inv.estimate_kind("spec.md", "Plan")[0] == "spec"


def test_extract_title():
    assert inv.extract_title("---\ntitle: 'Hello'\n---\n# Other\n", ".md") == "Hello"
    assert inv.extract_title("intro\n# Heading One\n", ".md") == "Heading One"
    assert inv.extract_title("<html><title> T <b>x</b></title></html>", ".html") == "T x"
    assert inv.extract_title("no heading", ".md") == ""


@pytest.mark.parametrize("path,expected", [
    ("/root/aads/aads-server/docs", "original"),
    ("/root/wt-foo/docs", "worktree"),
    ("/root/go100-card310/repo/docs", "worktree"),
    ("/root/go100/docs", "original"),
    ("/root/aads/aads-server-release-2bfdc89b/docs", "release"),
    ("/opt/go100/backend-releases/a7115356b/docs", "release"),
    ("/opt/go100/frontend-release-b0455d66c-20261002/report", "release"),
    ("/root/aads/aads-server-releases/x/docs", "release"),
    ("/root/aads-worktrees/x/docs", "worktree"),
    ("/root/release-notes-app/docs", "release"),
    ("/root/aads/_remote_docs/contabo14/kis/docs", "mirror"),
    ("/root/aads/_backups/x/docs", "backup"),
    ("/root/aads/_dirty_backup_20260929/docs", "backup"),
    ("/root/project-docs.bak.20260305/nas-image/reports", "backup"),
    ("/home/partner/obys/app.bak-20261001-0737/docs", "backup"),
    ("/root/bakery/docs", "original"),
])
def test_classify_root_kind(path, expected):
    assert inv.classify_root_kind(path) == expected


def row(server="contabo116", root="aads-server/docs", kind="original", sha="a" * 64, size=10, **extra):
    base = {"server": server, "root_alias": root, "root_kind": kind, "sha256": sha, "size_bytes": size,
            "copy_of_original": False, "snapshot_duplicate": False, "copy_of": "", "sha256_group_size": 0,
            "rel_path": "x.md", "path_sha256": "p" + root + server + sha, "goal_documents_registered": False}
    base.update(extra)
    return base


def test_mark_copies_requires_an_original_with_same_content():
    original = row()
    wt_copy = row(server="contabo14", root="found:root/wt-a/docs", kind="worktree")
    wt_unique = row(root="found:root/wt-b/docs", kind="worktree", sha="b" * 64)
    mirror = row(server="contabo116", root="_remote_docs", kind="mirror")
    other_original = row(server="cafe24_114", root="go100/docs", sha="a" * 64)
    rows = [original, wt_copy, wt_unique, mirror, other_original]
    inv.mark_copies(rows)
    assert not original["copy_of_original"] and not other_original["copy_of_original"]
    assert wt_copy["copy_of_original"] and wt_copy["copy_of"] == "cafe24_114:go100/docs"
    assert mirror["copy_of_original"] and mirror["copy_of"] == "contabo116:aads-server/docs"
    assert not wt_unique["copy_of_original"]
    assert original["sha256_group_size"] == 4 and wt_unique["sha256_group_size"] == 1


def test_mark_copies_prefers_same_server_original_and_ignores_empty_files():
    far = row(server="cafe24_114", root="go100/docs")
    near = row(server="contabo14", root="kis/docs")
    copy = row(server="contabo14", root="found:root/wt-a/docs", kind="worktree")
    empty_original = row(sha="e" * 64, size=0)
    empty_copy = row(root="found:root/wt-a/docs", kind="worktree", sha="e" * 64, size=0)
    rows = [far, near, copy, empty_original, empty_copy]
    inv.mark_copies(rows)
    assert copy["copy_of"] == "contabo14:kis/docs"
    assert not empty_copy["copy_of_original"]


def test_snapshot_duplicates_keep_one_representative_in_a_different_root():
    first = row(server="contabo14", root="found:opt/rel-a/docs", kind="release", sha="c" * 64)
    second = row(server="contabo14", root="found:opt/rel-b/docs", kind="release", sha="c" * 64)
    third = row(server="contabo14", root="found:opt/rel-c/docs", kind="release", sha="c" * 64)
    same_root_twin = row(server="contabo14", root="found:opt/rel-a/docs", kind="release", sha="c" * 64)
    unique = row(server="contabo14", root="found:opt/rel-c/docs", kind="release", sha="d" * 64)
    rows = [first, same_root_twin, second, third, unique]
    inv.mark_copies(rows)
    assert not first["snapshot_duplicate"] and not same_root_twin["snapshot_duplicate"]
    assert second["snapshot_duplicate"] and second["copy_of"] == "contabo14:found:opt/rel-a/docs"
    assert third["snapshot_duplicate"] and not unique["snapshot_duplicate"]
    assert not any(r["copy_of_original"] for r in rows)


def test_split_rows_collapses_copies_and_keeps_totals():
    original = row(sha="a" * 64)
    wt_copy = row(server="contabo14", root="wt", kind="worktree", sha="a" * 64, size=7)
    wt_copy2 = row(server="contabo14", root="wt", kind="worktree", sha="a" * 64, size=3,
                   goal_documents_registered=True)
    snap_a = row(server="contabo14", root="rel-a", kind="release", sha="c" * 64)
    snap_b = row(server="contabo14", root="rel-b", kind="release", sha="c" * 64)
    rows = [original, wt_copy, wt_copy2, snap_a, snap_b]
    inv.mark_copies(rows)
    kept, copies = inv.split_rows(rows)
    assert kept == [original, snap_a]
    assert len(kept) + sum(c["files"] for c in copies) == len(rows)
    by_kind = {(c["root_alias"], c["copy_kind"]): c for c in copies}
    assert by_kind[("wt", "of_original")]["files"] == 2
    assert by_kind[("wt", "of_original")]["size_bytes_total"] == 10
    assert by_kind[("wt", "of_original")]["goal_documents_registered_files"] == 1
    assert by_kind[("rel-b", "snapshot_duplicate")]["copy_of"] == "contabo14:rel-a"


def test_registration_absolute_and_relative_match_rules():
    abs_hash = inv.path_sha256("/root/aads/aads-server/docs/a.md")
    rel_hash = inv.sha256_text("docs/b.md")
    index = {abs_hash: ["7"], rel_hash: ["9"]}
    base = {"server": "contabo116", "root_kind": "original", "git_repo": "aads-server",
            "path_sha256": abs_hash, "repo_rel_sha256": [inv.sha256_text("docs/zzz.md")]}
    assert inv.registration_of(base, index) == ("absolute_path", ["7"])
    rel = dict(base, path_sha256="0" * 64, repo_rel_sha256=[rel_hash, inv.sha256_text("./docs/b.md")])
    assert inv.registration_of(rel, index) == ("relative_path", ["9"])
    # A relative doc_path is only evidence for the live aads-server repo, never for a copy or another repo.
    assert inv.registration_of(dict(rel, root_kind="worktree"), index)[0] == "none"
    assert inv.registration_of(dict(rel, git_repo="aads-dashboard"), index)[0] == "none"
    assert inv.registration_of(dict(rel, server="contabo14"), index)[0] == "none"


def test_load_goal_index_uses_hash_and_public_path(tmp_path):
    csv_path = tmp_path / "v.csv"
    csv_path.write_text(
        "id,path_sha256,public_path\n1,%s,\n2,%s,docs/a.md\n" % (inv.path_sha256("/abs/x.md"), "f" * 64),
        encoding="utf-8")
    index, total = inv.load_goal_index(csv_path)
    assert total == 2
    assert index[inv.path_sha256("/abs/x.md")] == ["1"]
    assert index[inv.sha256_text("docs/a.md")] == ["2"]


def test_collect_hashes_files_skips_symlinks_and_excluded_dirs(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "node_modules").mkdir()
    (docs / "a.md").write_text("# Alpha PRD\n", encoding="utf-8")
    (docs / "sub" / "b.HTML").write_text("<title>B</title>", encoding="utf-8")
    (docs / "c.txt").write_text("ignored", encoding="utf-8")
    (docs / "node_modules" / "n.md").write_text("ignored", encoding="utf-8")
    os.symlink(docs / "a.md", docs / "link.md")
    os.symlink(docs / "sub", docs / "linkdir")
    records = collect_records("srv", docs)
    got = {f["rel_path"]: f for f in files(records)}
    assert sorted(got) == ["a.md", "sub/b.HTML"]
    assert got["a.md"]["sha256"] == hashlib.sha256(b"# Alpha PRD\n").hexdigest()
    assert got["a.md"]["kind"] == "prd" and got["a.md"]["kind_basis"] == "title"
    assert got["a.md"]["path_sha256"] == inv.path_sha256(str(docs / "a.md"))
    root = [r for r in records if r["t"] == "root"][0]
    assert root["counted"] == 2 and root["skipped_symlink"] == 2 and root["status"] == "ok"
    assert str(tmp_path) not in json.dumps(records)


def test_collect_never_emits_body_or_absolute_path(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# Title\nunique-body-marker-12345\n", encoding="utf-8")
    dumped = json.dumps(collect_records("srv", docs))
    assert "unique-body-marker-12345" not in dumped
    assert str(tmp_path) not in dumped


def test_secret_in_body_or_name_blanks_path(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "leak.md").write_text("password = hunter2hunter2\n", encoding="utf-8")
    (docs / "clean.md").write_text("nothing here\n", encoding="utf-8")
    (docs / "api_key=abcdef.md").write_text("fine\n", encoding="utf-8")
    got = {f["path_sha256"]: f for f in files(collect_records("srv", docs))}
    assert got[inv.path_sha256(str(docs / "leak.md"))]["secret_scan"] == "hit"
    assert got[inv.path_sha256(str(docs / "leak.md"))]["rel_path"] == ""
    assert got[inv.path_sha256(str(docs / "api_key=abcdef.md"))]["rel_path"] == ""
    assert got[inv.path_sha256(str(docs / "clean.md"))]["rel_path"] == "clean.md"
    assert got[inv.path_sha256(str(docs / "clean.md"))]["secret_scan"] == "clean"


def test_large_file_is_hashed_but_not_scanned(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(inv, "MAX_SCAN_BYTES", 16)
    body = b"x" * 100
    (docs / "big.md").write_bytes(body)
    (rec,) = files(collect_records("srv", docs))
    assert rec["read_status"] == "ok_unscanned" and rec["secret_scan"] == "skipped_too_large"
    assert rec["size"] == 100
    assert rec["sha256"] == hashlib.sha256(body).hexdigest()


def test_resolve_roots_drops_nested_discovered_roots_and_keeps_known():
    roots = inv.resolve_roots(
        [("k1", "/r/aads-docs"), ("k2", "/r/app/static/reports")],
        ["/r/aads-docs/docs", "/r/other/docs", "/r/app/static/reports"])
    paths = [p for _a, p, _o in roots]
    assert paths[:2] == ["/r/aads-docs", "/r/app/static/reports"]  # known roots first, declared order
    assert paths[2:] == sorted(paths[2:])
    assert "/r/aads-docs/docs" not in paths
    assert paths.count("/r/app/static/reports") == 1
    assert dict((p, a) for a, p, _o in roots)["/r/other/docs"] == "found:r/other/docs"


def test_discover_roots_depth_exclusions_and_symlinks(tmp_path):
    for rel in ("a/docs", "a/b/c/reports", "a/b/c/d/docs", "node_modules/x/docs", ".hid/docs", "p/prd/docs"):
        (tmp_path / rel).mkdir(parents=True)
    os.symlink(tmp_path / "a" / "docs", tmp_path / "a" / "plan")
    found, hit = inv.discover_roots([str(tmp_path)], time.monotonic() + 30)
    rel = sorted(os.path.relpath(p, tmp_path) for p in found)
    assert rel == ["a/b/c/reports", "a/docs", "p/prd"]
    assert hit is False


def test_discover_roots_honours_deadline(tmp_path):
    (tmp_path / "a" / "docs").mkdir(parents=True)
    found, hit = inv.discover_roots([str(tmp_path)], time.monotonic() - 1)
    assert hit is True and found == []


def test_collect_reports_missing_root_and_realpath_alias(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("x", encoding="utf-8")
    os.symlink(docs, tmp_path / "docs2")
    records = []
    inv.collect("srv", records.append, known=[("gone", str(tmp_path / "nope")), ("one", str(docs)),
                                              ("two", str(tmp_path / "docs2"))], bases=())
    status = {r["root_alias"]: r["status"] for r in records if r["t"] == "root"}
    assert status["gone"] == "missing"
    assert {status["one"], status["two"]} == {"ok", "alias_of:" + ("one" if status["one"] == "ok" else "two")}
    assert len(files(records)) == 1


def test_collect_marks_git_worktree_file_as_worktree_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    (repo / "docs" / "a.md").write_text("x", encoding="utf-8")
    records = collect_records("srv", repo / "docs")
    root = [r for r in records if r["t"] == "root"][0]
    assert root["root_kind"] == "worktree" and root["git_worktree"] is True


def test_git_failure_reason_is_recorded_without_the_repo_path(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("x", encoding="utf-8")
    records = collect_records("srv", docs)
    root = [r for r in records if r["t"] == "root"][0]
    assert root["git_error"] and str(tmp_path) not in root["git_error"]
    assert files(records)[0]["git_state"] == "git_error"


def test_bogus_dot_git_directory_is_not_a_repository(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    assert inv.find_git_top(str(tmp_path / "docs")) == (None, False)


def test_stray_dot_git_at_filesystem_root_is_not_a_repository(tmp_path, monkeypatch):
    real_isdir, real_exists = inv.os.path.isdir, inv.os.path.exists
    monkeypatch.setattr(inv.os.path, "isdir", lambda p: p == "/.git" or real_isdir(p))
    monkeypatch.setattr(inv.os.path, "exists", lambda p: p == "/.git/HEAD" or real_exists(p))
    assert inv.find_git_top("/srv/docs") == (None, False)
    assert inv.find_git_top("/") == (None, False)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_state_committed_modified_staged_untracked(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")

    def git(*args):
        subprocess.run(["git", "-C", str(repo)] + list(args), check=True, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    git("init", "-q")
    (docs / "clean.md").write_text("c", encoding="utf-8")
    (docs / "mod.md").write_text("m", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    (docs / "mod.md").write_text("changed", encoding="utf-8")
    (docs / "staged.md").write_text("s", encoding="utf-8")
    git("add", "docs/staged.md")
    (docs / "new.md").write_text("n", encoding="utf-8")
    before = (repo / ".git" / "index").read_bytes()
    states = {f["rel_path"]: f["git_state"] for f in files(collect_records("srv", docs))}
    assert states == {"clean.md": "committed_clean", "mod.md": "committed_modified",
                      "staged.md": "staged_uncommitted", "new.md": "untracked"}
    assert (repo / ".git" / "index").read_bytes() == before
    root = [r for r in collect_records("srv", docs) if r["t"] == "root"][0]
    assert root["git_repo"] == "repo" and len(root["git_head"]) == 8


def raw_file(server="contabo116", alias="aads-server/docs", rel="a.md", **extra):
    base = {"t": "file", "server": server, "root_alias": alias, "root_kind": "original", "rel_path": rel,
            "path_sha256": inv.path_sha256("/x/" + rel), "repo_rel_sha256": [], "ext": ".md", "size": 5,
            "mtime": "2026-10-02T00:00:00Z", "sha256": "a" * 64, "read_status": "ok", "git_state": "no_git",
            "kind": "other", "kind_basis": "none", "secret_scan": "clean"}
    base.update(extra)
    return base


def raw_root(server="contabo116", alias="aads-server/docs", counted=1, **extra):
    base = {"t": "root", "server": server, "root_alias": alias, "root_kind": "original", "status": "ok",
            "counted": counted, "git_repo": "aads-server", "git_head": "abcd1234"}
    base.update(extra)
    return base


def test_merge_totals_equal_rows_and_report_failures(tmp_path):
    verdict = tmp_path / "v.csv"
    verdict.write_text("id,path_sha256,public_path\n1,%s,\n2,%s,\n" % (
        inv.path_sha256("/x/a.md"), "9" * 64), encoding="utf-8")
    records = [
        raw_file(rel="a.md", kind="prd", kind_basis="filename"),
        raw_file(rel="b.md", **{"path_sha256": inv.path_sha256("/x/b.md")}),
        raw_root(counted=2),
        raw_file(server="contabo14", alias="wt", rel="a.md", root_kind="worktree",
                 **{"path_sha256": inv.path_sha256("/y/a.md")}),
        raw_root(server="contabo14", alias="wt", counted=1, root_kind="worktree", git_repo="wt"),
        {"t": "root", "server": "cafe24_114", "root_alias": "gone", "root_kind": "original",
         "status": "missing", "counted": 0},
        {"t": "server", "server": "jinah244", "status": "not_collected", "reason": "ssh denied"},
    ]
    rows, summary = inv.merge(records, verdict)
    assert summary["total_rows"] == len(rows) == 3
    assert summary["measured_total"] == 3 and summary["measured_matches_rows"] is True
    assert summary["goal_documents"] == {"verdict_rows": 2, "matched_distinct_ids": 1, "not_found_on_any_server": 1}
    by_root = {(e["server"], e["root_alias"]): e for e in summary["by_root"]}
    assert by_root[("contabo116", "aads-server/docs")]["files"] == 2
    assert by_root[("contabo116", "aads-server/docs")]["planning_design_prd"] == 1
    assert by_root[("contabo116", "aads-server/docs")]["unregistered"] == 1
    assert by_root[("contabo14", "wt")]["copies"] == 1
    assert {(f["server"], f["status"]) for f in summary["failures"]} == {
        ("cafe24_114", "missing"), ("jinah244", "not_collected")}
    copy = [r for r in rows if r["server"] == "contabo14"][0]
    assert copy["copy_of_original"] is True and copy["registered_basis"] == "none"


def test_merge_flags_cut_off_collection(tmp_path):
    verdict = tmp_path / "v.csv"
    verdict.write_text("id,path_sha256,public_path\n", encoding="utf-8")
    rows, summary = inv.merge([raw_file()], verdict)
    assert summary["measured_matches_rows"] is False
    assert summary["failures"][0]["status"] == "no_root_record"


def test_csv_has_fixed_columns_no_absolute_paths_and_neutralises_formulas():
    rows = inv.build_rows([raw_file(rel="-sneaky.md"), raw_root()], {})
    text = inv.rows_to_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert tuple(parsed[0].keys()) == inv.CSV_COLUMNS
    assert parsed[0]["rel_path"] == "'-sneaky.md"
    assert parsed[0]["kind_is_estimate"] == "true"
    assert parsed[0]["goal_documents_registered"] == "false"
    assert parsed[0]["registered_goal_doc_ids"] == "" and parsed[0]["copy_of"] == ""
    assert "/x/" not in text


def test_markdown_table_sums_match_summary(tmp_path):
    verdict = tmp_path / "v.csv"
    verdict.write_text("id,path_sha256,public_path\n", encoding="utf-8")
    rows, summary = inv.merge([raw_file(), raw_root()], verdict)
    text = inv.render_markdown(summary)
    assert "| **합계** | 1 |" in text and "스냅샷 중복" in text


def test_secret_pattern_matches_canonical_api_pattern():
    source = (ROOT / "app/api/canonical_documents.py").read_text(encoding="utf-8")
    start = source.index("SECRET = re.compile(")
    end = source.index("\n)\n", start) + 3
    namespace = {"re": re}
    exec(source[start:end], namespace)  # only the regex literal assignment
    assert namespace["SECRET"].pattern == inv.SECRET.pattern


def test_script_is_read_only_and_python38_compatible():
    source = SCRIPT.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 8))
    for banned in ("asyncpg", "psycopg", "INSERT ", "UPDATE ", "DELETE ", ".removeprefix(", ".removesuffix(",
                   "is_relative_to(", "os.remove", "os.unlink", "shutil.move", "git add", "git commit"):
        assert banned not in source, banned
    # Remote collection pipes the script to python3 on stdin; it must not rely on __file__ at import time.
    module_level = [n for n in ast.parse(source).body if isinstance(n, ast.Assign)]
    assert not any("__file__" in ast.dump(n) for n in module_level)
