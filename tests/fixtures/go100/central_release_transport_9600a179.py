#!/usr/bin/env python3
"""Root SSH JSON-lines gate transport. Only central validate_contract grants authority."""
from datetime import datetime, timezone
import json
import os
import re
import select
import time
import secrets
from uuid import UUID

STAGES = frozenset(('preflight', 'source_prepare', 'slot_start', 'slot_restart',
                    'standby_sync', 'overlay_write', 'dropin_write',
                    'route_cutover', 'monitor', 'finalize'))

IDENTITY = ('run_id', 'owner_instance', 'owner_epoch', 'approval_id',
            'snapshot_hash', 'release_sha')


class TransportError(ValueError):
    pass


def validate_contract(contract):
    if not isinstance(contract, dict):
        raise TransportError('invalid_contract')
    if type(contract.get('run_id')) is not int or contract['run_id'] <= 0:
        raise TransportError('invalid_run_id')
    for key in ('owner_instance', 'owner_epoch'):
        if not isinstance(contract.get(key), str) or not contract[key].strip():
            raise TransportError('invalid_' + key)
    for key in ('approval_id', 'tenant_id'):
        try:
            if not isinstance(contract.get(key), str):
                raise ValueError()
            UUID(contract[key])
        except (ValueError, AttributeError) as exc:
            raise TransportError('invalid_' + key) from exc
    for key, expected in (('project', 'GO100'), ('component', 'backend'),
                          ('lane', 'api_only'), ('target_env', 'production')):
        if contract.get(key) != expected:
            raise TransportError('unsupported_scope')
    for key, size in (('release_sha', 40), ('snapshot_hash', 64)):
        if not isinstance(contract.get(key), str) or not re.fullmatch('[0-9a-f]{%d}' % size, contract[key]):
            raise TransportError('invalid_' + key)
    try:
        issued, expires = (datetime.fromisoformat(contract[k].replace('Z', '+00:00'))
                           for k in ('issued_at', 'expires_at'))
        now = datetime.now(timezone.utc)
        if issued.tzinfo is None or expires.tzinfo is None or not issued <= now < expires:
            raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise TransportError('invalid_contract_time') from exc
    # snapshot_hash covers the central full snapshot, not this transport subset.
    return contract


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransportError('duplicate_field')
        result[key] = value
    return result


class StreamGate:
    """One outstanding request; a failed channel is permanently poisoned."""
    def __init__(self, reader, writer, timeout=10):
        self.reader, self.writer = reader, writer
        self.timeout = timeout
        self.buffer = b''
        self.seq = 0
        self.failed = False
        self.contract = None

    def receive(self):
        deadline = time.monotonic() + self.timeout
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.reader], [], [], remaining)[0]:
                raise TransportError('timeout')
            chunk = os.read(self.reader.fileno(), 4096)
            if not chunk:
                raise TransportError('eof')
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise TransportError('oversized_message')
        line, self.buffer = self.buffer.split(b'\n', 1)
        try:
            return json.loads(line, object_pairs_hook=unique_object)
        except (UnicodeError, ValueError) as exc:
            raise TransportError('invalid_json') from exc

    def bootstrap(self):
        if self.contract is not None or self.failed:
            raise TransportError('invalid_bootstrap')
        try:
            message = self.receive()
            if (not isinstance(message, dict) or type(message.get('protocol')) is not int
                    or message['protocol'] != 1 or message.get('type') != 'init'):
                raise TransportError('invalid_init')
            self.contract = dict(validate_contract(message.get('contract')))
            return dict(self.contract)
        except Exception:
            self.failed = True
            raise

    def check(self, stage):
        if self.failed or self.contract is None:
            raise TransportError('channel_unavailable')
        try:
            if not isinstance(stage, str) or stage not in STAGES:
                raise TransportError('invalid_stage')
            validate_contract(self.contract)
            if self.buffer or select.select([self.reader], [], [], 0)[0]:
                raise TransportError('unsolicited_input')
            self.seq += 1
            nonce = secrets.token_hex(32)
            request = dict(protocol=1, type='gate', seq=self.seq, nonce=nonce, stage=stage,
                           **{k: self.contract[k] for k in IDENTITY})
            self.writer.write(json.dumps(request, separators=(',', ':')) + '\n')
            self.writer.flush()
            response = self.receive()
            if not isinstance(response, dict) or type(response.get('protocol')) is not int or response['protocol'] != 1 or response.get('type') != 'gate_result':
                raise TransportError('invalid_response')
            if type(response.get('seq')) is not int or response['seq'] != self.seq:
                raise TransportError('sequence_mismatch')
            if response.get('nonce') != nonce:
                raise TransportError('nonce_mismatch')
            if response.get('allow') is not True:
                raise TransportError('denied')
            validated = validate_contract(response.get('contract'))
            if validated != self.contract:
                raise TransportError('contract_changed')
            return dict(validated)
        except Exception:
            self.failed = True
            raise


