"""server-health 응답의 extra_disks(서브디스크) 파싱·실패 허용 검증.

루트(/) 디스크만 보이던 cafe24_114 카드에 /dev/sdb1 서브디스크를 더하는 기능이다.
추가 디스크 조회가 실패해도 기존 응답(disk_pct, status)은 깨지면 안 된다.
"""
import json
from types import SimpleNamespace

from app.api import ops
from app.services.server_registry import get_server_config

DF_SUB = (
    "Filesystem      Size  Used Avail Use% Mounted on\n"
    "/dev/sdb1        11T    6T  4.3T  55% /home/danharoo/www/data/files/goods/goodscode\n"
)


def _proc(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def test_parse_df_extra_fields():
    item = ops._parse_df_extra(DF_SUB, "백업 서브디스크")
    assert item == {
        "label": "백업 서브디스크",
        "mount": "/home/danharoo/www/data/files/goods/goodscode",
        "device": "/dev/sdb1",
        "size": "11T",
        "used": "6T",
        "available": "4.3T",
        "pct": 55,
    }


def test_parse_df_extra_mount_with_space():
    out = "Filesystem Size Used Avail Use% Mounted on\n/dev/sdb1 10T 1T 9T 10% /mnt/my disk\n"
    assert ops._parse_df_extra(out, "x")["mount"] == "/mnt/my disk"


def test_parse_df_extra_garbage_returns_none():
    assert ops._parse_df_extra("", "x") is None
    assert ops._parse_df_extra("Filesystem only header\n", "x") is None
    assert ops._parse_df_extra("h\n/dev/sdb1 1 2 3 notpct /m\n", "x") is None


def test_registry_registers_cafe24_114_backup_path():
    paths = get_server_config("cafe24_114")["extra_disk_paths"]
    assert any(p["path"].endswith("/goodscode/_aads-backup") for p in paths)
    assert not get_server_config("contabo92").get("extra_disk_paths")


def test_remote_health_config_carries_extra_disk_paths():
    cfg = ops._remote_health_config("cafe24_114")
    assert cfg["extra_disk_paths"]
    assert ops._remote_health_config("contabo116")["extra_disk_paths"] == []


def test_collect_extra_disks_uses_readonly_df(monkeypatch):
    calls = []

    def fake_probe(cfg, command, timeout=8):
        calls.append(command)
        return _proc(DF_SUB)

    monkeypatch.setattr(ops, "_run_server_probe", fake_probe)
    cfg = {"id": "t", "extra_disk_paths": [{"label": "L", "path": "/a b/c"}]}
    disks = ops._collect_extra_disks(cfg)
    assert len(disks) == 1 and disks[0]["device"] == "/dev/sdb1"
    assert calls == ["df -P -h -- '/a b/c'"]


def test_collect_extra_disks_failure_gives_empty_list(monkeypatch):
    cfg = {"id": "t", "extra_disk_paths": [{"label": "L", "path": "/x"}]}

    monkeypatch.setattr(ops, "_run_server_probe", lambda *a, **k: _proc("", 1, "no such file"))
    assert ops._collect_extra_disks(cfg) == []

    def boom(*a, **k):
        raise TimeoutError("ssh timeout")

    monkeypatch.setattr(ops, "_run_server_probe", boom)
    assert ops._collect_extra_disks(cfg) == []
    assert ops._collect_extra_disks({"id": "t"}) == []


def test_fallback_includes_extra_disks_and_keeps_status(monkeypatch):
    def fake_probe(cfg, command, timeout=8):
        if command.startswith("df -P -h --"):
            return _proc(DF_SUB)
        if command == "df -h /":
            return _proc("Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 100G 50G 50G 50% /\n")
        return _proc("", 1, "n/a")

    monkeypatch.setattr(ops, "_run_server_probe", fake_probe)
    cfg = ops._remote_health_config("cafe24_114")
    result = ops._collect_server_health_fallback(cfg)
    assert result["disk_pct"] == 50
    assert result["status"] == "ok"
    assert result["extra_disks"][0]["device"] == "/dev/sdb1"


def test_fallback_extra_disk_failure_keeps_base_response(monkeypatch):
    def fake_probe(cfg, command, timeout=8):
        if command.startswith("df -P -h --"):
            raise RuntimeError("boom")
        if command == "df -h /":
            return _proc("Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 100G 95G 5G 95% /\n")
        return _proc("", 1, "n/a")

    monkeypatch.setattr(ops, "_run_server_probe", fake_probe)
    result = ops._collect_server_health_fallback(ops._remote_health_config("cafe24_114"))
    assert result["extra_disks"] == []
    assert result["disk_pct"] == 95
    assert result["status"] == "critical"


def test_health_server_success_path_adds_extra_disks(monkeypatch):
    import asyncio

    def fake_probe(cfg, command, timeout=8):
        if command.startswith("curl"):
            return _proc(json.dumps({"status": "ok", "disk_pct": 40}))
        return _proc(DF_SUB)

    monkeypatch.setattr(ops, "_run_server_probe", fake_probe)
    payload = asyncio.run(ops.remote_server_health("cafe24_114"))
    assert payload["disk_pct"] == 40
    assert payload["source"] == "health_server"
    assert payload["extra_disks"][0]["mount"].endswith("/goodscode")
