"""Independent fail-closed validator for the GO100 JSON-lines gate protocol.

Callbacks supply policy and freshly validated authority; this module grants none.
"""
import json
import os
import re
import select
import time

STAGES = frozenset(('preflight', 'source_prepare', 'slot_start', 'slot_restart',
                    'standby_sync', 'overlay_write', 'dropin_write',
                    'route_cutover', 'monitor', 'finalize'))
IDENTITY = ('run_id', 'owner_instance', 'owner_epoch', 'approval_id',
            'snapshot_hash', 'release_sha')


class GateError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateError('duplicate_field')
        result[key] = value
    return result


class CentralGate:
    """Validate requests for one contract; any rejection poisons the channel.

    stage_policy(stage, contract) must return True. fresh_authority(contract)
    must return a freshly validated contract identical in value and type.
    """
    def __init__(self, contract, stage_policy=None, fresh_authority=None):
        if not isinstance(contract, dict) or not callable(stage_policy) or not callable(fresh_authority):
            raise GateError('missing_authority_or_policy')
        self.contract = dict(contract)
        if any(k not in contract for k in IDENTITY):
            raise GateError('invalid_identity')
        self.stage_policy = stage_policy
        self.fresh_authority = fresh_authority
        self.seq = 0
        self.failed = False
        self.buffer = b''

    def _same(self, a, b):
        return type(a) is type(b) and a == b

    def check(self, message):
        if self.failed:
            raise GateError('channel_unavailable')
        try:
            if not isinstance(message, dict) or type(message.get('protocol')) is not int or message['protocol'] != 1 or message.get('type') != 'gate':
                raise GateError('invalid_message')
            seq, nonce, stage = message.get('seq'), message.get('nonce'), message.get('stage')
            if type(seq) is not int or seq <= self.seq:
                raise GateError('invalid_sequence')
            if not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{64}', nonce):
                raise GateError('invalid_nonce')
            if not isinstance(stage, str) or stage not in STAGES:
                raise GateError('invalid_stage')
            if any(k not in message or not self._same(message[k], self.contract[k]) for k in IDENTITY):
                raise GateError('identity_changed')
            if self.stage_policy(stage, dict(self.contract)) is not True:
                raise GateError('stage_denied')
            fresh = self.fresh_authority(dict(self.contract))
            if not isinstance(fresh, dict) or fresh.keys() != self.contract.keys() or any(not self._same(fresh[k], v) for k, v in self.contract.items()):
                raise GateError('authority_changed')
            self.seq = seq
            return {'protocol': 1, 'type': 'gate_result', 'seq': seq,
                    'nonce': nonce, 'allow': True, 'contract': dict(fresh)}
        except Exception as exc:
            self.failed = True
            if isinstance(exc, GateError):
                raise
            raise GateError('callback_failed') from exc

    def receive(self, reader, timeout=10):
        if self.failed:
            raise GateError('channel_unavailable')
        try:
            deadline = time.monotonic() + timeout
            while b'\n' not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([reader], [], [], remaining)[0]:
                    raise GateError('timeout')
                chunk = os.read(reader.fileno(), 4096)
                if not chunk:
                    raise GateError('eof')
                self.buffer += chunk
                if len(self.buffer) > 65536:
                    raise GateError('oversized_message')
            line, self.buffer = self.buffer.split(b'\n', 1)
            return self.check(json.loads(line, object_pairs_hook=_unique))
        except Exception as exc:
            self.failed = True
            if isinstance(exc, GateError):
                raise
            raise GateError('invalid_input') from exc
