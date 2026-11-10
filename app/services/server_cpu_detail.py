"""Bounded, read-only access to existing host aads-cpu records.

No local probe, collection, database access, or load-based CPU approximation.
Unknown schemas deliberately produce null values. Contract version 1 is consumed
on demand by the subsequent ops UI; recent_samples is explicitly partial.
"""
from __future__ import annotations

import asyncio
import copy
import json
import inspect
import math
import shlex
import subprocess
import os
import selectors
import signal
import time
from datetime import datetime, timezone

from app.services.server_registry import get_server_config

SERVER_IDS = frozenset(('contabo116', 'contabo14', 'cafe24_114', 'contabo92', 'contabo42'))
MAX_BYTES = 2400000
MAX_POINTS = 120
TTL = 10
ERRORS = frozenset(('not_installed', 'access_denied', 'timeout', 'transport_unavailable',
                    'output_limit', 'query_failed', 'invalid_response', 'host_mismatch',
                    'unsupported_schema'))


def error_code(value):
    return value if isinstance(value, str) and value in ERRORS else 'invalid_response'

_cache = {}
_inflight = {}

# Bound both pipes while the process is running; no unbounded temporary files.
def bounded_run(argv, seconds, limit):
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    sel = selectors.DefaultSelector()
    chunks = {"out": bytearray(), "err": bytearray()}
    for stream, key in ((p.stdout, "out"), (p.stderr, "err")):
        os.set_blocking(stream.fileno(), False)
        sel.register(stream, selectors.EVENT_READ, key)
    deadline = time.monotonic() + seconds
    reason = None
    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            for key, _ in sel.select(min(remaining, .1)):
                data = os.read(key.fileobj.fileno(), 8192)
                if not data:
                    sel.unregister(key.fileobj)
                    continue
                chunks[key.data].extend(data)
                if len(chunks["out"]) + len(chunks["err"]) > limit:
                    reason = "output_limit"
                    break
            if reason:
                break
        if not reason:
            try:
                p.wait(timeout=max(.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
    finally:
        # Always reap descendants as well, including on cancellation/parse failure.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        sel.close()
        p.stdout.close()
        p.stderr.close()
    return p.returncode, bytes(chunks["out"]), bytes(chunks["err"]), reason
_BOUNDED_SOURCE = inspect.getsource(bounded_run)
EXPECTED_HOSTS = {
    "contabo116": "vmi3267555", "contabo14": "contabo14",
    "cafe24_114": "rfree-0009.cafe24.com", "contabo42": "vmi3725547",
    "contabo92": "vmi3722544",
}
_REMOTE = "import json,socket,subprocess,selectors,os,signal,time\n" + _BOUNDED_SOURCE + r"""
out = {"hostname": socket.gethostname(), "results": {}}
for name,args in (("status",["status"]),("snapshot",["snapshot"]),("history",["history","--minutes","MINUTES"])):
    try:
        rc, stdout, stderr, reason = bounded_run(["aads-cpu"]+args, 7, 768000)
        if reason:
            value = {"error":reason}
        elif rc in (0,2):
            value = {"data":json.loads(stdout), "exit_code":rc}
        else:
            value = {"error":"access_denied" if b"permission denied" in stderr.lower() else "query_failed"}
    except FileNotFoundError:
        value = {"error":"not_installed"}
    except Exception:
        value = {"error":"invalid_response"}
    out["results"][name]=value
print(json.dumps(out,allow_nan=False))
"""


def _ssh(cfg):
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            '-o', 'StrictHostKeyChecking=yes', '-p', str(int(cfg.get('ssh_port', 22))),
            'root@' + cfg['host']]


def transport(server_id, minutes):
    """Reuse registry root SSH transport, including 92's approved 14 mesh hop.

    Even 116 goes through SSH: the API container is never treated as its host.
    No new keys, SSH config, socket mounts or trust-on-first-use are created.
    """
    cfg = get_server_config(server_id)
    script = _REMOTE.replace('MINUTES', str(minutes))
    command = 'timeout 26 python3 -c ' + shlex.quote(script)
    argv = _ssh(cfg)
    if server_id == 'contabo92':
        probe = cfg.get('health_probe', {})
        if probe.get('via_server') != 'contabo14' or probe.get('via_alias') != 'mesh-92':
            return {'error': 'transport_unavailable'}
        argv = _ssh(get_server_config('contabo14'))
        command = 'timeout 29 ssh -T -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes mesh-92 ' + shlex.quote(command)
    try:
        rc, stdout, stderr, reason = bounded_run(argv + [command], 32, MAX_BYTES)
        if reason:
            return {'error': reason}
        if rc:
            return {'error': 'access_denied' if b'permission denied' in stderr.lower() else 'transport_unavailable'}
        data = json.loads(stdout)
        return data if isinstance(data, dict) else {'error': 'invalid_response'}
    except (OSError, ValueError):
        return {'error': 'transport_unavailable'}


def number(value, low=0, high=1e12):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high:
        return value
    return None


def timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 40:
            return None
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except ValueError:
        return None


def mapping(value):
    return value if isinstance(value, dict) else {}


def _metrics(sample, usable):
    cpu = mapping(sample.get('cpu'))
    valid = usable and cpu.get('valid') is True
    psi = mapping(sample.get('psi'))
    return {
        'execution_pct': number(cpu.get('execution_pct'), high=100) if valid else None,
        'iowait_pct': number(cpu.get('iowait_pct'), high=100) if valid else None,
        'steal_pct': number(cpu.get('steal_pct'), high=100) if valid else None,
        'psi': {resource: {kind: {key: number(mapping(mapping(psi.get(resource)).get(kind)).get(key)) if usable else None
                                for key in ('avg10', 'avg60', 'avg300', 'total')}
                           for kind in ('some', 'full')} for resource in ('cpu', 'io', 'memory')},
        'valid': bool(valid),
        'reason': None if valid else 'stale_or_invalid_or_missing_delta',
    }


def normalize(server_id, raw, minutes, top_n, now=None):
    now = time.time() if now is None else now
    result = {
        'contract_version': 1, 'server_id': server_id, 'source': 'host:aads-cpu',
        'status': error_code(raw['error']) if raw.get('error') else 'unknown', 'host_verified': False,
        'observed_at': None, 'sample_age_s': None,
        'installation': 'unknown', 'access': 'unknown', 'collection': 'unknown',
        'scope': 'existing observer records only; no collection or policy changes',
        'units': {'execution_pct': 'percent of all host logical CPUs (0..100)',
                  'process_cpu_pct': 'percent of one logical CPU (100 = one core)',
                  'quota_cpu_pct': 'unavailable unless observer denominator is verified',
                  'psi_avg': 'percent', 'psi_total': 'microseconds'},
        'cpu': _metrics({}, False), 'top_processes': [],
        'history': {'requested_minutes': minutes, 'representation': 'recent_samples_only',
                    'coverage': 'unknown', 'partial': True, 'first': None, 'last': None,
                    'connect_points': False, 'requested_first': datetime.fromtimestamp(now - minutes * 60, timezone.utc).isoformat(),
                    'requested_last': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    'max_points': MAX_POINTS, 'series': [], 'errors': []},
    }
    if raw.get('error'):
        result['access'] = error_code(raw['error'])
        return result
    results = mapping(raw.get('results'))
    snapshot_result = mapping(results.get('snapshot'))
    snap = mapping(snapshot_result.get('data'))
    sample = mapping(snap.get('sample'))
    # SSH host keys bind registry IP to the remote machine; observer identity must
    # additionally equal that machine's actual hostname. Missing identity fails closed.
    hostname = raw.get('hostname')
    if hostname != EXPECTED_HOSTS.get(server_id) or mapping(sample.get('host')).get('hostname') != hostname:
        result['status'] = error_code(snapshot_result['error']) if snapshot_result.get('error') else 'host_mismatch' if mapping(sample.get('host')).get('hostname') else 'unsupported_schema'
        result['installation'] = 'not_installed' if result['status'] == 'not_installed' else 'unknown'
        result['access'] = 'access_denied' if result['status'] == 'access_denied' else 'reachable'
        return result
    result.update(host_verified=True, installation='installed', access='readable')
    result['query_states'] = {name: error_code(mapping(results.get(name))['error'])
                              if mapping(results.get(name)).get('error') else 'readable'
                              for name in ('status', 'snapshot', 'history')}
    observed = sample.get('timestamp_utc')
    ts = timestamp(observed)
    age = max(0, now - ts) if ts is not None and ts <= now + 5 else None
    supplied_age = number(snap.get('sample_age_s'))
    if age is not None and supplied_age is not None:
        age = max(age, supplied_age)
    fresh = snap.get('status') == 'fresh' and age is not None and age <= 120
    result.update(status='fresh' if fresh else 'stale', observed_at=observed if ts is not None else None,
                  sample_age_s=age, collection='fresh' if fresh else 'stale_or_unknown')
    result['cpu'] = _metrics(sample, fresh)
    status_data = mapping(mapping(results.get('status')).get('data'))
    policy = mapping(status_data.get('policy'))
    if policy.get('server_id') not in (None, server_id):
        result.update(status='host_mismatch', host_verified=False, cpu=_metrics({}, False))
        return result
    result['observer'] = {
        'status': status_data.get('status'),
        'policy': {key: policy.get(key) for key in ('schema_version','server_id','interval_seconds','top_processes','automatic_business_actions','privacy')},
        'units': [{'unit': u.get('Id'), 'state': u.get('ActiveState'), 'substate': u.get('SubState')}
                  for u in mapping(status_data.get('units')).get('units', [])
                  if isinstance(u, dict) and u.get('Id') == 'go100-cpu-observer.service'],
    }
    for process in (sample.get('top_processes') or [])[:top_n]:
        if not isinstance(process, dict):
            continue
        valid = fresh and process.get('valid') is True
        result['top_processes'].append({
            **{key: number(process.get(key)) for key in ('pid', 'start_ticks')},
            **{key: number(process.get(key)) if valid else None for key in ('cpu_one_logical_pct','cpu_host_pct')},
            **{key: str(process.get(key) or '')[:256] for key in ('process_family','service_unit','cgroup_path')},
            'valid': bool(valid), 'reason': None if valid else 'stale_or_invalid_delta',
        })
    result['cgroups'] = []
    for group in (sample.get('cgroups') or [])[:128]:
        if not isinstance(group, dict):
            continue
        valid = fresh and group.get('delta_valid') is True
        result['cgroups'].append({
            'path': str(group.get('path') or '')[:256],
            'quota': {k: number(mapping(group.get('cpu_max')).get(k)) for k in ('quota_us','period_us','quota_cores')},
            'weight': number(group.get('cpu_weight')),
            'cpu_one_logical_pct': number(group.get('cpu_one_logical_pct')) if valid else None,
            'cpu_host_pct': number(group.get('cpu_host_pct')) if valid else None,
            'throttled_period_ratio': number(group.get('throttled_period_ratio')) if valid else None,
            'throttling_delta': {k: number(mapping(group.get('cpu_stat_delta')).get(k)) if valid else None for k in ('nr_throttled','nr_periods','throttled_usec')},
            'valid': bool(valid),
        })
    hist_result = mapping(results.get('history'))
    history = mapping(hist_result.get('data'))
    h = result['history']
    if hist_result.get('error'):
        h['errors'].append(error_code(hist_result['error']))
    h['observer_status'] = history.get('status')
    coverage = mapping(history.get('coverage'))
    h['observer_coverage'] = {k: coverage.get(k) for k in ('samples','valid_cpu_samples','invalid_cpu_samples','first_sample_utc','last_sample_utc','bytes_read','error_counts')}
    h['summary'] = {k: number(mapping(history.get('execution_cpu_summary')).get(k), high=100) for k in ('min','mean','max')}
    if coverage.get('error_counts') or history.get('errors'):
        h['errors'].append('observer_reported_errors')
    rows = history.get('recent_samples')
    if not isinstance(rows, list):
        h['errors'].append('missing_recent_samples')
        return result
    if len(rows) > MAX_POINTS:
        h['errors'].append('point_limit')
    points = {}
    for row in rows[:MAX_POINTS]:
        if not isinstance(row, dict):
            continue
        t = timestamp(row.get('timestamp_utc'))
        if mapping(row.get('host')).get('hostname') != hostname:
            h['errors'].append('history_host_mismatch')
            continue
        if t is None or not now - minutes * 60 <= t <= now:
            continue
        points[t] = {'observed_at': row['timestamp_utc'], **_metrics(row, True)}
    h['series'] = [points[t] for t in sorted(points)]
    if h['series']:
        h.update(first=h['series'][0]['observed_at'], last=h['series'][-1]['observed_at'], coverage='provided_samples_only')
    # Always partial: CLI's last-six samples do not prove whole-window coverage.
    return result


async def _collect(key):
    server_id, minutes = key
    try:
        raw = await asyncio.to_thread(transport, server_id, minutes)
        _cache[key] = (time.monotonic(), raw)
        return raw
    finally:
        _inflight.pop(key, None)


async def get_cpu_detail(server_id, minutes=15, top_n=10):
    if server_id not in SERVER_IDS or not get_server_config(server_id):
        raise KeyError(server_id)
    if type(minutes) is not int or not 1 <= minutes <= 60 or type(top_n) is not int or not 1 <= top_n <= 20:
        raise ValueError('invalid limits')
    key = (server_id, minutes)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL:
        raw = cached[1]
    else:
        # Cache by window so coverage/summary never describe a different interval.
        # Server/window validation bounds keys to 5 * 60; top-N shares each read.
        if key not in _inflight:
            _inflight[key] = asyncio.create_task(_collect(key))
        raw = await asyncio.shield(_inflight[key])
    try:
        return normalize(server_id, copy.deepcopy(raw), minutes, top_n)
    except (TypeError, ValueError, AttributeError):
        return normalize(server_id, {'error': 'invalid_response'}, minutes, top_n)
