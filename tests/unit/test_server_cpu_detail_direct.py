import copy
import json
import sys
from pathlib import Path
import pytest
from app.services import server_cpu_detail as cpu

@pytest.fixture
def recorded():
    return json.loads((Path(__file__).parents[1]/'fixtures/cpu/contabo116-cli.json').read_text())

def test_real_cli_schema_and_processes(recorded):
    now=cpu.timestamp(recorded['results']['snapshot']['data']['sample']['timestamp_utc'])+10
    r=cpu.normalize('contabo116',recorded,15,10,now)
    assert r['host_verified'] and r['status']=='fresh'
    assert r['cpu']['execution_pct']>0
    assert r['top_processes'][0]['cpu_one_logical_pct']>0
    assert r['cgroups'][0]['quota']['period_us']==100000
    assert r['history']['series'] and r['history']['partial']
    assert r['history']['observer_coverage']['samples']>0
    assert r['observer']['units'][0]['state']=='active'
    assert 'argv_sha256' not in json.dumps(r)

def test_stale_exit_two_preserves_observation_without_live_values(recorded):
    snap=recorded['results']['snapshot'];snap['exit_code']=2;snap['data']['status']='stale'
    now=cpu.timestamp(snap['data']['sample']['timestamp_utc'])+300
    r=cpu.normalize('contabo116',recorded,15,10,now)
    assert r['status']=='stale' and r['observed_at']
    assert r['cpu']['execution_pct'] is None
    assert r['top_processes'][0]['cpu_one_logical_pct'] is None

def test_identity_bound_to_requested_server(recorded):
    assert cpu.normalize('contabo14',recorded,15,10)['status']=='host_mismatch'
    recorded['results']['status']['data']['policy']['server_id']='contabo14'
    assert cpu.normalize('contabo116',recorded,15,10)['status']=='host_mismatch'

def test_invalid_delta_and_history_error_coverage(recorded):
    sample=recorded['results']['snapshot']['data']['sample'];sample['cpu']['valid']=False
    recorded['results']['history']['data']['coverage']['error_counts']={'partial_or_invalid_record':2}
    r=cpu.normalize('contabo116',recorded,15,10,cpu.timestamp(sample['timestamp_utc'])+10)
    assert r['cpu']['execution_pct'] is None
    assert r['history']['observer_coverage']['error_counts']['partial_or_invalid_record']==2
    assert 'observer_reported_errors' in r['history']['errors']

def test_running_output_limit_and_timeout():
    rc,out,err,reason=cpu.bounded_run([sys.executable,'-c','import sys; sys.stdout.write("x"*1000000)'],2,1024)
    assert reason=='output_limit' and len(out)<=1024+8192
    assert cpu.bounded_run([sys.executable,'-c','import time; time.sleep(20)'],.05,1024)[3]=='timeout'

@pytest.mark.asyncio
async def test_coalesces_concurrent_reads(monkeypatch,recorded):
    import asyncio
    import time
    calls=[]
    def probe(*args):
        calls.append(args);time.sleep(.03);return copy.deepcopy(recorded)
    cpu._cache.clear();cpu._inflight.clear();monkeypatch.setattr(cpu,'transport',probe)
    await asyncio.gather(*[cpu.get_cpu_detail('contabo116',15,5) for _ in range(5)])
    assert calls==[('contabo116',15)]
    with pytest.raises(KeyError): await cpu.get_cpu_detail('jinah244')
