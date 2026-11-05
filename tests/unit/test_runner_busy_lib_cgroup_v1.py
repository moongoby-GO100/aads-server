"""cgroup v2 단일 / v1 하이브리드(unified) / 없음 — 러너 동기화 preflight 의 cgroup 루트 결정.

실제 maintenance 메타데이터 구현(runner_busy_lib.sh 내장 Python)을 그대로 실행한다.
서비스 소속 정보만 fixture 이고 시스템 서비스에는 아무 영향이 없다.
"""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'scripts/runner_busy_lib.sh'

spec = importlib.util.spec_from_file_location(
    'cgroup_v1_protocol_fixture', ROOT / 'tests/unit/test_runner_maintenance_protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


@pytest.fixture
def module():
    text = LIB.read_text().split("<<'PY_RUNNER_MAINTENANCE'\n", 1)[1].split('\nPY_RUNNER_MAINTENANCE', 1)[0]
    loaded = types.ModuleType('cgroup_v1_maintenance_metadata')
    exec(compile(text, str(LIB), 'exec'), loaded.__dict__)
    return loaded


@pytest.fixture
def host(tmp_path):
    value = protocol.Host(tmp_path)
    try:
        yield value
    finally:
        value.finish()


def show_for(host, actor):
    return {
        'fixture.service': (f'LoadState=loaded\nActiveState=active\nMainPID={actor.pid}\n'
                            f'ControlGroup=/fixture.service\nExecStart={{ path={host.path / "actor.sh"} ; }}\n'),
        'aads-pipeline-runner.service': 'LoadState=not-found\n',
        'aads-pipeline-litellm-runner.service': 'LoadState=not-found\n',
    }


def make_layout(host, kind):
    """systemctl fixture 가 만든 cgroup.procs 를 kind 별 트리로 옮긴다. 루트 경로를 돌려준다."""
    procs = (host.cg / 'fixture.service' / 'cgroup.procs').read_text()
    base = host.path / f'layout-{kind}'
    base.mkdir()
    if kind == 'v2':
        (base / 'cgroup.controllers').write_text('cpu memory pids\n')
        group = base / 'fixture.service'
    elif kind == 'hybrid':
        # v1 하이브리드: 루트는 tmpfs 라 cgroup.controllers 가 없고 v2 뷰는 unified/ 아래에만 있다.
        (base / 'unified').mkdir()
        (base / 'unified' / 'cgroup.controllers').write_text('cpu memory pids\n')
        group = base / 'unified' / 'fixture.service'
    else:
        return base
    group.mkdir()
    (group / 'cgroup.procs').write_text(procs)
    return base


def prime(host):
    actor = host.actor()
    subprocess.run(['systemctl', 'show', 'fixture.service'], env=host.env, check=True, capture_output=True)
    return actor


def test_resolve_v2_single_hierarchy(module, tmp_path, monkeypatch):
    (tmp_path / 'cgroup.controllers').write_text('cpu\n')
    (tmp_path / 'unified').mkdir()
    monkeypatch.setattr(module, 'SYSTEM_CGROUP_ROOT', tmp_path)
    assert module.resolve_cgroup_root(tmp_path) == tmp_path


@pytest.mark.parametrize('with_controllers', [True, False])
def test_resolve_hybrid_uses_unified(module, tmp_path, monkeypatch, with_controllers):
    (tmp_path / 'unified').mkdir()
    if with_controllers:
        (tmp_path / 'unified' / 'cgroup.controllers').write_text('cpu\n')
    monkeypatch.setattr(module, 'SYSTEM_CGROUP_ROOT', tmp_path)
    assert module.resolve_cgroup_root(tmp_path) == tmp_path / 'unified'


def test_resolve_neither_is_deferred_with_cause(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'SYSTEM_CGROUP_ROOT', tmp_path)
    with pytest.raises(module.Deferred, match='cgroup root unresolved') as info:
        module.resolve_cgroup_root(tmp_path)
    assert str(tmp_path / 'cgroup.controllers') in str(info.value)
    assert str(tmp_path / 'unified') in str(info.value)


def test_resolve_keeps_injected_fixture_root_and_rejects_missing(module, tmp_path):
    assert module.resolve_cgroup_root(tmp_path) == tmp_path
    with pytest.raises(module.Deferred, match='cgroup root unresolved'):
        module.resolve_cgroup_root(tmp_path / 'missing')


@pytest.mark.parametrize('kind', ['v2', 'hybrid'])
def test_inventory_is_aware_on_v2_and_hybrid(module, host, kind):
    actor = prime(host)
    base = make_layout(host, kind)
    assert module.inventory(host.state, 'fixture.service', proc=host.proc, cgroups=base,
                            show=show_for(host, actor)) == 'AWARE'


def test_inventory_defers_when_no_cgroup_root(module, host, monkeypatch):
    actor = prime(host)
    base = make_layout(host, 'none')
    monkeypatch.setattr(module, 'SYSTEM_CGROUP_ROOT', base)
    with pytest.raises(module.Deferred, match='cgroup root unresolved'):
        module.inventory(host.state, 'fixture.service', proc=host.proc, cgroups=base,
                         show=show_for(host, actor))


def test_inventory_names_the_missing_cgroup_path(module, host):
    actor = prime(host)
    base = host.path / 'empty-root'
    base.mkdir()
    with pytest.raises(module.Deferred, match='cgroup path missing') as info:
        module.inventory(host.state, 'fixture.service', proc=host.proc, cgroups=base,
                         show=show_for(host, actor))
    assert str(base / 'fixture.service' / 'cgroup.procs') in str(info.value)


def run_cli(host, cgroups):
    code = (f'source {LIB}\n'
            f'runner_maintenance_metadata preflight {host.state} fixture.service {host.proc} {cgroups}\n')
    return subprocess.run(['bash', '-c', code], env=host.env, capture_output=True, text=True, timeout=20)


def test_cli_preflight_stdout_contract_on_hybrid(host):
    prime(host)
    result = run_cli(host, make_layout(host, 'hybrid'))
    assert result.stdout.strip() == 'AWARE', result.stderr
    assert result.returncode == 0


def test_cli_preflight_missing_root_stays_bootstrap_required_with_cause(host):
    prime(host)
    result = run_cli(host, host.path / 'missing-root')
    assert result.stdout.strip() == 'BOOTSTRAP_REQUIRED'
    assert result.returncode == 3
    assert 'cgroup root unresolved' in result.stderr
    assert 'FileNotFoundError' not in result.stderr


def test_cli_oserror_defer_names_the_path(host):
    # 존재하지 않는 state 루트: 예외 이름만이 아니라 어떤 경로였는지 남아야 한다.
    missing = host.path / 'no-state'
    code = (f'source {LIB}\n'
            f'runner_maintenance_metadata preflight {missing} fixture.service {host.proc} {host.cg}\n')
    result = subprocess.run(['bash', '-c', code], env=host.env, capture_output=True, text=True, timeout=20)
    assert result.stdout.strip() == 'BOOTSTRAP_REQUIRED'
    assert str(missing) in result.stderr


def test_live_update_controller_pins_current_library():
    controller = (ROOT / 'scripts/runner_live_update.sh').read_text()
    pins = controller.split("RUNNER_LIVE_COMPANION_PINS='", 1)[1].split("'", 1)[0].split()
    assert hashlib.sha256(LIB.read_bytes()).hexdigest() in pins
    assert '78e55d7ad9dc00395ae3689f9ba374a21d7060cd7e6453bd7e8e1be62c3108ee' in pins



BLOCK_MARKERS = (
    ("<<'PY_RUNNER_LIVE_PROCESS'\n", '\nPY_RUNNER_LIVE_PROCESS'),
    ("<<'PY_RUNNER_MAINTENANCE'\n", '\nPY_RUNNER_MAINTENANCE'),
)


def embedded_blocks():
    text = LIB.read_text()
    return [text.split(start, 1)[1].split(end, 1)[0] for start, end in BLOCK_MARKERS]


# cafe24_114 는 Python 3.8.10 이다. 3.9+ 전용 API 가 한 줄이라도 있으면 inventory 가 UNKNOWN 이 된다.
PY39_ONLY = (
    r'\.removeprefix\(', r'\.removesuffix\(', r'\.is_relative_to\(', r'\.with_stem\(',
    r'\.readlink\(\)', r'\bzoneinfo\b', r'\bmath\.lcm\b', r'\bfunctools\.cache\b', r'@cache\b',
    r'\bast\.unparse\b', r'\bos\.waitstatus_to_exitcode\b', r'\bos\.pidfd_open\b',
    r'\brandom\.randbytes\b', r'\bgraphlib\b', r'\basyncio\.to_thread\b', r'\bitertools\.pairwise\b',
    r'\.bit_count\(', r'\bzip\([^)]*strict=', r'\btomllib\b', r'\bhashlib\.file_digest\b',
)


def test_embedded_python_has_no_py39_only_api():
    import re
    found = []
    for index, block in enumerate(embedded_blocks()):
        for lineno, line in enumerate(block.splitlines(), 1):
            for pattern in PY39_ONLY:
                if re.search(pattern, line):
                    found.append(f'block{index}:{lineno}: {pattern}: {line.strip()}')
    assert not found, found


def test_embedded_python_has_no_py39_only_syntax_or_annotations():
    import ast
    for block in embedded_blocks():
        tree = ast.parse(block, feature_version=(3, 8))
        for node in ast.walk(tree):
            # list[str] / dict[str, int] 같은 내장 제네릭 서브스크립트는 3.9+ 에서만 런타임 평가된다.
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                assert node.value.id not in ('list', 'dict', 'set', 'tuple', 'frozenset', 'type'), node.lineno
            # 어노테이션 안의 `X | Y` 는 3.10+ 에서만 평가된다.
            annotations = []
            if isinstance(node, ast.arg) and node.annotation is not None:
                annotations.append(node.annotation)
            if isinstance(node, ast.FunctionDef) and node.returns is not None:
                annotations.append(node.returns)
            if isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
            for annotation in annotations:
                assert not any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                               for n in ast.walk(annotation)), annotation.lineno


def test_embedded_python_parses_under_python38():
    import shutil
    py38 = shutil.which('python3.8')
    if not py38:
        pytest.skip('python3.8 not installed on this host; static 3.9+ API scan covers it')
    for block in embedded_blocks():
        result = subprocess.run([py38, '-c', 'import ast, sys; ast.parse(sys.stdin.read())'],
                                input=block, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr


def test_deleted_cwd_suffix_strip_is_38_compatible_and_equivalent():
    suffix = ' (deleted)'

    def strip(cwd):
        return cwd[:-len(suffix)] if cwd.endswith(suffix) else cwd

    for cwd in ('/srv/app (deleted)', '/srv/app', '/srv/app (deleted) (deleted)', '(deleted)', ' (deleted)', ''):
        assert strip(cwd) == (cwd[:-len(suffix)] if cwd.endswith(suffix) else cwd)
    assert strip('/srv/app (deleted)') == '/srv/app'
    assert strip('/srv/app') == '/srv/app'
    assert strip('/srv/app (deleted) (deleted)') == '/srv/app (deleted)'
    assert strip(' (deleted)') == ''
    assert "cwd[:-len(' (deleted)')] if cwd.endswith(' (deleted)') else cwd" in LIB.read_text()

