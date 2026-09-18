#!/usr/bin/env python3
"""Hub TLS: self-signed cert via openssl CLI. Stdlib + openssl, no pip."""
from __future__ import annotations

import ipaddress
import socket
import ssl
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import util


def cert_paths() -> tuple[Path, Path]:
    base = util.state_dir()
    return base / "hub.crt", base / "hub.key"


def ca_path() -> Path:
    raw = util.env_opt("HUB_CA")
    if raw:
        return Path(raw)
    crt, _ = cert_paths()
    return crt


def parse_host(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        return (urlparse(text).hostname or "").strip()
    try:
        ipaddress.ip_address(text)
        return text
    except ValueError:
        pass
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            return host.strip()
    return text.split("/")[0].strip()


def classify_names(values: Iterable[str]) -> tuple[list[str], list[str]]:
    dns: set[str] = set()
    ips: set[str] = set()
    for raw in values:
        host = parse_host(raw)
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
            ips.add(host)
        except ValueError:
            dns.add(host.lower())
    dns.add("localhost")
    ips.add("127.0.0.1")
    try:
        hostname = socket.gethostname().strip().lower()
        if hostname:
            try:
                ipaddress.ip_address(hostname)
                ips.add(hostname)
            except ValueError:
                dns.add(hostname)
    except OSError:
        pass
    return sorted(dns), sorted(ips)


def as_https(url: str, default_host: str = "127.0.0.1", default_port: int = 8788) -> str:
    text = (url or "").strip()
    if not text:
        return f"https://{default_host}:{default_port}"
    if text.startswith("http://"):
        text = "https://" + text[7:]
    elif not text.startswith("https://"):
        text = "https://" + text
    parsed = urlparse(text)
    host = parsed.hostname or default_host
    port = parsed.port or default_port
    return f"https://{host}:{port}"


def ensure_hub_cert(dns_names: list[str], ip_names: list[str]) -> tuple[Path, Path]:
    crt, key = cert_paths()
    crt.parent.mkdir(parents=True, exist_ok=True)
    alt_lines = [f"DNS.{i} = {name}" for i, name in enumerate(dns_names, start=1)]
    alt_lines += [f"IP.{i} = {name}" for i, name in enumerate(ip_names, start=1)]
    config = "\n".join(
        [
            "[req]",
            "default_bits = 2048",
            "prompt = no",
            "distinguished_name = dn",
            "x509_extensions = ext",
            "",
            "[dn]",
            "CN = traffic-monitor-hub",
            "",
            "[ext]",
            "basicConstraints = CA:FALSE",
            "keyUsage = digitalSignature, keyEncipherment",
            "extendedKeyUsage = serverAuth",
            "subjectAltName = @alt",
            "",
            "[alt]",
            *alt_lines,
            "",
        ]
    )
    if not shutil_which("openssl"):
        raise SystemExit("openssl is required to create the hub TLS certificate")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "hub.cnf"
        cfg.write_text(config, encoding="utf-8")
        try:
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "825",
                    "-keyout",
                    str(key),
                    "-out",
                    str(crt),
                    "-config",
                    str(cfg),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise SystemExit(exc.stderr or exc.stdout or str(exc)) from exc
    key.chmod(0o600)
    crt.chmod(0o644)
    return crt, key


def shutil_which(name: str) -> Optional[str]:
    from shutil import which

    return which(name)


def client_context() -> ssl.SSLContext:
    ca = ca_path()
    if not ca.is_file():
        raise RuntimeError(f"missing hub TLS CA file: {ca}")
    return ssl.create_default_context(cafile=str(ca))


def server_context() -> ssl.SSLContext:
    crt, key = cert_paths()
    if not crt.is_file() or not key.is_file():
        raise SystemExit(f"hub TLS cert missing: {crt} {key}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(crt), str(key))
    return ctx
