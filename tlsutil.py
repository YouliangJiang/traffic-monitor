#!/usr/bin/env python3
"""Hub TLS: self-signed cert via the openssl CLI; agents pin its SHA-256 fingerprint."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import util


def cert_paths() -> tuple[Path, Path]:
    base = util.state_dir()
    return base / "hub.crt", base / "hub.key"


def ensure_hub_cert() -> tuple[Path, Path]:
    crt, key = cert_paths()
    if crt.is_file() and key.is_file():
        return crt, key
    if not shutil.which("openssl"):
        raise SystemExit("openssl is required to create the hub TLS certificate")
    crt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_key, tmp_crt = Path(tmp) / "hub.key", Path(tmp) / "hub.crt"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
             "-subj", "/CN=traffic-monitor-hub", "-keyout", str(tmp_key), "-out", str(tmp_crt)],
            check=True, capture_output=True,
        )
        tmp_key.chmod(0o600)
        tmp_crt.chmod(0o644)
        shutil.move(str(tmp_key), key)
        shutil.move(str(tmp_crt), crt)
    return crt, key


def normalize_fingerprint(text: str) -> str:
    return (text or "").replace(":", "").strip().lower()


def fingerprint(crt: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(crt.read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def server_context() -> ssl.SSLContext:
    crt, key = cert_paths()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(crt), str(key))
    return ctx


class PinError(RuntimeError):
    """The hub presented a certificate other than the pinned one."""


class HubError(RuntimeError):
    def __init__(self, code: int, detail: str) -> None:
        super().__init__(f"hub HTTP {code}: {detail}")
        self.code = code


def post_json(url: str, pin: str, token: str, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """POST to the hub. The certificate is checked against the pin before the token is sent."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"hub URL must be https://HOST:PORT, got {url!r}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 8788, timeout=timeout, context=ctx)
    try:
        conn.connect()
        got = hashlib.sha256(conn.sock.getpeercert(binary_form=True) or b"").hexdigest()
        if not hmac.compare_digest(got, normalize_fingerprint(pin)):
            raise PinError(f"hub certificate fingerprint mismatch: {got}")
        body = json.dumps(payload).encode()
        conn.request("POST", parsed.path or "/", body=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connection": "close",
        })
        resp = conn.getresponse()
        raw = resp.read(65536).decode("utf-8", errors="replace")
        if resp.status != 200:
            raise HubError(resp.status, raw[:200])
        return json.loads(raw) if raw else {}
    finally:
        conn.close()


if __name__ == "__main__":
    # install.sh: create the cert if needed and print its fingerprint.
    os.umask(0o077)
    print(fingerprint(ensure_hub_cert()[0]))
