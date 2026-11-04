import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "offload_retention.sh"
OFFLOAD = ROOT / "scripts" / "disk_offload_cafe24.sh"
EXPECTED_ROOT = "/home/danharoo/www/data/files/goods/goodscode/_aads-backup"
TODAY = "20261104"


def _bash(code: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; {code}'],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _classify(names: list[str], today: str = TODAY) -> dict[str, str]:
    stdin = "\n".join(names) + ("\n" if names else "")
    proc = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; retention_classify {today}'],
        input=stdin, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = {}
    for line in proc.stdout.splitlines():
        verdict, name, _ = line.split("\t", 2)
        result[name] = verdict
    return result


def _days_before(days: int, today: str = TODAY) -> str:
    proc = subprocess.run(
        ["date", "-u", "-d", f"{today[:4]}-{today[4:6]}-{today[6:]} -{days} days", "+%Y%m%d"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def test_script_is_syntax_valid() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_daily_boundary_day_30_kept_day_31_candidate() -> None:
    d30, d31 = _days_before(30), _days_before(31)
    verdicts = _classify([TODAY, d30, d31])
    assert verdicts[d30] == "KEEP_DAILY"
    assert verdicts[d31] == "DELETE_CANDIDATE"


def test_monthly_first_day_kept_within_12_months() -> None:
    verdicts = _classify([TODAY, "20251201", "20251101", "20260101"])
    assert verdicts["20251201"] == "KEEP_MONTHLY"
    assert verdicts["20260101"] == "KEEP_MONTHLY"
    assert verdicts["20251101"] == "DELETE_CANDIDATE"


def test_monthly_boundary_on_first_of_month_keeps_exactly_12_months_back() -> None:
    verdicts = _classify(["20261101", "20251101", "20251001"], today="20261101")
    assert verdicts["20251101"] == "KEEP_MONTHLY"
    assert verdicts["20251001"] == "DELETE_CANDIDATE"


def test_non_first_day_old_folder_is_candidate_even_in_monthly_window() -> None:
    assert _classify([TODAY, "20260215"])["20260215"] == "DELETE_CANDIDATE"


def test_non_date_folders_are_manual_review_not_candidates() -> None:
    verdicts = _classify([TODAY, "tmp-cleanup-20261016", "2026110", "202611040", "20261340"])
    assert verdicts["tmp-cleanup-20261016"] == "MANUAL"
    assert verdicts["2026110"] == "MANUAL"
    assert verdicts["202611040"] == "MANUAL"
    assert verdicts["20261340"] == "MANUAL"


def test_future_dated_folder_is_manual() -> None:
    verdicts = _classify(["20261120", TODAY])
    assert verdicts["20261120"] == "MANUAL"
    assert verdicts[TODAY] == "KEEP_LATEST"


def test_latest_folder_always_kept_even_when_old() -> None:
    verdicts = _classify(["20200105", "20200104"], today=TODAY)
    assert verdicts["20200105"] == "KEEP_LATEST"
    assert verdicts["20200104"] == "DELETE_CANDIDATE"


def test_empty_list_classifies_to_nothing() -> None:
    assert _classify([]) == {}


def test_invalid_today_is_rejected() -> None:
    assert _bash("retention_classify 20261399 </dev/null").returncode == 2


@pytest.mark.parametrize(
    ("total", "cands", "ok"),
    [(0, 0, False), (10, 5, True), (10, 6, False), (3, 0, True), (1, 1, False)],
)
def test_candidate_ratio_limit(total: int, cands: int, ok: bool) -> None:
    proc = _bash(f"retention_check_limits {total} {cands}")
    assert (proc.returncode == 0) is ok


def _run_main(listing: str, tmp_path: Path, root: str | None = EXPECTED_ROOT, extra: str = "") -> subprocess.CompletedProcess:
    listing_file = tmp_path / "listing.tsv"
    listing_file.write_text(listing, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "AADS_RETENTION_LISTING_FILE": str(listing_file)}
    if root is not None:
        env["AADS_RETENTION_REMOTE_ROOT"] = root
    return subprocess.run(
        ["bash", str(SCRIPT), "--today", TODAY] + ([extra] if extra else []),
        capture_output=True, text=True, timeout=60, env=env,
    )


def _listing(names: list[str]) -> str:
    return "".join(f"8912345\t{n}/\n" for n in names)


def test_main_prints_lists_and_zero_deletions(tmp_path: Path) -> None:
    proc = _run_main(_listing([TODAY, _days_before(1), _days_before(40), "tmp-cleanup-20261016"]), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "-- 삭제 후보 1개" in proc.stdout
    assert _days_before(40) in proc.stdout.split("-- 삭제 후보")[1].split("-- 수동 검토")[0]
    assert "tmp-cleanup-20261016" in proc.stdout.split("-- 수동 검토")[1]
    assert "삭제 실행 0건" in proc.stdout


def test_main_aborts_when_candidates_exceed_half(tmp_path: Path) -> None:
    proc = _run_main(_listing([TODAY, "20260102", "20260103", "20260104"]), tmp_path)
    assert proc.returncode == 1
    assert "50%" in proc.stderr
    assert "-- 삭제 후보" not in proc.stdout


def test_main_aborts_when_no_date_folder_parsed(tmp_path: Path) -> None:
    proc = _run_main(_listing(["tmp-cleanup-20261016"]), tmp_path)
    assert proc.returncode == 1
    assert "0개" in proc.stderr


@pytest.mark.parametrize("root", ["", "/home/danharoo/www", EXPECTED_ROOT + "/"])
def test_main_aborts_on_unexpected_remote_root(tmp_path: Path, root: str) -> None:
    proc = _run_main(_listing([TODAY]), tmp_path, root=root)
    assert proc.returncode == 1
    assert "REMOTE_ROOT" in proc.stderr


def test_main_rejects_unknown_options(tmp_path: Path) -> None:
    assert _run_main(_listing([TODAY]), tmp_path, extra="--unknown").returncode == 2


def test_remote_settings_match_disk_offload_script() -> None:
    offload = OFFLOAD.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    assert f'REMOTE_ROOT="{EXPECTED_ROOT}"' in offload
    assert f'RETENTION_EXPECTED_ROOT="{EXPECTED_ROOT}"' in script
    host = re.search(r'^REMOTE="([^"]+)"', offload, re.M).group(1)
    assert f'remote="{host}"' in script
    ssh_opts = re.search(r"^SSH_OPTS='([^']+)'", offload, re.M).group(1)
    assert f"ssh_opts='{ssh_opts}'" in script


def test_script_has_no_destructive_or_remote_write_commands() -> None:
    code = "\n".join(
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"\b(rm|mv|rsync|chmod|chown|truncate|unlink|shred)\b", code)
    assert "--apply" not in code
    assert "find " not in code
