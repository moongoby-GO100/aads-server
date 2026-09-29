from scripts import qwen3_embedding_worker as worker


def test_parse_mem_available_converts_kb_to_mb():
    text = "MemTotal:       16000000 kB\nMemFree:          100000 kB\nMemAvailable:    9991308 kB\n"
    assert worker.parse_mem_available_mb(text) == 9757


def test_parse_returns_none_when_missing_or_invalid():
    assert worker.parse_mem_available_mb("MemTotal: 1 kB\nMemFree: 2 kB\n") is None
    assert worker.parse_mem_available_mb("MemAvailable: abc kB\n") is None
    assert worker.parse_mem_available_mb("MemAvailable:\n") is None
    assert worker.parse_mem_available_mb("") is None


def test_read_available_mb_uses_meminfo_file(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text("MemAvailable:    9991308 kB\n")
    assert worker.read_available_mb(str(path)) == 9757


def test_read_available_mb_falls_back_when_file_missing(tmp_path):
    value = worker.read_available_mb(str(tmp_path / "nope"))
    assert isinstance(value, int) and value >= 0


def test_read_available_mb_falls_back_when_entry_missing(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text("MemTotal: 1 kB\n")
    value = worker.read_available_mb(str(path))
    assert isinstance(value, int) and value >= 0


def test_read_available_mb_returns_zero_when_sysconf_fails(tmp_path, monkeypatch):
    def boom(_name):
        raise ValueError("unsupported")

    monkeypatch.setattr(worker.os, "sysconf", boom)
    assert worker.read_available_mb(str(tmp_path / "nope")) == 0


def test_read_available_mb_returns_zero_on_oserror(tmp_path, monkeypatch):
    def boom(_name):
        raise OSError("fail")

    monkeypatch.setattr(worker.os, "sysconf", boom)
    assert worker.read_available_mb(str(tmp_path / "nope")) == 0


def test_desired_concurrency_regression_on_real_available_memory():
    assert worker.desired_concurrency(1.53, 8, 378, 0.003) == 0
    assert worker.desired_concurrency(1.53, 8, 9757, 0.003) >= 1
