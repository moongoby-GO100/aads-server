"""체크포인트 patch 열 암호화(v1) 시험용 도우미 — 러너 구현과 별개로 형식 명세만으로 만든 독립 구현.

형식: b"v1:" + nonce(16) + 암호문 + tag(32).
  tag = HMAC-SHA256(mac키, b"v1:" + job_id + b"\\n" + base_sha + b"\\0" + nonce + 암호문)
  mac키 = HMAC(master, "aads-ckpt-v1|mac"),  비밀번호 = hex(HMAC(master, "aads-ckpt-v1|enc|" + nonce))
  AES-256-CTR 키·IV = PBKDF2-HMAC-SHA256(비밀번호, 소금 없음, 1회) 48바이트.
러너는 같은 값을 openssl `-pbkdf2 -iter 1 -nosalt -pass fd:N` 으로 만들고, 여기서는 -K/-iv 로 풀어 서로 맞는지 본다.
"""
import hashlib
import hmac
import os
import secrets
import subprocess
from pathlib import Path

VER = b"v1:"
NONCE, TAG = 16, 32


def make_key_file(directory: Path, name: str = "checkpoint.key", mode: int = 0o600, content: str | None = None) -> Path:
    path = Path(directory) / name
    if content is not None or not path.exists():
        path.write_text((content if content is not None else secrets.token_hex(32)) + "\n")
    os.chmod(path, mode)
    return path


def key_env(directory: Path, **kw) -> dict:
    return {"RUNNER_CHECKPOINT_KEY_FILE": str(make_key_file(directory, **kw))}


def _master(key_file) -> bytes:
    return bytes.fromhex(Path(key_file).read_text().strip())


def _ctr(passphrase: bytes, data: bytes, decrypt: bool) -> bytes:
    d = hashlib.pbkdf2_hmac("sha256", passphrase, b"", 1, 48)
    cmd = ["openssl", "enc"] + (["-d"] if decrypt else []) + ["-aes-256-ctr", "-K", d[:32].hex(), "-iv", d[32:].hex()]
    return subprocess.run(cmd, input=data, capture_output=True, check=True).stdout


def _tag(mac_key: bytes, aad: bytes, nonce: bytes, ct: bytes) -> bytes:
    return hmac.new(mac_key, VER + aad + b"\0" + nonce + ct, hashlib.sha256).digest()


def encrypt(key_file, job_id: str, base_sha: str, plain: bytes) -> bytes:
    master = _master(key_file)
    nonce = secrets.token_bytes(NONCE)
    mac_key = hmac.new(master, b"aads-ckpt-v1|mac", hashlib.sha256).digest()
    pw = hmac.new(master, b"aads-ckpt-v1|enc|" + nonce, hashlib.sha256).hexdigest().encode()
    ct = _ctr(pw, plain, False)
    return VER + nonce + ct + _tag(mac_key, f"{job_id}\n{base_sha}".encode(), nonce, ct)


def decrypt(key_file, job_id: str, base_sha: str, blob: bytes) -> bytes:
    master = _master(key_file)
    assert blob.startswith(VER)
    nonce, ct, tag = blob[len(VER):len(VER) + NONCE], blob[len(VER) + NONCE:-TAG], blob[-TAG:]
    mac_key = hmac.new(master, b"aads-ckpt-v1|mac", hashlib.sha256).digest()
    assert hmac.compare_digest(tag, _tag(mac_key, f"{job_id}\n{base_sha}".encode(), nonce, ct))
    pw = hmac.new(master, b"aads-ckpt-v1|enc|" + nonce, hashlib.sha256).hexdigest().encode()
    return _ctr(pw, ct, True)
