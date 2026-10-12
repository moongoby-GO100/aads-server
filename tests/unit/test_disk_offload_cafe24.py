from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "disk_offload_cafe24.sh"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_interrupted_dump_transfer_resumes_and_retries_long_enough() -> None:
    script = _script()
    assert 'RSYNC_RETRIES="${AADS_OFFLOAD_RSYNC_RETRIES:-30}"' in script
    assert "rsync -a --partial --append" in script


def test_retained_dumps_are_backfilled_before_new_dump() -> None:
    script = _script()
    assert script.index("backfill_retained_dumps") < script.index('tmp="$dump.partial.$$"')
    assert "offload_dump_file \"$retained\"" in script


def test_old_local_dump_removal_is_checksum_fenced() -> None:
    script = _script()
    checksum_guard = '[[ -n "$old_local_sum" && "$old_local_sum" == "$old_remote_sum" ]]'
    assert checksum_guard in script
    assert script.index(checksum_guard) < script.index('rm -f -- "$old"')


def test_checksum_mismatch_is_quarantined_before_full_retry() -> None:
    script = _script()
    quarantine = 'mv -- \'$remote_dump\' \'$quarantine\''
    assert quarantine in script
    assert script.index(quarantine) < script.index(
        'rsync_resume "$local_dump" "$REMOTE:$remote_dump"',
        script.index(quarantine),
    )


def test_pg_dump_requires_latest_dump_size_plus_reserve() -> None:
    script = _script()
    assert "expected_dump_kb=$(((latest_dump_bytes + 1023) / 1024))" in script
    assert "required_free_kb=$((expected_dump_kb + MIN_FREE_GB * 1024 * 1024))" in script


def test_failures_use_deduplicated_ohvis_alert_instead_of_telegram() -> None:
    script = _script()
    assert "INSERT INTO alert_history" in script
    assert "INTERVAL '6 hours'" in script
    assert "api.telegram.org" not in script
