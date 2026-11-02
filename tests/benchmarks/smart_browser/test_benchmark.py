import copy
import hashlib
import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from jsonschema import Draft202012Validator, ValidationError

sys.path.insert(0, str(Path(__file__).parent))
import benchmark as b
from fixture_server import FixtureServer, Handler


@pytest.fixture(params=["inprocess", "http"])
def server(request):
    if request.param == "inprocess":
        state = SimpleNamespace()
        state.reset = lambda: FixtureServer.reset(state)
        state.reset()
        yield state
        return
    try:
        instance = FixtureServer()
    except PermissionError as exc:
        pytest.skip(f"localhost socket denied by sandbox: {exc}")
    with instance as s:
        thread = threading.Thread(target=s.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{s.server_port}"
        s.shutdown()
        thread.join()


def get(base, path, method="GET"):
    if not isinstance(base, str):
        handler = object.__new__(Handler)
        handler.server = base
        handler.path = path
        handler.wfile = io.BytesIO()
        status = []
        handler.send_response = status.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        getattr(handler, "do_" + method)()
        return status[0], handler.wfile.getvalue()
    try:
        with urlopen(Request(base + path, method=method), timeout=3) as r:
            return r.status, r.read()
    except HTTPError as e:
        return e.code, e.read()


def self_test(tmp_path):
    # Deliberately synthetic evidence: never supplied as actual adapter performance.
    (tmp_path / "synthetic.txt").write_text("evaluator unit test only")
    artifact = {
        "path": "synthetic.txt",
        "sha256": hashlib.sha256((tmp_path / "synthetic.txt").read_bytes()).hexdigest(),
    }
    data = b.not_run("browser_agent")
    data["mode"] = "self_test"
    data["adapter_version"] = "unit-test"
    oracle = json.loads((b.ROOT / "private/oracle.json").read_text())
    for r in data["records"]:
        r.update(
            status="passed",
            reason=None,
            answer=oracle[r["fixture_id"]],
            wall_seconds=1.25,
            interventions=0,
            recovery=True,
            artifacts=[artifact],
        )
    return data


def test_all_schema_and_input_contracts():
    for name in ["manifest", "request", "result"]:
        Draft202012Validator.check_schema(json.loads((b.ROOT / f"{name}.schema.json").read_text()))
    Draft202012Validator(json.loads((b.ROOT / "manifest.schema.json").read_text())).validate(
        b.MANIFEST
    )
    ids = [f["id"] for f in b.MANIFEST["fixtures"]]
    assert len(ids) == len(set(ids)) == 20
    assert set(ids) == set(json.loads((b.ROOT / "private/oracle.json").read_text()))
    validator = Draft202012Validator(json.loads((b.ROOT / "request.schema.json").read_text()))
    for f in b.MANIFEST["fixtures"]:
        for t in range(1, 4):
            r = b.request(f, "http://127.0.0.1:8765", t)
            validator.validate(r)
            assert "answer" not in json.dumps(r) and "oracle" not in json.dumps(r)
    with pytest.raises(ValueError):
        b.request(b.MANIFEST["fixtures"][0], "https://example.com", 1)


def test_unimplemented_adapters():
    for adapter in ["browser_agent", "Aside"]:
        result = b.evaluate(b.not_run(adapter))
        assert result["counts"] == {"not_run": 60}
        assert result["denominator"] == 60 and result["accuracy"] is None
        assert result["cost_usd"] is None and result["wall_seconds_measured"] is None
    assert b.plan()["kind"] == "plan_not_measurement"
    assert b.plan()["trials"][1]["order"] == ["Aside", "browser_agent"]


def test_oracle_mismatch_and_mock_guard(tmp_path):
    data = self_test(tmp_path)
    with pytest.raises(ValueError, match="self-tests"):
        b.evaluate(data, tmp_path)
    assert b.evaluate(data, tmp_path, True)["kind"] == "evaluator_self_test"
    data["records"][0]["answer"] = {"wrong": True}
    with pytest.raises(ValueError, match="oracle mismatch"):
        b.evaluate(data, tmp_path, True)


@pytest.mark.parametrize("status", ["failed", "cancelled", "blocked", "crashed"])
def test_failures_in_denominator(tmp_path, status):
    data = self_test(tmp_path)
    data["records"][0].update(status=status, reason="injected", answer=None, recovery=False)
    result = b.evaluate(data, tmp_path, True)
    assert result["counts"][status] == 1 and result["denominator"] == 60
    assert result["accuracy"] == 59 / 60 and result["cost_usd"] is None
    assert result["recovery_observed"]["false"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "unknown",
        "status",
        "extra",
        "hash",
        "permissions",
        "seed",
        "reset",
        "version",
        "artifact",
        "time",
        "cost",
        "nan",
        "inf",
        "interventions",
        "not-run-measured",
        "no-artifact",
        "no-time",
        "reason",
    ],
)
def test_reject_invalid(tmp_path, mutation):
    data = self_test(tmp_path)
    r = data["records"][0]
    if mutation == "missing":
        data["records"].pop()
    elif mutation == "duplicate":
        data["records"][1] = copy.deepcopy(r)
    elif mutation == "unknown":
        r["fixture_id"] = "unknown"
    elif mutation == "status":
        r["status"] = "success-ish"
    elif mutation == "extra":
        r["oracle"] = "leak"
    elif mutation == "hash":
        r["input_hash"] = "0" * 64
    elif mutation == "permissions":
        r["permissions"]["project"] = "B"
    elif mutation == "seed":
        r["seed"] = 0
    elif mutation == "reset":
        r["reset"] = "none"
    elif mutation == "version":
        r["fixture_version"] = "2"
    elif mutation == "artifact":
        r["artifacts"][0]["path"] = "../oracle.json"
    elif mutation == "time":
        r["wall_seconds"] = -1
    elif mutation == "cost":
        r["cost_usd"] = -1
    elif mutation == "nan":
        r["wall_seconds"] = float("nan")
    elif mutation == "inf":
        r["cost_usd"] = float("inf")
    elif mutation == "interventions":
        r["interventions"] = -1
    elif mutation == "not-run-measured":
        r["status"] = "not_run"
    elif mutation == "no-artifact":
        r["artifacts"] = []
    elif mutation == "no-time":
        r["wall_seconds"] = None
    elif mutation == "reason":
        r.update(status="failed", reason=None)
    with pytest.raises((ValueError, ValidationError)):
        b.evaluate(data, tmp_path, True)


def test_artifact_hash(tmp_path):
    data = self_test(tmp_path)
    (tmp_path / "synthetic.txt").write_text("changed")
    with pytest.raises(ValueError, match="digest"):
        b.evaluate(data, tmp_path, True)


def test_http_all_fixtures_and_oracle_isolation(server):
    for f in b.MANIFEST["fixtures"]:
        assert get(server, "/reset", "POST")[0] == 200
        code, body = get(server, f["inputs"]["path"])
        assert code == 200 and b"<!doctype html>" in body
        assert b"oracle" not in body
    for path in [
        "/private/oracle.json",
        "/manifest.json",
        "/result.schema.json",
        "/../private/oracle.json",
        "/%2e%2e/private/oracle.json",
        "/fixture/table/../../private/oracle.json",
        "/evidence/source-verification.json",
    ]:
        assert get(server, path)[0] == 404


def test_reset_reproducible(server):
    for f in b.MANIFEST["fixtures"]:
        get(server, "/reset", "POST")
        first = get(server, f["inputs"]["path"])
        get(server, "/control/submit", "POST")
        get(server, "/reset", "POST")
        assert get(server, f["inputs"]["path"]) == first


def test_faults_and_scope_cursor(server):
    assert get(server, "/control/checkpoint", "POST")[0] == 200
    assert get(server, "/control/crash", "POST")[0] == 503
    assert json.loads(get(server, "/control/resume", "POST")[1]) == {
        "value": "saved-17",
        "effects": 1,
    }
    get(server, "/reset", "POST")
    assert get(server, "/control/ack-loss", "POST")[0] == 504
    assert json.loads(get(server, "/receipt")[1])["effects"] == 1
    get(server, "/reset", "POST")
    a = get(server, "/control/submit", "POST")
    assert get(server, "/control/submit", "POST") == a
    assert get(server, "/control/submit?payload=changed", "POST")[0] == 409
    assert json.loads(get(server, "/receipt")[1])["effects"] == 1
    get(server, "/reset", "POST")
    get(server, "/control/cancel", "POST")
    assert get(server, "/control/submit", "POST")[0] == 409
    assert json.loads(get(server, "/receipt")[1])["effects"] == 0
    assert get(server, "/scope?project=B")[0] == 403
    assert get(server, "/cursor?cursor=B")[0] == 400
    assert json.loads(get(server, "/cursor?cursor=A")[1])["ids"] == [3, 4]
    assert json.loads(get(server, "/events?after=2")[1])["event_seq"] == [3, 4]
    csv = get(server, "/download.csv")[1]
    oracle = json.loads((b.ROOT / "private/oracle.json").read_text())
    assert hashlib.sha256(csv).hexdigest() == oracle["download"]["sha256"]


def test_secret_in_artifact_rejected(tmp_path):
    data = self_test(tmp_path)
    (tmp_path / "synthetic.txt").write_text("SYNTHETIC-SECRET-17")
    data["records"][0]["artifacts"][0]["sha256"] = hashlib.sha256(
        (tmp_path / "synthetic.txt").read_bytes()
    ).hexdigest()
    with pytest.raises(ValueError, match="unredacted"):
        b.evaluate(data, tmp_path, True)


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:8765@192.0.2.1', 'http://user:pass@127.0.0.1:8765',
    'http://127.0.0.1:8765@127.0.0.1:8765', 'http://192.0.2.1:8765',
    'https://127.0.0.1:8765', 'http://localhost:8765', 'http://127.0.0.1',
    'http://127.0.0.1:', 'http://127.0.0.1:abc', 'http://127.0.0.1:0',
    'http://127.0.0.1:-1', 'http://127.0.0.1:65536', 'http://127.0.0.1:08765',
    'http://127.0.0.1:8765/', 'http://127.0.0.1:8765/path',
    'http://127.0.0.1:8765?', 'http://127.0.0.1:8765?q=x',
    'http://127.0.0.1:8765#', 'http://127.0.0.1:8765#fragment',
    'http://127.0.0.1:8765\n', ' http://127.0.0.1:8765',
    'http://127.0.0.1:87\t65', 'http://[127.0.0.1:8765', None, 123,
])
def test_request_rejects_invalid_origins_without_network(url, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail('request validation attempted network access')

    monkeypatch.setattr(socket, 'socket', forbidden)
    fixture = b.MANIFEST['fixtures'][0]
    with pytest.raises((ValueError, ValidationError)):
        b.request(fixture, url, 1)
    record = b.request(fixture, 'http://127.0.0.1:8765', 1)
    record['base_url'] = url
    assert not Draft202012Validator(b.REQUEST_SCHEMA).is_valid(record)


@pytest.mark.parametrize('port', [1, 9, 80, 8765, 9999, 10000, 59999, 60000,
                                  64999, 65000, 65499, 65500, 65529, 65530, 65535])
def test_request_valid_port_boundaries(port):
    record = b.request(b.MANIFEST['fixtures'][0], f'http://127.0.0.1:{port}', 1)
    Draft202012Validator(b.REQUEST_SCHEMA).validate(record)


@pytest.mark.parametrize('trial', [0, 4, -1, True, None, '1', 1.5, float('nan')])
def test_request_validates_trial(trial):
    with pytest.raises((ValueError, ValidationError)):
        b.request(b.MANIFEST['fixtures'][0], 'http://127.0.0.1:8765', trial)


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'permissions', 'seed'])
def test_request_validates_fixture(mutation):
    fixture = copy.deepcopy(b.MANIFEST['fixtures'][0])
    if mutation == 'missing':
        fixture.pop('inputs')
    elif mutation == 'extra':
        fixture['answer'] = 'oracle'
    elif mutation == 'permissions':
        fixture['permissions']['actions'] = 'invalid'
    else:
        fixture['seed'] = 'invalid'
    with pytest.raises(ValidationError):
        b.request(fixture, 'http://127.0.0.1:8765', 1)


@pytest.mark.parametrize('failure', ['absent', 'absent_shell', 'dependencies', 'launch', 'assertion', 'runtime', 'passed'])
def test_browser_smoke_failure_classification(tmp_path, monkeypatch, capsys, failure):
    from contextlib import nullcontext

    import browser_smoke

    class BrowserError(Exception):
        pass

    executable = tmp_path / 'chromium'
    if failure != 'absent':
        executable.touch()
    closed = []

    class Page:
        def goto(self, url):
            if failure == 'runtime':
                raise OSError('runtime failure')

        def get_by_text(self, *args, **kwargs):
            return self

        get_by_role = get_by_text
        locator = get_by_text

        def wait_for(self):
            pass

        click = wait_for

        def is_visible(self):
            return failure == 'assertion'

        def inner_text(self):
            return 'Detail 17'

    browser = SimpleNamespace(new_page=Page, close=lambda: closed.append(True))

    def launch(**kwargs):
        if failure == 'absent_shell':
            raise BrowserError("Executable doesn't exist at /missing/headless-shell")
        if failure in ('dependencies', 'launch'):
            raise BrowserError('Host system is missing dependencies' if failure == 'dependencies'
                               else 'unexpected launch failure')
        return browser

    chromium = SimpleNamespace(executable_path=str(executable), launch=launch)
    api = SimpleNamespace(Error=BrowserError,
                          sync_playwright=lambda: nullcontext(SimpleNamespace(chromium=chromium)))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', api)
    server = SimpleNamespace(server_port=8765, serve_forever=lambda: None, shutdown=lambda: None)
    monkeypatch.setattr(browser_smoke, 'FixtureServer', lambda: nullcontext(server))
    if failure in ('launch', 'assertion', 'runtime'):
        error = {'launch': BrowserError, 'assertion': AssertionError, 'runtime': OSError}[failure]
        with pytest.raises(error):
            browser_smoke.main()
        assert 'not_run' not in capsys.readouterr().out
    else:
        assert browser_smoke.main() == (0 if failure == 'passed' else 2)
        assert json.loads(capsys.readouterr().out)['status'] == (
            'passed' if failure == 'passed' else 'not_run')
    if failure in ('assertion', 'runtime', 'passed'):
        assert closed == [True]
