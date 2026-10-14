"""register_work_hierarchy 쓰기 게이트 오프라인 테스트. 네트워크·DB 없음(urlopen 은 전부 가짜)."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
RATIFIED_IDS_MD5 = "c523a6d044c5d0be31f3469161a66beb"


def _load():
    spec = importlib.util.spec_from_file_location("register_work_hierarchy", HERE / "register_work_hierarchy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load()


class FakeNet:
    """urlopen 대체. 호출을 기록하고 트리/등록 응답을 흉내낸다."""

    def __init__(self, tree_items=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.tree_items = tree_items or []
        self.n = 0

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items())))
        if req.get_method() == "GET":
            payload = {"items": self.tree_items}
        else:
            self.n += 1
            payload = {"work_item": {"id": f"id-{self.n}"}, "idempotent_replay": False}
            if body and body.get("idempotency_key") in getattr(self, "seen", set()):
                payload["idempotent_replay"] = True
            self.seen = getattr(self, "seen", set()) | {body["idempotency_key"]}
        resp = io.BytesIO(json.dumps(payload).encode())
        resp.status = 201 if req.get_method() == "POST" else 200
        resp.__enter__ = lambda self=resp: self
        resp.__exit__ = lambda *a: False
        return resp

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()
    monkeypatch.setattr(reg.urllib.request, "urlopen", fake)
    for k in ("AADS_BEARER_TOKEN", "AADS_ACTOR_SESSION_ID", "AADS_API_BASE", *reg.SERVICE_PRINCIPAL_ENV):
        monkeypatch.delenv(k, raising=False)
    return fake


def _run_apply(tmp_path):
    return reg.apply(reg.build_items(), str(tmp_path / "out.json"))


SCOPE_STRINGS = [
    "anything",
    "x",
    "decision:canonical-work-hierarchy-90-ratification-20261014",
    "495fa90b-f93a-42d0-88f3-296724afb3a6",
    "61410ce2-1830-40d1-a8c3-eb1b8244acd1",
    "CEO ratification tenant=other-tenant project=GO100 goal=00000000-0000-0000-0000-000000000000",
    "service_user=someone-else actor=b749ff17-43d3-4b87-b67f-482b1748b82b",
]


@pytest.mark.parametrize("approval_ref", SCOPE_STRINGS)
@pytest.mark.parametrize("session", ["", "b749ff17-43d3-4b87-b67f-482b1748b82b", "other-session"])
def test_service_principal_blocked_for_any_approval_string(net, monkeypatch, tmp_path, approval_ref, session):
    monkeypatch.setenv("AADS_USE_SERVICE_PRINCIPAL", "1")
    monkeypatch.setenv("AADS_MONITOR_KEY", "dummy-not-a-real-key")
    monkeypatch.setenv("AADS_SERVICE_PRINCIPAL_APPROVAL_REF", approval_ref)
    monkeypatch.setenv("AADS_API_BASE", "http://127.0.0.1:8100/api/v1")
    if session:
        monkeypatch.setenv("AADS_ACTOR_SESSION_ID", session)
    assert _run_apply(tmp_path) == 2
    assert net.calls == []


@pytest.mark.parametrize("env", [
    {"AADS_USE_SERVICE_PRINCIPAL": "1"},
    {"AADS_MONITOR_KEY": "dummy"},
    {"AADS_SERVICE_PRINCIPAL_APPROVAL_REF": "anything"},
])
def test_any_service_env_blocks_even_with_valid_bearer(net, monkeypatch, tmp_path, env):
    monkeypatch.setenv("AADS_BEARER_TOKEN", "dummy-user-jwt")
    monkeypatch.setenv("AADS_ACTOR_SESSION_ID", "s1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert _run_apply(tmp_path) == 2
    assert net.calls == []


def test_no_credentials_blocks(net, tmp_path):
    assert _run_apply(tmp_path) == 2
    assert net.calls == []


def test_bearer_requires_explicit_session_no_ceo_default(net, monkeypatch, tmp_path):
    monkeypatch.setenv("AADS_BEARER_TOKEN", "dummy-user-jwt")
    assert _run_apply(tmp_path) == 2
    assert net.calls == []
    assert not hasattr(reg, "DEFAULT_ACTOR_SESSION")


@pytest.mark.parametrize("base", [
    "http://aads.newtalk.kr/api/v1", "https://aads.newtalk.kr.evil.com/api/v1",
    "https://evil.example/api/v1", "http://127.0.0.1.evil.com/api/v1",
    "http://user:p@127.0.0.1/api/v1", "https://127.0.0.1/api/v1", "https://aads.newtalk.kr@evil.com/api/v1",
])
def test_bearer_not_sent_to_disallowed_base(net, monkeypatch, tmp_path, base):
    monkeypatch.setenv("AADS_BEARER_TOKEN", "dummy-user-jwt")
    monkeypatch.setenv("AADS_ACTOR_SESSION_ID", "s1")
    monkeypatch.setenv("AADS_API_BASE", base)
    assert _run_apply(tmp_path) == 2
    assert net.calls == []


def test_existing_items_stop_before_any_post(net, monkeypatch, tmp_path):
    net.tree_items = [{"id": "w1", "children": [{"id": "w2", "children": []}]}]
    monkeypatch.setenv("AADS_BEARER_TOKEN", "dummy-user-jwt")
    monkeypatch.setenv("AADS_ACTOR_SESSION_ID", "s1")
    assert _run_apply(tmp_path) == 2
    assert net.posts == []
    assert len(net.calls) == 1


def test_bearer_with_explicit_session_is_the_only_write_path(net, monkeypatch, tmp_path):
    monkeypatch.setenv("AADS_BEARER_TOKEN", "dummy-user-jwt")
    monkeypatch.setenv("AADS_ACTOR_SESSION_ID", "s1")
    assert _run_apply(tmp_path) == 0
    assert len(net.posts) == 90 + 90
    for _, _, headers in net.calls:
        lowered = {k.lower(): v for k, v in headers.items()}
        assert "x-monitor-key" not in lowered
        assert lowered["x-chat-session-id"] == "s1"


def test_dry_run_and_read_only_checks_pass(net, capsys):
    items = reg.build_items()
    check = reg.self_check(items)
    assert check["counts"] == {"epic": 9, "story": 27, "task": 54}
    assert check["total"] == 90 and check["unique_keys"] == 90 and check["bad_parent_links"] == []
    sys.argv = ["register_work_hierarchy.py"]
    assert reg.main() == 0
    assert net.calls == []
    assert "unique_keys" in capsys.readouterr().out


def test_result_json_matches_ratified_scope():
    data = json.loads((HERE / "20261014_AADS_업무계층등록결과.json").read_text(encoding="utf-8"))
    ids = sorted(data["created_ids"].values())
    assert len(ids) == len(set(ids)) == 90
    assert hashlib.md5(",".join(ids).encode()).hexdigest() == RATIFIED_IDS_MD5
    assert data["goal_id"] == reg.GOAL_ID
    ratification = data["ratification"]
    assert ratification["decision_entry_id"] == "495fa90b-f93a-42d0-88f3-296724afb3a6"
    assert ratification["grants_new_write_permission"] is False
    assert data["audit"]["original_executor"] == "runner-09547ef0 / service principal (X-Monitor-Key)"
