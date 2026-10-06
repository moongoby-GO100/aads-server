"""Real Bash/flock/PID protocol regressions; only service/proc membership is a fixture.

No service is signalled. Only subprocesses created by these tests are terminated.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'scripts/runner_busy_lib.sh'


def wait_for(check, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('fixture condition did not become true')


@pytest.fixture
def module():
    text = LIB.read_text().split("<<'PY_RUNNER_MAINTENANCE'\n", 1)[1].split('\nPY_RUNNER_MAINTENANCE', 1)[0]
    loaded = types.ModuleType('actual_maintenance_metadata')
    exec(compile(text, str(LIB), 'exec'), loaded.__dict__)
    return loaded


class Host:
    def __init__(self, tmp):
        self.path = tmp
        self.state = tmp / 'state'
        self.proc = tmp / 'proc'
        self.cg = tmp / 'cgroup'
        self.bin = tmp / 'bin'
        for p in (self.proc, self.cg, self.bin):
            p.mkdir()
        (self.proc / 'sys').symlink_to('/proc/sys', target_is_directory=True)
        self.pids = []
        (tmp / 'runner_busy_lib.sh').write_bytes(LIB.read_bytes())
        self.env = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                    'FIXTURE': str(tmp), 'PROTOCOL_LIBRARY': str(LIB)}
        # Only OS service membership is supplied. PID identities, fds, cmdlines,
        # lock ownership and lifetime are read from actual local subprocesses.
        systemctl = self.bin / 'systemctl'
        systemctl.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
root=Path(os.environ['FIXTURE'])
if sys.argv[1] != 'show':
    with (root/'systemctl-mutations').open('a') as f: f.write(' '.join(sys.argv[1:])+'\\n')
    sys.exit(0)
unit=sys.argv[2]
if unit != 'fixture.service':
    print('LoadState=not-found\\nActiveState=inactive\\nMainPID=0\\nControlGroup=')
    sys.exit(0)
pid=int((root/'main.pid').read_text())
rows={}
for p in Path('/proc').iterdir():
    if not p.name.isdigit(): continue
    try:
        fields=(p/'stat').read_text().rsplit(')',1)[1].split()
        if fields[0] not in ('Z','X'): rows[int(p.name)]=int(fields[1])
    except FileNotFoundError: pass
members={pid}
while True:
    nxt=members|{p for p,parent in rows.items() if parent in members}
    if nxt==members: break
    members=nxt
for p in members:
    target=root/'proc'/str(p)
    if not target.exists() and not target.is_symlink(): target.symlink_to('/proc/'+str(p),target_is_directory=True)
group=root/'cgroup'/'fixture.service';group.mkdir(exist_ok=True)
(group/'cgroup.procs').write_text('\\n'.join(map(str,members)))
source=(root/'state'/'actors'/f'{pid}.json')
import json
actor=json.loads(source.read_text())
print(f"LoadState=loaded\\nActiveState=active\\nMainPID={pid}\\nControlGroup=/fixture.service\\nExecStart={{ path={actor['source']} ; }}")
''')
        systemctl.chmod(0o755)
        self.library = f'source {shlex.quote(str(LIB))}\n'
        # Real metadata implementation accepts proc/cgroup fixture roots. The
        # shell transaction itself and every flock invocation remain unchanged.
        self.controller_library = self.library + '''eval "$(declare -f runner_maintenance_metadata | sed '1s/runner_maintenance_metadata/actual_metadata/')"
runner_maintenance_metadata() {
    if [[ "$1" == preflight || "$1" == quiescent ]]; then
        actual_metadata "$@" "$FIXTURE/proc" "$FIXTURE/cgroup"
    else
        actual_metadata "$@"
    fi
}
'''

    def actor(self, worker=False, orphan=False, drop_lease=False, worker_cwd=None, fault_metadata=False, name='actor.sh'):
        path = self.path / name
        code = self.library + f'''
set -e
runner_maintenance_enter {shlex.quote(str(self.state))} "$0"
declare -A _bg_jobs
_reap_bg_jobs() {{
 for pid in "${{!_bg_jobs[@]}}"; do
  if ! kill -0 "$pid" 2>/dev/null; then wait "$pid" || true; unset '_bg_jobs[$pid]'; fi
 done
}}
printf '%s' "$$" > "$FIXTURE/main.pid"
touch "$FIXTURE/ready"
'''
        if worker or orphan:
            code += '''( touch "$FIXTURE/worker-ready"; while [[ ! -f "$FIXTURE/worker-done" ]]; do sleep .02; done ) &
printf '%s' "$!" > "$FIXTURE/worker.pid"
'''
            if worker_cwd is not None:
                code = code.replace('( touch "$FIXTURE/worker-ready"', '( cd ' + shlex.quote(str(worker_cwd)) + '; touch "$FIXTURE/worker-ready"')
            if drop_lease:
                code = code.replace('touch "$FIXTURE/worker-ready";', 'exec 200>&-; touch "$FIXTURE/worker-ready";')
            if not orphan:
                code += '_bg_jobs[$!]=job\n'
        if fault_metadata:
            code += 'runner_maintenance_record() { return 3; }\n'
        code += '''while [[ ! -f "$FIXTURE/stop" ]]; do
 runner_maintenance_checkpoint_or_hold
 printf '.\n' >> "$FIXTURE/claims"
 sleep .02
done
'''
        path.write_text(code)
        log = (self.path / (name + '.log')).open('w')
        process = subprocess.Popen(['bash', str(path)], env=self.env, stdout=log, stderr=log)
        self.pids.append((process, log))
        wait_for(lambda: (self.path / 'ready').exists())
        if worker or orphan:
            wait_for(lambda: (self.path / 'worker-ready').exists())
        return process

    def controller(self, timeout='3', callback='printf applied >> "$FIXTURE/applied"'):
        code = self.controller_library + f'''
apply() {{ {callback}; }}
runner_maintenance_transaction fixture.service {timeout} {shlex.quote(str(self.state))} apply
'''
        p = subprocess.Popen(['bash', '-c', code], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.pids.append((p, None))
        return p

    def finish(self):
        (self.path / 'worker-done').touch()
        (self.path / 'stop').touch()
        for process, stream in reversed(self.pids):
            if process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=3)
            if stream:
                stream.close()


@pytest.fixture
def host(tmp_path):
    value = Host(tmp_path)
    try:
        yield value
    finally:
        value.finish()


def test_actual_worker_finishes_before_exclusive_apply_and_daemon_stays_alive(host):
    actor = host.actor(worker=True)
    controller = host.controller()
    time.sleep(.25)
    assert controller.poll() is None
    assert not (host.path / 'applied').exists()
    assert actor.poll() is None
    (host.path / 'worker-done').touch()
    stdout, stderr = controller.communicate(timeout=6)
    assert controller.returncode == 0, stdout + stderr
    assert (host.path / 'applied').read_text() == 'applied'
    assert actor.poll() is None
    wait_for(lambda: json.loads((host.state/'actors'/f'{actor.pid}.json').read_text())['phase'] == 'RUNNING')


def test_orphan_inherited_descriptor_blocks_even_after_daemon_closes_own_fd(host):
    actor = host.actor(orphan=True)
    controller = host.controller(timeout='.4')
    wait_for(lambda: json.loads((host.state/'actors'/f'{actor.pid}.json').read_text())['phase'] == 'QUIESCENT' and not Path(f'/proc/{actor.pid}/fd/200').exists())
    assert not Path(f'/proc/{actor.pid}/fd/200').exists()
    worker = int((host.path/'worker.pid').read_text())
    assert Path(f'/proc/{worker}/fd/200').exists()
    stdout, stderr = controller.communicate(timeout=4)
    assert controller.returncode == 3, stdout + stderr
    assert not (host.path / 'applied').exists()
    assert actor.poll() is None
    assert Path(f'/proc/{worker}').exists()
    wait_for(lambda: json.loads((host.state/'actors'/f'{actor.pid}.json').read_text())['phase'] == 'RUNNING')


def test_controller_termination_releases_admission_without_requeue_or_actor_exit(host):
    actor = host.actor(orphan=True)
    controller = host.controller(timeout='5')
    wait_for(lambda: json.loads((host.state/'actors'/f'{actor.pid}.json').read_text())['phase'] == 'QUIESCENT')
    # Terminate only this fixture process, including its blocking flock child.
    children = Path(f'/proc/{controller.pid}/task/{controller.pid}/children').read_text().split()
    controller.terminate()
    for child in children:
        os.kill(int(child), 15)
    controller.wait(timeout=3)
    wait_for(lambda: json.loads((host.state/'actors'/f'{actor.pid}.json').read_text())['phase'] == 'RUNNING')
    assert actor.poll() is None
    assert not (host.path/'applied').exists()


def test_exclusive_controller_serializes_second_controller_and_new_actor(host):
    actor = host.actor()
    first = host.controller(callback='touch "$FIXTURE/apply-start"; while [[ ! -f "$FIXTURE/apply-done" ]]; do sleep .02; done; touch "$FIXTURE/applied"')
    wait_for(lambda: (host.path/'apply-start').exists())
    second = host.controller(timeout='.3', callback='touch "$FIXTURE/second-applied"')
    code = host.library + f'runner_maintenance_enter {shlex.quote(str(host.state))} "$0"\ntouch "$FIXTURE/new-admitted"\n'
    path = host.path/'new-actor.sh';path.write_text(code)
    child=subprocess.Popen(['bash',str(path)],env=host.env);host.pids.append((child,None))
    stdout, stderr=second.communicate(timeout=3)
    assert second.returncode==3,stdout+stderr
    assert not (host.path/'second-applied').exists()
    assert not (host.path/'new-admitted').exists()
    (host.path/'apply-done').touch()
    assert first.wait(timeout=3)==0
    assert child.wait(timeout=3)==0
    assert (host.path/'new-admitted').exists()
    assert actor.poll() is None


@pytest.mark.parametrize('field,value', [('boot_id','stale'),('start_ticks','0'),('pid',1),('protocol',0),('source','/untrusted/source'),('source_sha256','0'*64),('library_sha256','invalid')])
def test_stale_or_forged_actor_identity_never_applies(host, field, value):
    actor=host.actor()
    path=host.state/'actors'/f'{actor.pid}.json'
    data=json.loads(path.read_text());data[field]=value;path.write_text(json.dumps(data))
    controller=host.controller(timeout='.2');stdout,stderr=controller.communicate(timeout=3)
    assert controller.returncode==3,stdout+stderr
    assert not (host.path/'applied').exists()
    assert actor.poll() is None


@pytest.mark.parametrize('state', ['active','inactive','failed'])
def test_legacy_without_capability_is_never_bootstrapped(module,tmp_path,state):
    root=tmp_path/'state';module.initialize(root)
    show={unit:'LoadState=not-found\n' for unit in ('aads-pipeline-runner.service','aads-pipeline-litellm-runner.service')}
    show['fixture.service']=f'LoadState=loaded\nActiveState={state}\nMainPID={os.getpid() if state=="active" else 0}\nControlGroup=\n'
    with pytest.raises((module.Deferred,FileNotFoundError)):
        module.inventory(root,'fixture.service',show=show)


@pytest.mark.parametrize('failure',['proc_missing','cgroup_missing','proc_permission','invalid_service','capability_permission'])
def test_unknown_observation_fails_closed(module,host,monkeypatch,failure):
    actor=host.actor()
    subprocess.run(['systemctl','show','fixture.service'],env=host.env,check=True,capture_output=True)
    kwargs={'proc':host.proc,'cgroups':host.cg,'show':{'fixture.service':f'LoadState=loaded\nActiveState=active\nMainPID={actor.pid}\nControlGroup=/fixture.service\nExecStart={{ path={host.path / "actor.sh"} ; }}\n','aads-pipeline-runner.service':'LoadState=not-found\n','aads-pipeline-litellm-runner.service':'LoadState=not-found\n'}}
    service='fixture.service'
    if failure=='proc_missing':kwargs['proc']=host.path/'missing'
    if failure=='cgroup_missing':kwargs['cgroups']=host.path/'missing'
    if failure=='invalid_service':service='invalid;service'
    if failure=='capability_permission':(host.state/'actors'/f'{actor.pid}.json').chmod(0o666)
    if failure=='proc_permission':
        original=Path.read_text
        def denied(path,*args,**kw):
            if path==host.proc/str(actor.pid)/'stat':raise PermissionError('fixture')
            return original(path,*args,**kw)
        monkeypatch.setattr(Path,'read_text',denied)
    with pytest.raises((module.Deferred,OSError)):
        module.inventory(host.state,service,**kwargs)


def test_source_identity_is_pinned_when_file_changes_after_start(host):
    actor=host.actor()
    path=host.state/'actors'/f'{actor.pid}.json';original=json.loads(path.read_text())
    source=host.path/'actor.sh';future=host.path/'future.sh';future.write_text(source.read_text()+'\n# future version\n');future.replace(source)
    controller=host.controller();stdout,stderr=controller.communicate(timeout=5)
    assert controller.returncode==0,stdout+stderr
    wait_for(lambda: json.loads(path.read_text())['phase']=='RUNNING')
    assert json.loads(path.read_text())['source_sha256']==original['source_sha256']
    assert original['source_sha256']!=hashlib.sha256(source.read_bytes()).hexdigest()


def test_production_entry_and_every_business_loop_are_inside_lifetime_lease():
    source=(ROOT/'scripts/pipeline-runner.sh').read_text()
    assert source==(ROOT/'scripts/pipeline-runner.sh.local').read_text()
    assert source.index('runner_maintenance_enter /run/aads-runner-maintenance')<source.index('source ~/.claude/current.env')
    main=source.split('main() {',1)[1].split('\n}\n',1)[0]
    assert main.index('runner_maintenance_checkpoint')<main.index('pending=$(claim_queued_job')
    assert '[[ "${RUNNER_MAINTENANCE_ACTIVE:-0}" == 1 ]] && return 0' in source.split('maybe_reexec_on_self_change() {',1)[1].split('\n}\n',1)[0]
    checkpoint=LIB.read_text().split('runner_maintenance_checkpoint() {',1)[1].split('\n}\n',1)[0]
    code='\n'.join(line for line in checkpoint.splitlines() if not line.lstrip().startswith('#'))
    assert 'flock -u' not in code
    assert 'kill ' not in code and 'exit ' not in code and '_shutdown_finalize_job' not in code


def test_unlocked_forged_capability_descriptor_is_rejected(module,host):
    module.initialize(host.state)
    path=host.path/'pretender.sh'
    path.write_text(f'exec 200>{shlex.quote(str(host.state/"lifetime.lock"))}\necho $$ > "$FIXTURE/fake.pid"\nwhile [[ ! -f "$FIXTURE/stop" ]]; do sleep .02; done\n')
    process=subprocess.Popen(['bash',str(path)],env=host.env)
    host.pids.append((process,None))
    wait_for(lambda:(host.path/'fake.pid').exists())
    module.record(host.state,process.pid,'RUNNING',str(path),str(LIB))
    with pytest.raises(module.Deferred,match='not locked'):
        module.read_actor(host.state,process.pid,Path('/proc/sys/kernel/random/boot_id').read_text().strip(),Path('/proc'))
    (host.path/'stop').touch();process.wait(timeout=3)


def test_child_without_inherited_lease_still_defers_at_exclusive_process_probe(host):
    actor=host.actor(orphan=True,drop_lease=True)
    worker=int((host.path/'worker.pid').read_text())
    assert not Path(f'/proc/{worker}/fd/200').exists()
    controller=host.controller(timeout='2')
    stdout,stderr=controller.communicate(timeout=4)
    assert controller.returncode==3,stdout+stderr
    assert 'live or unknown descendant' in stderr
    assert not (host.path/'applied').exists()
    assert actor.poll() is None and Path(f'/proc/{worker}').exists()


def test_apply_exception_releases_leases_and_resumes_without_requeue(host):
    actor=host.actor()
    controller=host.controller(callback='touch "$FIXTURE/apply-entered"; return 42')
    stdout,stderr=controller.communicate(timeout=4)
    assert controller.returncode==42,stdout+stderr
    assert (host.path/'apply-entered').exists()
    wait_for(lambda:json.loads((host.state/'actors'/f'{actor.pid}.json').read_text())['phase']=='RUNNING')
    assert actor.poll() is None


def test_controller_parent_exit_does_not_release_apply_child_leases_early(host):
    actor=host.actor()
    controller=host.controller(callback='touch "$FIXTURE/apply-start"; while [[ ! -f "$FIXTURE/apply-done" ]]; do sleep .02; done; touch "$FIXTURE/apply-finished"')
    wait_for(lambda:(host.path/'apply-start').exists())
    claims=(host.path/'claims').read_text()
    controller.terminate();controller.wait(timeout=3)
    # The transaction subprocess, not an unrelated guardian SSH, continues to
    # own both leases while its apply callback is actually running.
    for name in ('admission.lock','lifetime.lock'):
        with (host.state/name).open('w') as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    time.sleep(.08)
    assert (host.path/'claims').read_text()==claims
    assert actor.poll() is None
    (host.path/'apply-done').touch()
    wait_for(lambda:(host.path/'apply-finished').exists())
    wait_for(lambda:json.loads((host.state/'actors'/f'{actor.pid}.json').read_text())['phase']=='RUNNING')


def test_bundle_rejects_tampered_archive_before_any_install(host):
    sync=(ROOT/'scripts/sync_pipeline_runner_remote.sh').read_text()
    start=sync.index('runner_apply_bundle() {')
    function=sync[start:sync.index('\n}\n',start)+3]
    archive=host.path/'invalid.tar';archive.write_bytes(b'invalid tar with wrong SHA')
    code=host.library+function+f'\nrunner_apply_bundle fixture.service {shlex.quote(str(archive))} {"0"*64} {shlex.quote(str(host.path/"pipeline-runner.sh"))} 1\n'
    result=subprocess.run(['bash','-c',code],env=host.env,capture_output=True,text=True)
    assert result.returncode!=0
    assert 'bundle hash mismatch' in result.stderr
    assert not (host.path/'pipeline-runner.sh').exists()
    assert not (host.path/'systemctl-mutations').exists()


def test_all_repository_restart_entrypoints_use_shared_protocol():
    safe=(ROOT/'scripts/safe_runner_restart_once.sh').read_text()
    assert 'systemctl restart' not in '\n'.join(line for line in safe.splitlines() if not line.lstrip().startswith('#'))
    assert '/restart_local_runner.sh"' in safe
    local=(ROOT/'scripts/restart_local_runner.sh').read_text()
    assert 'runner_maintenance_transaction "$SERVICE" 30 /run/aads-runner-maintenance restart_under_maintenance' in local


@pytest.mark.parametrize('drop_lease',[False,True])
def test_deleted_cwd_terminal_like_orphan_cannot_be_restarted(host,drop_lease):
    with tempfile.TemporaryDirectory(prefix='aads-wt-maintenance-protocol-') as directory:
        actor=host.actor(orphan=True,drop_lease=drop_lease,worker_cwd=directory)
        worker=int((host.path/'worker.pid').read_text())
        Path(directory).rmdir()
        assert os.readlink(f'/proc/{worker}/cwd').endswith(' (deleted)')
        controller=host.controller(timeout='.5')
        stdout,stderr=controller.communicate(timeout=4)
        assert controller.returncode==3,stdout+stderr
        assert not (host.path/'applied').exists()
        assert actor.poll() is None and Path(f'/proc/{worker}').exists()


def test_cancelled_drain_resumes_admission_while_original_worker_remains_alive(host):
    actor=host.actor(worker=True)
    controller=host.controller(timeout='.3')
    stdout,stderr=controller.communicate(timeout=4)
    assert controller.returncode==3,stdout+stderr
    wait_for(lambda:json.loads((host.state/'actors'/f'{actor.pid}.json').read_text())['phase']=='RUNNING')
    worker=int((host.path/'worker.pid').read_text())
    assert Path(f'/proc/{worker}').exists()
    assert actor.poll() is None
    assert not (host.path/'worker-done').exists()
    assert not (host.path/'applied').exists()


def test_metadata_failure_holds_daemon_and_existing_worker_instead_of_systemd_exit(host):
    actor=host.actor(worker=True,fault_metadata=True)
    controller=host.controller(timeout='.4')
    stdout,stderr=controller.communicate(timeout=4)
    assert controller.returncode==3,stdout+stderr
    wait_for(lambda:'MAINTENANCE_FAULT_HOLD' in (host.path/'actor.sh.log').read_text())
    worker=int((host.path/'worker.pid').read_text())
    assert actor.poll() is None and Path(f'/proc/{worker}').exists()
    assert Path(f'/proc/{actor.pid}/fd/200').exists()
    claims=(host.path/'claims').read_text()
    time.sleep(.1)
    assert (host.path/'claims').read_text()==claims
    assert not (host.path/'applied').exists()


@pytest.mark.parametrize('mismatch',[None,'same_pid','source_hash','helper_hash','wrong_phase'])
def test_started_service_must_be_new_exact_source_waiting_on_admission(module,host,monkeypatch,mismatch):
    old=host.actor()
    monkeypatch.setenv('PATH',host.env['PATH']);monkeypatch.setenv('FIXTURE',str(host.path))
    admission=(host.state/'admission.lock').open('w')
    lifetime=(host.state/'lifetime.lock').open('w')
    try:
        fcntl.flock(admission,fcntl.LOCK_EX)
        wait_for(lambda:json.loads((host.state/'actors'/f'{old.pid}.json').read_text())['phase']=='QUIESCENT')
        fcntl.flock(lifetime,fcntl.LOCK_EX)
        plan=module.restart_plan('fixture.service')
        assert plan['previous_pid']==old.pid
        # Simulate systemd restart only for these owned local fixtures. The old
        # actor is already quiescent and has no worker. No production service call.
        children=Path(f'/proc/{old.pid}/task/{old.pid}/children').read_text().split()
        old.terminate()
        for child in children:
            try:os.kill(int(child),15)
            except ProcessLookupError:pass
        old.wait(timeout=3)
        log=(host.path/'replacement.log').open('w')
        new=subprocess.Popen(['bash',str(host.path/'actor.sh')],env=host.env,stdout=log,stderr=log)
        host.pids.append((new,log))
        (host.path/'main.pid').write_text(str(new.pid))
        cap=host.state/'actors'/f'{new.pid}.json'
        wait_for(lambda:cap.exists() and json.loads(cap.read_text())['phase']=='WAIT_ADMISSION')
        assert not Path(f'/proc/{new.pid}/fd/200').exists()
        if mismatch=='same_pid':plan['previous_pid']=new.pid
        if mismatch=='source_hash':plan['source_sha256']='0'*64
        if mismatch=='helper_hash':plan['library_sha256']='0'*64
        if mismatch=='wrong_phase':
            data=json.loads(cap.read_text());data['phase']='QUIESCENT';cap.write_text(json.dumps(data))
        if mismatch is None:
            assert module.inventory(host.state,'fixture.service',True,host.proc,host.cg,restart=plan)=='AWARE'
        else:
            with pytest.raises(module.Deferred,match='restart actor'):
                module.inventory(host.state,'fixture.service',True,host.proc,host.cg,restart=plan)
        assert new.poll() is None
        assert not Path(f'/proc/{new.pid}/fd/200').exists()
    finally:
        lifetime.close();admission.close()


def test_both_restart_callbacks_verify_started_identity_before_transaction_release():
    for relative in ('scripts/sync_pipeline_runner_remote.sh','scripts/restart_local_runner.sh'):
        text=(ROOT/relative).read_text()
        function_name='runner_apply_bundle' if 'sync_' in relative else 'restart_under_maintenance'
        body=text.split(function_name+'() {',1)[1].split('\n}\n',1)[0]
        assert body.index('restart-plan')<body.index('systemctl restart')<body.index('restart-verify')
        assert '/run/aads-runner-maintenance' in body
