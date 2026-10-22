"""오비서 근태 M1-B: localStorage 근태 1회 자동 이관 — 정적 검사 + node 로 이관 로직 실행."""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

HTML = (Path(__file__).parents[2] / "app/static/apps/obys/index.html").read_text(encoding="utf-8")


def _slice(start: str, end: str) -> str:
    head = HTML.index(start)
    return HTML[head:HTML.index(end, head)]


MIGRATION_JS = _slice("    async function pushLocalShifts(targets) {", "    async function inventoryApi(")


def test_auto_migrate_hooked_after_settings_load_in_all_entrypoints():
    assert HTML.count("loadServerSettings().catch(") >= 3
    assert HTML.count(".then(() => autoMigrateLocalShifts())") == 3


def test_ledger_key_and_no_new_server_api():
    assert 'const ATTENDANCE_MIGRATION_LEDGER = "yeoljeong-attendance-migrated-v1"' in MIGRATION_JS
    assert "console." not in MIGRATION_JS
    assert not re.search(r"workspaceApi\(`[^`]*migrat", MIGRATION_JS)


def test_duplicate_409_counts_as_success_and_failures_stay_local():
    push = _slice("    async function pushLocalShifts(targets) {", "    async function uploadLocalShifts(")
    assert "=== 409" in push
    assert push.count("state.shifts = state.shifts.filter") == 2
    assert "result.failed.push" in push


def test_manual_button_still_wired():
    assert 'id="uploadLocalShiftsBtn"' in HTML
    assert "uploadLocalShifts(state.shifts.map(item => item.id))" in HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="node 필요")
def test_migration_logic_runs_in_node(tmp_path):
    harness = textwrap.dedent("""
        const store = {};
        const localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
        const toasts = [];
        const showToast = m => toasts.push(m);
        let posts = 0;
        const state = { shifts: [
          { id: "a", date: "2026-10-01", staff: "가", branch: "B1", start: "09:00" },
          { id: "b", date: "2026-10-01", staff: "나", branch: "B1", start: "09:00" },
          { id: "c", date: "2026-10-02", staff: "다", branch: "B1", start: "09:00" },
          { id: "d", date: "2026-10-02", staff: "라", branch: "NOPE", start: "09:00" },
        ] };
        let serverShifts = []; let serverShiftsError = ""; let employeePreview = null;
        const hasServerAuth = () => true;
        const businessIdForBranchName = n => (n === "B1" ? "biz1" : "");
        const selectedSettingsBusinessId = () => "";
        function saveState() {}
        async function loadAttendanceRecords() { serverShifts = [{ businessId: "biz1" }, { businessId: "biz1" }]; }
        async function saveShiftToServer(shift) {
          posts += 1;
          if (shift.id === "b") { const e = new Error("dup"); e.status = 409; throw e; }
          if (shift.id === "c") { const e = new Error("boom"); e.status = 500; throw e; }
        }
        __SRC__
        (async () => {
          const first = await autoMigrateLocalShifts();
          const afterFirst = state.shifts.map(s => s.id);
          const postsAfterFirst = posts;
          const second = await autoMigrateLocalShifts();
          console.log(JSON.stringify({ first, afterFirst, secondPosts: posts - postsAfterFirst, second,
            ledger: JSON.parse(store["yeoljeong-attendance-migrated-v1"]), toasts }));
        })();
    """).replace("__SRC__", MIGRATION_JS.split("    async function uploadLocalShifts(")[0]
                 + "async function uploadLocalShifts(" + MIGRATION_JS.split("    async function uploadLocalShifts(")[1])
    script = tmp_path / "h.js"
    script.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["first"]["attempted"] == 3 and data["first"]["succeeded"] == 2
    assert data["first"]["duplicate"] == 1 and data["first"]["failed"] == 1
    assert data["afterFirst"] == ["c", "d"]
    assert data["secondPosts"] == 0
    entry = data["ledger"]["businesses"]["biz1"]
    assert entry["failedIds"] == ["c"] and entry["failed"] == 1
    assert data["ledger"]["lastRun"]["localBefore"] == 4 and data["ledger"]["lastRun"]["localAfter"] == 2
    assert data["ledger"]["lastRun"]["serverCounts"] == {"biz1": 2}
    assert any("2건 이관, 1건 실패" in t for t in data["toasts"])
    assert not any("biz1" in t for t in data["toasts"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node 필요")
def test_inline_scripts_parse(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", HTML, re.S)
    assert scripts
    for i, body in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(body, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
