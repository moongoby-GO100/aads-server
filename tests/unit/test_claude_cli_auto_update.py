import json

import pytest

from scripts import claude_cli_auto_update as update


def test_patch_lane_rejects_minor_jump_and_downgrade():
    assert update.same_patch_lane("2.1.280", "2.1.281")
    assert not update.same_patch_lane("2.1.280", "2.2.0")
    assert not update.same_patch_lane("2.1.280", "2.1.279")


def test_replace_pin_changes_only_expected_artifact_fields():
    source = (update.ROOT / "Dockerfile").read_text()
    old, old_sha = update.pinned_artifact(source)
    target = "2.1.999"
    checksum = "a" * 64
    changed = update.replace_pin(source, old, old_sha, target, checksum)
    assert update.pinned_artifact(changed) == (target, checksum)
    assert f"= '{target} (Claude Code)'" in changed
    assert old_sha not in changed
    with pytest.raises(update.Deferred):
        update.replace_pin(source.replace("--checksum=sha256:", "--checksum=none:"),
                           old, old_sha, target, checksum)


def test_manifest_identity_failure_is_closed(monkeypatch):
    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, *_):
            return self.body

    responses = iter([
        Response(b"2.1.281"),
        Response(json.dumps({"version": "2.1.280", "platforms": {
            "linux-x64": {"binary": "claude", "checksum": "b" * 64,
                          "size": 233_000_000}}}).encode()),
    ])
    monkeypatch.setattr(update, "urlopen", lambda *_, **__: next(responses))
    with pytest.raises(update.Deferred, match="identity"):
        update.official_release()


def test_pending_candidate_blocks_new_release(monkeypatch):
    monkeypatch.setattr(update, "state", lambda: {"version": "2.1.281", "phase": "pushed"})
    with pytest.raises(update.Deferred, match="previous CLI candidate"):
        update.prepare("2.1.282", "a" * 64, 233_000_000)
