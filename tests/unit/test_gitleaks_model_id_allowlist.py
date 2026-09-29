"""`.gitleaks.toml` 회귀시험 — 공개 모델 id 오탐은 풀고, 실제 키 차단은 유지한다.

배경: 2026-09-29 runner-7d3e75f0 이 `"model_key": "claude-haiku-4-5-20251001"` 한 줄 때문에
gitleaks generic-api-key 에 막혀 승인 산출물이 폐기됐다(오류 사전
git.gitleaks_blocks_masking_test_fixture 재발 22회째).

세 층으로 나눈다.
  1. 설정 구조 — 기본 룰셋 확장, allowlist 가 좁은지 (gitleaks 없이 항상 돈다).
  2. allowlist 정규식 의미 — 모델 id 는 통과, 실제 키 모양은 통과 못 함 (항상 돈다).
  3. gitleaks 실동작 — 실제 바이너리로 스테이징 후 스캔. 바이너리가 없는 환경(운영 이미지)
     에서는 건너뛴다. 게이트가 켜진 환경이면 AADS_REQUIRE_GITLEAKS=1 로 강제한다.

가짜 키는 런타임에 조립한다 — 이 파일 자체가 키 스캔에 걸리면 안 된다.
"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CONFIG = _REPO / ".gitleaks.toml"
_HOOK = _REPO / "scripts" / "hooks" / "pre-commit"
_GITLEAKS = shutil.which("gitleaks")
_GIT = shutil.which("git")
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

_MODEL_IDS = [
    "claude-haiku-4-5-20251001",
    "claude-opus-5-5",
    "claude-sonnet-5",
    "claude-fable-5-1",
    "claude-3-5-sonnet-20241022",
    "gpt-4o-mini",
    "gpt-5.1-codex",
    "gemini-2.5-flash",
    "gemini-3-pro-preview",
    "deepseek-chat",
    "deepseek-reasoner",
    "gemini/gemini-2.5-flash",
    "anthropic/claude-opus-5-5",
]


def _rand(n: int, seed: int = 7) -> str:
    r = random.Random(seed)
    return "".join(r.choice(_ALPHABET) for _ in range(n))


def _fake_real_keys() -> dict[str, str]:
    """실제 제공사 키 형식을 따르는 가짜 값. 이름 → 값."""
    return {
        "anthropic": "sk-ant-" + "api03-" + _rand(93, 1) + "AA",
        "openai": "sk-" + "proj-" + _rand(74, 2) + "T3Blb" + "kFJ" + _rand(74, 3),
        "gemini": "AIza" + _rand(35, 4),
        "github": "gh" + "p_" + _rand(36, 5).replace("-", "a").replace("_", "b"),
        "aws": "AK" + "IA" + "IOSFODNN7EXAMPLQ",
    }


def _config() -> dict:
    return tomllib.loads(_CONFIG.read_text(encoding="utf-8"))


def _allow_regexes() -> list[re.Pattern]:
    out = []
    for al in _config()["allowlists"]:
        out.extend(re.compile(p) for p in al.get("regexes", []))
    return out


# ── 1. 설정 구조 ─────────────────────────────────────────────────────


def test_config_extends_default_ruleset():
    cfg = _config()
    assert cfg["extend"]["useDefault"] is True, "기본 룰셋을 끄면 보안 게이트가 사라진다"
    assert "rules" not in cfg, "룰을 통째로 대체하지 말고 기본 룰셋을 확장만 한다"


def test_allowlist_is_narrow():
    """파일·경로·커밋 통째 제외 금지, generic-api-key 한 룰에만."""
    lists = _config()["allowlists"]
    assert lists, "모델 id allowlist 가 있어야 한다"
    for al in lists:
        assert al.get("targetRules") == ["generic-api-key"]
        for broad in ("paths", "commits", "stopwords"):
            assert not al.get(broad), "%s 로 넓게 빼면 게이트가 무력화된다" % broad
        assert al.get("regexTarget", "secret") == "secret"
        assert al.get("regexes")


def test_no_top_level_path_allowlist():
    cfg = _config()
    assert "allowlist" not in cfg, "전역 allowlist(paths 등) 금지 — [[allowlists]] 만 쓴다"


# ── 2. allowlist 정규식 의미 ─────────────────────────────────────────


@pytest.mark.parametrize("model_id", _MODEL_IDS)
def test_allow_regex_accepts_public_model_ids(model_id):
    assert any(p.search(model_id) for p in _allow_regexes()), model_id


def test_allow_regex_rejects_real_key_formats():
    for name, value in _fake_real_keys().items():
        assert not any(p.search(value) for p in _allow_regexes()), name


@pytest.mark.parametrize(
    "value",
    [
        "gpt-" + "Zk3fQ9vL2mXp7RtYw4NcB8dHsJ6aUeVq",  # 대문자 섞인 난수
        "claude-" + "a" * 40,  # 토큰 길이 초과
        "sk-claude-opus-5-5",  # 계열명으로 시작하지 않음
        "claude-opus-5-5 extra",  # 값 전체가 모델 id 가 아님
        "claude-",  # 본문 없음
    ],
)
def test_allow_regex_rejects_lookalikes(value):
    assert not any(p.search(value) for p in _allow_regexes()), value


# ── 3. gitleaks 실동작 ───────────────────────────────────────────────

_need_gitleaks = pytest.mark.skipif(
    not (_GITLEAKS and _GIT) and os.environ.get("AADS_REQUIRE_GITLEAKS") != "1",
    reason="gitleaks/git 없음 — 실동작 검증 불가(설정 구조·정규식 시험은 위에서 수행됨)",
)


def _stage_and_scan(tmp_path: Path, files: dict[str, str], with_config: bool = True):
    assert _GITLEAKS and _GIT, "AADS_REQUIRE_GITLEAKS=1 인데 gitleaks/git 이 없다"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def run(*cmd):
        return subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True)

    assert run(_GIT, "init", "-q", ".").returncode == 0
    if with_config:
        shutil.copy(_CONFIG, tmp_path / ".gitleaks.toml")
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert run(_GIT, "add", "-A").returncode == 0
    return run(_GITLEAKS, "git", "--staged", "--no-banner", "--redact", "-v")


@_need_gitleaks
def test_staged_model_id_literals_pass(tmp_path):
    src = (
        'row = {"call_source": "anthropic_client", "model_key": "claude-haiku-4-5-20251001", "calls": 3}\n'
        'MODELS = ["claude-opus-5-5", "gpt-4o-mini", "gemini-2.5-flash", "deepseek-chat"]\n'
        'api_key_model = "claude-opus-5-5"\n'
    )
    res = _stage_and_scan(tmp_path, {"sample.py": src})
    assert res.returncode == 0, res.stdout + res.stderr


@_need_gitleaks
def test_staged_real_key_formats_still_blocked(tmp_path):
    """이게 깨지면 보안 게이트가 무력화된 것이다."""
    for name, value in _fake_real_keys().items():
        repo = tmp_path / name
        repo.mkdir()
        res = _stage_and_scan(repo, {"leak.py": 'TOKEN = "%s"\n' % value})
        assert res.returncode == 1, "%s 형식 키가 차단되지 않았다: %s" % (name, res.stdout + res.stderr)


@_need_gitleaks
def test_staged_random_value_named_like_model_still_blocked(tmp_path):
    """`gpt-` 접두만 붙인 난수 값은 모델 id 가 아니므로 계속 차단된다."""
    src = 'api_key = "gpt-%s"\n' % "Zk3fQ9vL2mXp7RtYw4NcB8dHsJ6aUeVq1oGiKlMn0PxCz"
    res = _stage_and_scan(tmp_path, {"leak.py": src})
    assert res.returncode == 1, res.stdout + res.stderr


@_need_gitleaks
def test_scan_without_config_file_still_runs(tmp_path):
    """`.gitleaks.toml` 이 없어도 gitleaks 는 기본 룰로 죽지 않고 돈다."""
    res = _stage_and_scan(tmp_path, {"ok.py": "x = 1\n"}, with_config=False)
    assert res.returncode == 0, res.stdout + res.stderr


# ── 훅: 설정·바이너리가 없어도 죽지 않는다 ───────────────────────────


def _hook_gitleaks_block() -> str:
    text = _HOOK.read_text(encoding="utf-8")
    start = text.index("# ─── Step 1b: gitleaks 2차 스캔")
    end = text.index("# ─── Step 1c:")
    return text[start:end]


def test_hook_gitleaks_step_warns_instead_of_dying_when_binary_missing(tmp_path):
    block = _hook_gitleaks_block()
    script = "RED=''; YELLOW=''; NC=''\n" + block + '\necho "REACHED_END"\n'
    bash = shutil.which("bash")
    assert bash
    res = subprocess.run(
        [bash, "-c", script],
        cwd=tmp_path,
        env={"PATH": str(tmp_path), "HOME": str(tmp_path)},  # gitleaks 없는 PATH
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "gitleaks 미설치" in res.stdout
    assert "REACHED_END" in res.stdout


def test_hook_still_calls_gitleaks_with_default_config_discovery():
    """훅은 --config 를 지정하지 않는다 — 저장소 루트의 .gitleaks.toml 이 자동 적용된다."""
    block = _hook_gitleaks_block()
    assert "gitleaks git --staged --no-banner --redact -v" in block
    assert "--config" not in block
    assert "command -v gitleaks" in block
