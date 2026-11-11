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


def _pin_main(monkeypatch, pinned):
    monkeypatch.setattr(update, "command", lambda argv, **kw: "b" * 40)
    monkeypatch.setattr(update, "pinned_artifact", lambda source: (pinned, "c" * 64))


def test_pending_candidate_blocks_new_release(monkeypatch):
    # 이전 후보가 main 고정값과 다르면(아직 들어가지 않음) 새 후보를 만들지 않는다.
    monkeypatch.setattr(update, "state", lambda: {"version": "2.1.281", "phase": "pushed"})
    _pin_main(monkeypatch, "2.1.280")
    with pytest.raises(update.Deferred, match="previous CLI candidate"):
        update.prepare("2.1.282", "a" * 64, 233_000_000)


def test_pinned_incomplete_candidate_is_superseded_by_newer_patch(monkeypatch, capsys):
    # main 에 이미 고정된 이전 후보(2.1.285 pushed)는 같은 차선의 새 공식판으로 대체된다.
    monkeypatch.setattr(update, "state", lambda: {"version": "2.1.285", "phase": "pushed"})
    _pin_main(monkeypatch, "2.1.285")

    class Reached(Exception):
        pass

    def stop(*args):
        raise Reached

    monkeypatch.setattr(update, "verified_binary", stop)
    with pytest.raises(Reached):
        update.prepare("2.1.322", "a" * 64, 233_000_000)
    assert '"phase": "superseded"' in capsys.readouterr().out


def test_superseding_never_crosses_minor_lane(monkeypatch):
    monkeypatch.setattr(update, "state", lambda: {"version": "2.1.285", "phase": "pushed"})
    _pin_main(monkeypatch, "2.1.285")
    with pytest.raises(update.Deferred, match="previous CLI candidate"):
        update.prepare("2.2.0", "a" * 64, 233_000_000)


def test_already_pinned_requires_effective_runtime_convergence(monkeypatch):
    monkeypatch.setattr(update, "official_release", lambda: ("2.1.280", "a" * 64, 233_000_000))
    monkeypatch.setattr(update, "prepare", lambda *args: {})
    monkeypatch.setattr(update, "chat_converged", lambda version: False)
    with pytest.raises(update.Deferred, match="have not converged"):
        update.apply()
    monkeypatch.setattr(update, "chat_converged", lambda version: True)
    monkeypatch.setattr(update, "runner_binary", lambda: "/runner/claude")
    monkeypatch.setattr(update, "run_version", lambda argv: "2.1.280")
    assert update.apply()["phase"] == "up_to_date"
