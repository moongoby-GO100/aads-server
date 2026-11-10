"""Read-only interactive /usage collection on the execution host.

Only slash commands are sent. Unrecognized screens fail closed; no model prompt,
login action, credential mutation, or inferred reset timestamp is permitted.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import threading
import signal
import json
import math
import os
import pty
import re
import select
import subprocess
import time
import fcntl
import struct
import termios
import sys
from datetime import datetime, timezone

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class LoginRequiredError(RuntimeError):
    """The runtime requested authentication; local metadata is not evidence."""


class QuotaIdentityError(ValueError):
    """Untrusted identity evidence invalidates all prior observations."""


class DuplicateQuotaKeyError(QuotaIdentityError):
    """A duplicate JSON member must never fall back to text parsing."""


def _inspect_quota_json(clean, account, identities):
    # Collect pairs before constructing dicts: decoded escape-equivalent keys
    # and identities in overwritten values must remain visible at every depth.
    duplicate = False
    # Even a truncated object can contain explicit conflicting identity evidence.
    for match in re.finditer(r'"(?:account|acc\\u006funt)"\s*:\s*("(?:[^"\\]|\\.)*")', clean):
        identities.append(json.loads(match[1]))
    def pairs_hook(pairs):
        nonlocal duplicate
        result = {}
        for key, value in pairs:
            if key in result:
                duplicate = True
            if key.casefold() == 'account':
                identities.append(value)
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=pairs_hook)
    offset = 0
    payloads = 0
    while offset < len(clean):
        if clean[offset] not in '{[':
            offset += 1
            continue
        try:
            item, end = decoder.raw_decode(clean, offset)
            if isinstance(item, dict) and "groups" in item:
                payloads += 1
        except json.JSONDecodeError:
            offset += 1
        else:
            offset = end
    if any(not isinstance(value, str) or not value.strip() for value in identities):
        raise QuotaIdentityError('quota_identity_ambiguous')
    if any(not account or value.casefold() != account.casefold() for value in identities):
        raise QuotaIdentityError('quota_account_mismatch')
    if payloads > 1:
        raise QuotaIdentityError("quota_identity_ambiguous")
    if duplicate:
        raise DuplicateQuotaKeyError('duplicate_quota_json_key')


def clean_screen(text: str) -> str:
    return _ANSI.sub('', _OSC.sub('', text)).replace('\r', '')


def login_required_screen(text: str) -> bool:
    # Captured on 116, 2026-11-10. Never send a selection, even when a
    # slash-command banner is also present in the terminal history.
    clean = clean_screen(text)
    return bool(re.search(r'Select login method\s*:', clean, re.I))


def _native_usage(clean, account):
    """CLI 1.5.4 modal, captured on the host; never infer reset timestamps."""
    marker = '└ Models & Quota'
    if clean.count(marker) != 1 or clean.count('esc Close') != 1:
        raise ValueError('quota_modal_incomplete_or_ambiguous')
    body, tail = clean.split(marker, 1)[1].split('↑/↓ Scroll', 1)
    if not re.match(r'.*esc Close', tail, re.S):
        raise ValueError('quota_modal_incomplete')
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or lines.pop(0).casefold() != ('Account: ' + account).casefold():
        raise QuotaIdentityError('quota_account_mismatch')
    groups = []
    for header, name in [('GEMINI MODELS', 'Gemini'), ('CLAUDE AND GPT MODELS', 'Claude and GPT')]:
        if not lines or lines.pop(0) != header:
            raise ValueError('incomplete_quota_groups')
        if not lines or not lines[0].startswith('Models within this group: '):
            raise ValueError('invalid_quota_models')
        models = lines.pop(0).split(': ', 1)[1].split(', ')
        windows = []
        for title, period in [('Weekly Limit Remaining', 'weekly'), ('Five Hour Limit Remaining', 'five-hour')]:
            if not lines or lines.pop(0) != title:
                raise ValueError('incomplete_quota_windows')
            if lines and lines[0].startswith('Disabled: '):
                reason = lines.pop(0)
                if period != 'five-hour' or windows[0]['remaining_percent'] != 0 or not re.fullmatch(
                    r'Disabled: You have hit your weekly limit, the 5-hour limit does not currently apply\. Your weekly limit will fully refresh in [\w ,]+\.', reason):
                    raise ValueError('invalid_disabled_window')
                windows.append({'period': period, 'remaining_percent': None, 'used_percent': None,
                                'disabled': True, 'reset_at': None,
                                'reset_description': '주간 한도 소진으로 5시간 한도 비적용'})
                continue
            value = re.fullmatch(r'\[[█░]+\]\s*(\d+(?:\.\d+)?)%', lines.pop(0) if lines else '')
            refresh = re.fullmatch(r'Refreshes in ((?:\d+[dhms]\s*)+)', lines.pop(0) if lines else '')
            if not value or not refresh or not 0 <= float(value[1]) <= 100:
                raise ValueError('invalid_quota_window')
            remaining = float(value[1])
            windows.append({'period': period, 'remaining_percent': remaining, 'used_percent': 100 - remaining,
                            'reset_at': None, 'reset_description': refresh[1].strip(), 'disabled': False})
        groups.append({'name': name, 'models': models, 'windows': windows})
    # The modal ends with the provider's explanation. Any extra window before
    # this fixed prose is a malformed/partial response, never decoration.
    explanation = ' '.join(line.lstrip('│').strip() for line in lines)
    expected = ('Within each group, models share a weekly limit and a 5-hour limit. Quota is '
                'consumed proportionally to the cost of the tokens. Thus, limits will last '
                'longer with shorter tasks or using more cost-effective models. The 5-hour '
                'limit smooths out aggregate demand to fairly distribute global capacity '
                'across all users, while your weekly limit is tied directly to your individual tier.')
    if explanation != expected:
        raise ValueError('unrecognized_quota_modal_tail')
    return groups


def parse_usage(text: str, account: str, catalog: list[dict]) -> list[dict]:
    """Accept explicit quota fields only; never derive quota from relay capacity.

    JSON is accepted for clients exposing a structured screen. Text parsing is
    deliberately conservative until a captured host screen proves its grammar.
    """
    if login_required_screen(text):
        raise LoginRequiredError('quota_login_required')
    clean = clean_screen(text)
    identities = re.findall(r'^\s*Account:\s*([^\s]+)\s*$', clean, re.M | re.I)
    text_identities = list(identities)
    # Also inspect inline identity evidence in malformed mixed-format screens.
    explicit_identities = re.findall(r'\bAccount:[ \t]*([^\s]+)', clean, re.I)
    _inspect_quota_json(clean, account, explicit_identities)
    if '└ Models & Quota' in clean:
        if '{' in clean or len(identities) != 1:
            raise QuotaIdentityError('quota_identity_ambiguous')
        return _native_usage(clean, account)
    if any(ord(char) < 32 and char not in '\n\t' for char in clean):
        raise ValueError('invalid_quota_control_sequence')
    identities = text_identities
    if len(identities) > 1:
        raise QuotaIdentityError('quota_identity_ambiguous')
    # Outside the structured payload only known terminal decoration is allowed.
    # Never skip usage-shaped lines merely because JSON was found earlier.
    def decoration(line):
        return (not line.strip() or line.strip() in {'/usage', '>', '❯',
                'Type /help for commands', '/help for commands'}
                or re.fullmatch(r'[ \t─━═┄┅┈┉-]+', line) is not None
                or line.strip() == '── decorative separator ──')

    payload = None
    outside = []
    for line in clean.splitlines():
        try:
            candidate = json.loads(line)
        except ValueError:
            outside.append(line)
            continue
        if isinstance(candidate, dict) and 'groups' in candidate:
            if payload is not None:
                raise QuotaIdentityError('quota_identity_ambiguous')
            payload = candidate
        else:
            outside.append(line)
    if payload is not None and any(not decoration(line) for line in outside):
        raise ValueError('mixed_quota_formats')
    if payload is None:
        # A period and the word remaining must be present on the same line.
        groups = []
        current = None
        for line in clean.splitlines():
            header = re.fullmatch(r'\s*(Gemini|Claude and GPT)\s*:?[ \t]*', line)
            if re.match(r'\s*(?:Gemini|Claude and GPT)(?:\s*:|\s*$)', line) and header is None:
                raise ValueError('invalid_quota_group')
            if header:
                name = header[1]
                current = {'name': name, 'models': [], 'windows': []}
                groups.append(current)
                continue
            membership = re.fullmatch(r'\s*Models:\s*([a-zA-Z0-9., _-]+)\s*', line)
            if re.match(r'\s*Models\b', line, re.I) and (membership is None or current is None or current['models']):
                raise ValueError('invalid_quota_models')
            if membership and current is not None:
                current['models'] = [m.strip() for m in membership[1].split(',') if m.strip()]
                continue
            window = re.fullmatch(r'\s*(weekly|five-hour)\s*:\s*(\d+(?:\.\d+)?)%\s+remaining\s*', line, re.I)
            if re.match(r'\s*(?:weekly|five[ -]hour)\b', line, re.I) and (window is None or current is None):
                raise ValueError('invalid_quota_window')
            if window and current is not None:
                current['windows'].append({'period': window[1].lower(),
                                           'remaining_percent': float(window[2]), 'reset_at': None})
                continue
            if not decoration(line) and not re.fullmatch(r'\s*Account:\s*[^\s]+\s*', line, re.I):
                raise ValueError('unrecognized_quota_line')
        if len(identities) != 1:
            raise ValueError('quota_identity_missing_or_ambiguous')
        payload = {'account': identities[0], 'groups': groups}
    if identities and identities[0].casefold() != str(payload.get('account', '')).casefold():
        raise QuotaIdentityError('quota_account_mismatch')
    if not account or str(payload.get('account', '')).casefold() != account.casefold():
        raise QuotaIdentityError('quota_account_mismatch')
    if set(payload) - {'account', 'groups'}:
        raise ValueError('unexpected_quota_fields')
    if not isinstance(payload.get('groups'), list):
        raise ValueError('invalid_quota_groups')
    result, seen = [], set()
    for group in payload['groups']:
        if not isinstance(group, dict):
            raise ValueError('invalid_quota_group')
        if set(group) - {'name', 'models', 'windows'}:
            raise ValueError('unexpected_quota_fields')
        name, models, windows = group.get('name'), group.get('models'), group.get('windows')
        if name not in {'Gemini', 'Claude and GPT'} or name in seen or not models or not isinstance(windows, list) or not windows:
            raise ValueError('invalid_quota_group')
        if not isinstance(models, list) or any(not isinstance(m, str) or not m for m in models):
            raise ValueError('invalid_quota_models')
        seen.add(name)
        normalized, periods = [], set()
        for window in windows:
            if not isinstance(window, dict):
                raise ValueError('invalid_quota_window')
            if set(window) - {'period', 'remaining_percent', 'reset_at'}:
                raise ValueError('unexpected_quota_fields')
            remaining, period = window.get('remaining_percent'), window.get('period')
            if (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
                    or not math.isfinite(remaining) or not 0 <= remaining <= 100
                    or period not in {'weekly', 'five-hour'} or period in periods):
                raise ValueError('invalid_quota_window')
            periods.add(period)
            reset = window.get('reset_at')
            if reset is not None:
                if not isinstance(reset, str):
                    raise ValueError('invalid_quota_reset')
                dt = datetime.fromisoformat(reset.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    raise ValueError('invalid_quota_reset')
                reset = dt.isoformat()
            normalized.append({'period': period, 'remaining_percent': remaining,
                               'used_percent': 100 - remaining, 'reset_at': reset})
        if periods != {'weekly', 'five-hour'}:
            raise ValueError('incomplete_quota_windows')
        result.append({'name': name, 'models': list(dict.fromkeys(models)), 'windows': normalized})
    if not result:
        raise ValueError('quota_screen_unrecognized')
    return result


def read_usage(binary: str, env: dict, timeout: float = 12, *, cancel_event=None) -> str:
    """Read a bounded slash-command session, owning its entire process group.

    The optional cooperative signal preserves the existing three-argument API.
    Unknown startup screens time out without sending any command.
    """
    deadline = time.monotonic() + timeout
    # Reserve part of the total deadline for TERM, KILL and reaping.
    cleanup_budget = min(1.0, timeout / 3)
    read_deadline = deadline - cleanup_budget
    master, slave = pty.openpty()
    proc = None
    try:
        # Give the interactive CLI a controlling terminal without preexec_fn
        # (unsafe from the collector thread). The tiny exec wrapper owns no auth.
        wrapper = ('import os,sys,fcntl,termios; '
                   'fcntl.ioctl(0,termios.TIOCSCTTY,0); '
                   'os.execvpe(sys.argv[1],[sys.argv[1]],os.environ)')
        proc = subprocess.Popen([sys.executable, '-c', wrapper, binary], env=env, stdin=slave, stdout=slave,
                                stderr=slave, start_new_session=True)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 48, 150, 0, 0))
        os.close(slave)
        slave = None
        chunks, sent, complete = bytearray(), False, False
        native_ready_at = None
        while time.monotonic() < read_deadline and len(chunks) < 131072:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('quota_collection_cancelled')
            ready, _, _ = select.select([master], [], [], min(.02, max(0, read_deadline - time.monotonic())))
            if ready:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) >= 131072:
                    raise ValueError('quota_screen_limit')
                screen = clean_screen(chunks.decode('utf-8', errors='replace'))
                if login_required_screen(screen):
                    raise LoginRequiredError('quota_login_required')
                # Explicit slash-command affordance; do not treat a generic >
                # prompt or an account/login banner as verified readiness.
                if not sent and '? for shortcuts' in screen and '@' in screen and native_ready_at is None:
                    # CLI 1.5.4 opens the shell before its quota manager is
                    # initialized; opening /usage then leaves a loading modal.
                    native_ready_at = time.monotonic() + 3
                if not sent and re.search(r'(?:^|\n)\s*(?:Type /help for commands|/help for commands)\s*(?:\n|$)', screen):
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError('quota_collection_cancelled')
                    os.write(master, b'/usage\r')
                    chunks.clear()
                    sent = True
                elif sent and '└ Models & Quota' in screen:
                    # The loading modal has the same close footer. Require a
                    # fully parsed final redraw before accepting completion.
                    modal = '└ Models & Quota' + screen.rsplit('└ Models & Quota', 1)[1]
                    if 'esc Close' not in modal:
                        continue
                    modal = modal.split('esc Close', 1)[0] + 'esc Close'
                    identity = re.search(r'Account:\s*([^\s]+)', modal)
                    try:
                        if identity:
                            _native_usage(modal, identity[1])
                        else:
                            continue
                    except (ValueError, IndexError):
                        continue
                    chunks = bytearray(modal.encode())
                    complete = True
                    break
                elif sent and re.search(r'(?:^|\n)[ \t]*[>❯][ \t]*$', screen):
                    # A returned interactive prompt is required. EOF, a quiet
                    # interval and parseable quota fields alone are not completion.
                    complete = True
                    break
            if not sent and native_ready_at is not None and time.monotonic() >= native_ready_at:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError('quota_collection_cancelled')
                os.write(master, b'/usage\r')
                chunks.clear()
                sent = True
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('quota_collection_cancelled')
        if not sent:
            raise TimeoutError('quota_cli_not_ready')
        if not chunks:
            raise TimeoutError('quota_screen_empty')
        if not complete:
            raise TimeoutError('quota_screen_incomplete')
        return chunks.decode('utf-8', errors='replace')
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
        if proc is not None:
            # start_new_session makes proc.pid our owned PGID even if its
            # leader has exited. Never skip group cleanup based on poll().
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                except OSError as exc:
                    raise CleanupIncompleteError('quota_group_signal_failed') from exc
                if sig == signal.SIGTERM:
                    until = min(deadline - cleanup_budget / 2, time.monotonic() + cleanup_budget / 2)
                    while time.monotonic() < until:
                        try:
                            os.killpg(proc.pid, 0)
                        except ProcessLookupError:
                            break
                        except OSError as exc:
                            raise CleanupIncompleteError('quota_group_check_failed') from exc
                        time.sleep(.01)
            try:
                proc.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise CleanupIncompleteError('quota_leader_not_reaped') from exc
            # Delivery of SIGKILL is asynchronous. Give surviving group members
            # the remaining bounded cleanup interval before releasing the lock.
            while time.monotonic() < deadline:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                except OSError as exc:
                    raise CleanupIncompleteError('quota_group_check_failed') from exc
                time.sleep(min(.01, max(0, deadline - time.monotonic())))

            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise CleanupIncompleteError('quota_group_check_failed') from exc
            else:
                raise CleanupIncompleteError('quota_process_group_remaining')


class CleanupIncompleteError(RuntimeError):
    """Owned group still exists; this collector must not open another PTY."""


class UsageCollector:
    """One active account epoch; switch invalidates old observations and flights."""
    def __init__(self, reader=read_usage, *, ttl=120, backoff=30, timeout=12):
        self.reader, self.ttl, self.backoff, self.timeout = reader, ttl, backoff, timeout
        self.identity = None
        self.epoch = 0
        self.task = None
        self.cached = None
        self.next_attempt = 0.0
        self.failures = 0
        self.cancel_event = None
        self._events = {}
        self._reader_lock = threading.Lock()
        self._cleanup_failed = False

    def observe_binding(self, binding, env):
        """Invalidate and signal the current reader without starting another."""
        identity = (str(binding.get('account') or '').casefold(), env.get('HOME'),
                    env.get('XDG_CONFIG_HOME'), bool(binding.get('bound')),
                    bool(binding.get('needs_login')))
        if identity != self.identity:
            if self.cancel_event is not None:
                self.cancel_event.set()
            self.identity, self.epoch = identity, self.epoch + 1
            self.cached, self.task, self.next_attempt, self.failures = None, None, 0.0, 0
        return identity

    async def get(self, binding, binary, env, catalog, *, wait_timeout=None):
        identity = self.observe_binding(binding, env)
        if not identity[3] or identity[4] or not identity[0]:
            self.cached = None
            return self._empty((None,), 'unknown')
        if time.monotonic() < self.next_attempt and self.cached is not None:
            return copy.deepcopy(self.cached)
        epoch = self.epoch
        if self.task is None or self.task.done():
            self.cancel_event = threading.Event()
            self._events[epoch] = self.cancel_event
            self.task = asyncio.create_task(self._collect(identity, epoch, binary, env, catalog))
        try:
            result = await asyncio.wait_for(asyncio.shield(self.task), timeout=wait_timeout)
        except asyncio.TimeoutError:
            result = copy.deepcopy(self.cached) if self.cached else self._empty(identity, 'collection_failed')
            result.update(usage_status='collection_failed', usage_source='agy_cli_usage',
                          runtime_auth_status='unknown',
                          usage_note='Quota collection still pending')
            for group in result['groups']:
                for window in group['windows']:
                    window.update(remaining_percent=None, used_percent=None, reset_at=None, stale=True)
        if epoch != self.epoch:
            return self._empty((None,), 'unknown')
        return copy.deepcopy(result)

    @staticmethod
    def _empty(identity, status):
        return {'usage_status': status, 'usage_source': 'agy_cli_usage',
                'runtime_auth_status': 'unknown',
                'quota_account': identity[0] or None, 'usage_collected_at': None,
                'groups': [], 'used_percent': None, 'remaining_percent': None}

    def _read(self, binary, env, cancel):
        # Legacy readers retain their public signature and are serialized too.
        # Queued stale epochs cannot open a PTY after their cancellation.
        with self._reader_lock:
            if self._cleanup_failed:
                raise CleanupIncompleteError('quota_reader_quarantined')
            if cancel.is_set():
                raise InterruptedError('quota_collection_cancelled')
            try:
                if 'cancel_event' in inspect.signature(self.reader).parameters:
                    return self.reader(binary, env, self.timeout, cancel_event=cancel)
                return self.reader(binary, env, self.timeout)
            except CleanupIncompleteError:
                self._cleanup_failed = True
                raise

    async def _collect(self, identity, epoch, binary, env, catalog):
        previous = self.cached
        cancel = self._events[epoch]
        try:
            text = await asyncio.to_thread(self._read, binary, env, cancel)
            groups = parse_usage(text, identity[0], catalog)
            collected = datetime.now(timezone.utc).isoformat()
            for group in groups:
                for window in group['windows']:
                    window.update(account=identity[0], source='agy_cli_usage',
                                  collected_at=collected, stale=False)
            result = {'usage_status': 'available', 'usage_source': 'agy_cli_usage',
                      'runtime_auth_status': 'verified',
                      'quota_account': identity[0], 'usage_collected_at': collected,
                      'usage_note': 'Independent provider quota groups from CLI /usage',
                      'groups': groups}
            failures, delay = 0, self.ttl
        except asyncio.CancelledError:
            cancel.set()
            self._events.pop(epoch, None)
            raise
        except LoginRequiredError:
            # Do not retain the previous account's quota, login flags or
            # measurement time after an explicit runtime authentication failure.
            failures, delay = self.failures + 1, self.backoff
            result = self._empty(identity, 'login_required')
            result.update(quota_account=None, runtime_auth_status='login_required',
                          usage_note='CLI requested login; credential metadata is not runtime authentication')
        except Exception as exc:
            failure_reason = str(exc) if isinstance(exc, (TimeoutError, CleanupIncompleteError, InterruptedError, ValueError)) else type(exc).__name__
            failures = self.failures + 1
            delay = min(300, self.backoff * 2 ** min(failures - 1, 4))
            if isinstance(exc, QuotaIdentityError):
                previous = None
            result = copy.deepcopy(previous) if previous else self._empty(identity, 'collection_failed')
            if isinstance(exc, QuotaIdentityError):
                result['quota_account'] = None
            result['usage_status'] = 'collection_failed'
            result['runtime_auth_status'] = 'unknown'
            result['usage_note'] = 'Provider quota collection failed: ' + failure_reason
            for group in result['groups']:
                for window in group['windows']:
                    window.update(remaining_percent=None, used_percent=None, reset_at=None, stale=True)
        self._events.pop(epoch, None)
        if epoch == self.epoch:
            self.cached, self.next_attempt, self.failures = result, time.monotonic() + delay, failures
        return result
