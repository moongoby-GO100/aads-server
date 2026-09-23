from scripts import audit_claude_cli_runtime as audit


def test_pinned_artifact_requires_official_url_and_checksum():
    dockerfile = (audit.ROOT / "Dockerfile").read_text()
    version, checksum = audit.pinned_artifact(dockerfile)
    assert tuple(map(int, version.split("."))) >= (2, 1, 280)
    assert len(checksum) == 64
    bad = dockerfile.replace("--checksum=sha256:", "--checksum=none:")
    try:
        audit.pinned_artifact(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("unpinned binary must fail closed")


def test_audit_reports_effective_paths_separately(monkeypatch):
    pinned, _ = audit.pinned_artifact((audit.ROOT / "Dockerfile").read_text())
    monkeypatch.setattr(audit, "runner_binary", lambda: "/opt/runner/claude")
    monkeypatch.setattr(audit, "active_container", lambda: "aads-server-green")
    calls = []

    def version(argv):
        calls.append(argv)
        if argv[0] == "/usr/bin/claude":
            return pinned
        if argv[0] == "/opt/runner/claude":
            return "2.1.259"
        if "aads-server-green" in argv:
            return pinned
        return None

    monkeypatch.setattr(audit, "run_version", version)
    result = audit.audit()
    assert result["host_global_version"] == pinned
    assert result["runner_version"] == "2.1.259"
    assert result["active_chat_version"] == pinned
    assert result["standby_chat_version"] is None
    assert not result["converged"]
    assert len(calls) == 4
