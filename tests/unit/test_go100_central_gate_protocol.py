"""Isolated policy/authority stubs exercise transport, not operating authority."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.go100_central_gate_protocol import CentralGate, GateError, IDENTITY, STAGES

SOURCE_COMMIT = '9600a179339b666d32dc8cd4442a205d00110167'
SOURCE_PATH = 'scripts/go100/central_release_transport.py'
SOURCE_SHA256 = '03079585354cad4aebc80436dc8128a02a131858cd1dc297a115a53accedc7cc'
FIXTURE = ROOT / 'tests/fixtures/go100/central_release_transport_9600a179.py'


def contract():
    now = datetime.now(timezone.utc)
    return dict(run_id=1, owner_instance='owner', owner_epoch='epoch',
                approval_id='11111111-1111-4111-8111-111111111111',
                tenant_id='22222222-2222-4222-8222-222222222222',
                snapshot_hash='a'*64, release_sha='b'*40, project='GO100',
                component='backend', lane='api_only', target_env='production',
                issued_at=(now-timedelta(minutes=1)).isoformat(),
                expires_at=(now+timedelta(minutes=5)).isoformat())


def request(c, stage='preflight', seq=1, nonce='a'*64):
    return dict(protocol=1, type='gate', seq=seq, nonce=nonce, stage=stage,
                **{k: c[k] for k in IDENTITY})


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.c = contract()
        self.g = CentralGate(self.c, lambda s, c: True, lambda c: c)

    def test_fixture_hash(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), SOURCE_SHA256)

    def test_all_stages_and_echo(self):
        for n, stage in enumerate(sorted(STAGES), 1):
            response = self.g.check(request(self.c, stage, n))
            self.assertEqual(response['nonce'], 'a'*64)
            self.assertEqual(response['seq'], n)
            self.assertEqual(response['contract'], self.c)
        spec = importlib.util.spec_from_file_location('remote', FIXTURE)
        remote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(remote)
        self.assertEqual(STAGES, remote.STAGES)
        self.assertEqual(IDENTITY, remote.IDENTITY)

    def test_reject_and_poison(self):
        cases = [dict(protocol=True), dict(type='init'), dict(seq=True),
                 dict(seq=0), dict(nonce='A'*64), dict(nonce='a'*63),
                 dict(stage='unknown'), dict(run_id=True), dict(owner_epoch='changed')]
        for changes in cases:
            with self.subTest(changes=changes):
                gate = CentralGate(self.c, lambda s, c: True, lambda c: c)
                msg = request(self.c); msg.update(changes)
                with self.assertRaises(GateError): gate.check(msg)
                with self.assertRaises(GateError): gate.check(request(self.c))
        self.g.check(request(self.c))
        with self.assertRaises(GateError): self.g.check(request(self.c))
        with self.assertRaises(GateError): self.g.check(request(self.c, seq=2))

    def test_policy_authority_and_callbacks(self):
        with self.assertRaises(GateError): CentralGate(self.c)
        for policy, authority in [(lambda s, c: False, lambda c: c),
                                  (lambda s, c: True, lambda c: None),
                                  (lambda s, c: True, lambda c: dict(c, run_id=True)),
                                  (lambda s, c: s == 'preflight', lambda c: c)]:
            gate = CentralGate(self.c, policy, authority)
            with self.assertRaises(GateError): gate.check(request(self.c, stage='source_prepare'))
            with self.assertRaises(GateError): gate.check(request(self.c))
        def fail(c): raise RuntimeError('db unavailable')
        with self.assertRaises(GateError): CentralGate(self.c, lambda s, c: True, fail).check(request(self.c))

    def test_eof_timeout_and_bad_json(self):
        for payload, expected in [(b'', 'eof'), (None, 'timeout'), (b'{bad}\n', 'invalid_input'),
                                  (b'{"seq":1,"seq":2}\n', 'duplicate_field')]:
            rd, wr = os.pipe()
            try:
                if payload is not None:
                    os.write(wr, payload); os.close(wr); wr = None
                with os.fdopen(rd, 'rb') as reader:
                    with self.assertRaisesRegex(GateError, expected): self.g.receive(reader, .05)
                    with self.assertRaises(GateError): self.g.check(request(self.c))
            finally:
                if wr is not None: os.close(wr)
            self.setUp()

    def test_real_streamgate_roundtrip(self):
        code = '''import importlib.util, sys\ns=importlib.util.spec_from_file_location("remote", sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\ng=m.StreamGate(sys.stdin.buffer, sys.stdout, timeout=2); g.bootstrap(); g.check("preflight"); g.check("source_prepare")\n'''
        child = subprocess.Popen([sys.executable, '-c', code, str(FIXTURE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.stdin.write((json.dumps(dict(protocol=1, type='init', contract=self.c))+'\n').encode()); child.stdin.flush()
            for seq in (1, 2):
                result = self.g.receive(child.stdout, timeout=2)
                self.assertEqual(result['seq'], seq)
                child.stdin.write((json.dumps(result)+'\n').encode()); child.stdin.flush()
            child.stdin.close(); child.stdin = None
            _, err = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 0, err.decode())
        finally:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=2)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream: stream.close()

    def test_real_streamgate_rejects_bad_central_responses(self):
        code = '''import importlib.util, sys
s=importlib.util.spec_from_file_location("remote", sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
g=m.StreamGate(sys.stdin.buffer, sys.stdout, timeout=.3); g.bootstrap()
try:
    g.check("preflight")
except m.TransportError as e:
    print(str(e), file=sys.stderr)
else:
    raise AssertionError("response accepted")
try:
    g.check("source_prepare")
except m.TransportError as e:
    assert str(e) == "channel_unavailable", str(e)
else:
    raise AssertionError("failed channel reused")
'''
        for case, expected in [('nonce', 'nonce_mismatch'), ('seq', 'sequence_mismatch'),
                               ('identity', 'contract_changed'), ('deny', 'denied'),
                               ('eof', 'eof'), ('timeout', 'timeout')]:
            with self.subTest(case=case):
                child = subprocess.Popen([sys.executable, '-c', code, str(FIXTURE)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                streams = (child.stdin, child.stdout, child.stderr)
                try:
                    child.stdin.write((json.dumps(dict(protocol=1, type='init', contract=self.c))+'\n').encode())
                    child.stdin.flush()
                    result = CentralGate(self.c, lambda s, c: True, lambda c: c).receive(child.stdout, timeout=2)
                    if case == 'eof':
                        child.stdin.close()
                    elif case != 'timeout':
                        if case == 'nonce': result['nonce'] = 'b' * 64
                        elif case == 'seq': result['seq'] += 1
                        elif case == 'identity': result['contract']['owner_epoch'] = 'changed'
                        else: result['allow'] = False
                        child.stdin.write((json.dumps(result)+'\n').encode())
                        child.stdin.flush()
                    child.wait(timeout=3)
                    error = child.stderr.read().decode()
                    self.assertEqual(child.returncode, 0, error)
                    self.assertEqual(error.strip(), expected)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=2)
                    for stream in streams:
                        stream.close()
                    self.assertIsNotNone(child.poll())
                    self.assertTrue(all(stream.closed for stream in streams))


if __name__ == '__main__':
    unittest.main()
