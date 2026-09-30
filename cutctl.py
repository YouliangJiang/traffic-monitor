#!/usr/bin/env python3
"""Root helper: apply or remove the generic cap-cutoff nftables table."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import fcntl
import hashlib
import json
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import util

TABLE = "trafficmon-cut"
PRIVATE_V4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
LINK_V4 = "169.254.0.0/16"
TG_HOST = "api.telegram.org"


def _run(argv: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def iface_exists(name: str) -> bool:
    return Path(f"/sys/class/net/{name}").exists()


def ssh_ports() -> list[int]:
    ports: set[int] = set()
    try:
        proc = _run(["sshd", "-T"], timeout=5)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.startswith("port "):
                    ports.add(int(line.split()[1]))
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass
    if not ports:
        ports.add(22)
    return sorted(p for p in ports if 1 <= p <= 65535)


def resolve_host(host: str, port: int) -> tuple[list[str], list[str]]:
    v4: list[str] = []
    v6: list[str] = []
    if not host:
        return v4, v6
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return v4, v6
    for fam, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if fam == socket.AF_INET:
            if ip not in v4:
                v4.append(ip)
        elif fam == socket.AF_INET6:
            if ip not in v6:
                v6.append(ip)
    return v4, v6


def _loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost", "localhost4", "localhost6"}


def hub_endpoint() -> tuple[str, int]:
    raw = util.env_opt("FLEET_HUB_URL") or util.env_opt("HUB_URL")
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    port = int(parsed.port or 8788)
    return host, port


def telegram_needed() -> bool:
    return bool(util.env_opt("TELEGRAM_BOT_TOKEN"))


def is_hub() -> bool:
    role = util.env_opt("ROLE").strip().lower()
    if role == "hub":
        return True
    if role == "agent":
        return False
    return bool(util.env_opt("TELEGRAM_BOT_TOKEN")) and util.env_int("HUB_PORT", 0) > 0


def delete_table() -> None:
    _run(["nft", "delete", "table", "inet", TABLE], timeout=10)


def nft_set_block(name: str, typ: str, addrs: Iterable[str]) -> str:
    items = ", ".join(addrs)
    if not items:
        return f"  set {name} {{\n    type {typ};\n  }}\n"
    return f"  set {name} {{\n    type {typ};\n    elements = {{ {items} }}\n  }}\n"


def collect_endpoints() -> dict:
    """Resolve hub and Telegram now. Keep the previous set if DNS fails."""
    hub_host, hub_port = hub_endpoint()
    hub_v4: list[str] = []
    hub_v6: list[str] = []
    if hub_host and not _loopback_host(hub_host):
        hub_v4, hub_v6 = resolve_host(hub_host, hub_port)
    tg_v4: list[str] = []
    tg_v6: list[str] = []
    if telegram_needed():
        tg_v4, tg_v6 = resolve_host(TG_HOST, 443)
    prev: dict = {}
    import cut
    raw = cut.read_applied()
    if isinstance(raw, dict) and isinstance(raw.get("endpoints"), dict):
        prev = raw["endpoints"]
    if hub_host and not _loopback_host(hub_host) and not hub_v4 and not hub_v6:
        hub_v4 = list(prev.get("hub4") or [])
        hub_v6 = list(prev.get("hub6") or [])
    if telegram_needed() and not tg_v4 and not tg_v6:
        tg_v4 = list(prev.get("tg4") or [])
        tg_v6 = list(prev.get("tg6") or [])
    return {
        "hub_port": hub_port,
        "hub4": sorted(set(hub_v4)),
        "hub6": sorted(set(hub_v6)),
        "tg4": sorted(set(tg_v4)),
        "tg6": sorted(set(tg_v6)),
    }


def tailscale_cgroup_path() -> str:
    direct = Path("/sys/fs/cgroup/system.slice/tailscaled.service")
    if direct.is_dir():
        return "system.slice/tailscaled.service"
    proc = _run(["systemctl", "show", "-p", "ControlGroup", "tailscaled.service"], timeout=3)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ControlGroup="):
            path = line.split("=", 1)[1].strip().lstrip("/")
            if path and Path("/sys/fs/cgroup", path).is_dir():
                return path
    return ""


def tailscale_cgroup_rule(path: str) -> str:
    if not path:
        return ""
    level = path.count("/") + 1
    expr = 'socket cgroupv2 level %d "%s" accept' % (level, path)
    probe = (
        "table inet tmcutprobe {\n"
        "  chain output {\n"
        "    type filter hook output priority filter; policy accept;\n"
        "    %s\n"
        "  }\n"
        "}\n"
    ) % expr
    proc = subprocess.run(
        ["nft", "-c", "-f", "-"],
        input=probe,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return "    " + expr


def append_tailscale_underlay(lines: list[str], direction: str) -> None:
    """Keep the WireGuard/DERP underlay. tailscale0 alone is only the inner tunnel."""
    cg = tailscale_cgroup_path()
    if not iface_exists("tailscale0") and not cg:
        return
    lines.append("    udp dport 41641 accept")
    if direction == "output":
        lines.append("    udp sport 41641 accept")
    rule = tailscale_cgroup_rule(cg)
    if rule:
        lines.append(rule)


def build_nft(want_cut: bool, endpoints: dict = None) -> str:
    if not want_cut:
        return ""
    ssh = ", ".join(str(p) for p in ssh_ports()) or "22"
    endpoints = endpoints if endpoints is not None else collect_endpoints()
    hub_port = int(endpoints["hub_port"])
    hub_v4 = endpoints["hub4"]
    hub_v6 = endpoints["hub6"]
    tg_v4 = endpoints["tg4"]
    tg_v6 = endpoints["tg6"]
    listen_hub = is_hub()
    hub_listen = util.env_int("HUB_PORT", 8788)
    has_ts = iface_exists("tailscale0")
    priv = ", ".join(PRIVATE_V4)
    lines = [
        f"table inet {TABLE} {{",
        nft_set_block("tg4", "ipv4_addr", tg_v4),
        nft_set_block("tg6", "ipv6_addr", tg_v6),
        nft_set_block("hub4", "ipv4_addr", hub_v4),
        nft_set_block("hub6", "ipv6_addr", hub_v6),
        "  chain input {",
        "    type filter hook input priority filter - 10; policy drop;",
        '    iif "lo" accept',
    ]
    if has_ts:
        lines.append('    iifname "tailscale0" accept')
    append_tailscale_underlay(lines, "input")
    lines.extend(
        [
            f"    ip saddr {{ {priv} }} accept",
            f"    ip saddr {LINK_V4} accept",
            "    ip6 saddr fc00::/7 accept",
            "    ip6 saddr fe80::/10 accept",
            "    ct state established,related accept",
            "    ct state invalid drop",
            f"    tcp dport {{ {ssh} }} accept",
        ]
    )
    if listen_hub:
        lines.append(f"    tcp dport {hub_listen} accept")
    lines.extend(
        [
            "  }",
            "  chain output {",
            "    type filter hook output priority filter - 10; policy drop;",
            '    oif "lo" accept',
        ]
    )
    if has_ts:
        lines.append('    oifname "tailscale0" accept')
    if listen_hub:
        lines.append(f"    tcp sport {hub_listen} ct state established accept")
    append_tailscale_underlay(lines, "output")
    lines.extend(
        [
            f"    ip daddr {{ {priv} }} accept",
            f"    ip daddr {LINK_V4} accept",
            "    ip6 daddr fc00::/7 accept",
            "    ip6 daddr fe80::/10 accept",
            f"    tcp sport {{ {ssh} }} accept",
            f"    ip daddr @hub4 tcp dport {hub_port} accept",
            f"    ip6 daddr @hub6 tcp dport {hub_port} accept",
            "    ip daddr @tg4 tcp dport 443 accept",
            "    ip6 daddr @tg6 tcp dport 443 accept",
            "    udp dport { 53, 67, 68, 123 } accept",
            "    tcp dport 53 accept",
            "    icmp type { echo-reply, destination-unreachable, time-exceeded } accept",
            "    icmpv6 type { destination-unreachable, packet-too-big, time-exceeded, echo-reply, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept",
            "  }",
            "  chain forward {",
            "    type filter hook forward priority filter - 10; policy drop;",
        ]
    )
    if has_ts:
        lines.append('    iifname "tailscale0" accept')
        lines.append('    oifname "tailscale0" accept')
    lines.extend(
        [
            "  }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def _root_state() -> Path:
    import cut
    directory = cut.applied_path().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError("cutoff state directory is not trusted")
    return directory


@contextmanager
def _locked():
    directory = _root_state()
    fd = os.open(directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _kernel_state() -> tuple[bool, str]:
    tables = _run(["nft", "-j", "list", "tables"])
    if tables.returncode:
        raise RuntimeError("cannot inspect nftables tables")
    objects = json.loads(tables.stdout).get("nftables") or []
    exists = any((entry.get("table") or {}).get("name") == TABLE and (entry.get("table") or {}).get("family") == "inet" for entry in objects)
    if not exists:
        return False, ""
    result = _run(["nft", "-j", "list", "table", "inet", TABLE])
    if result.returncode:
        raise RuntimeError("cannot inspect cutoff table")

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in {"handle", "metainfo"}}
        if isinstance(value, list):
            return [clean(v) for v in value if not (isinstance(v, dict) and "metainfo" in v)]
        return value

    digest = hashlib.sha256(json.dumps(clean(json.loads(result.stdout)), sort_keys=True).encode()).hexdigest()
    return True, digest


def apply_rules(want: str, endpoints: dict = None) -> None:
    exists, _ = _kernel_state()
    # Build first. DNS or validation failures must leave existing protection intact.
    text = build_nft(True, endpoints) if want == "cut" else ""
    transaction = (f"delete table inet {TABLE}\n" if exists else "") + text
    if not transaction:
        return
    proc = subprocess.run(["nft", "-f", "-"], input=transaction, capture_output=True, text=True, timeout=20, check=False)
    if proc.returncode:
        raise RuntimeError((proc.stderr or "nft transaction failed").strip()[:500])


def write_applied(applied: dict) -> None:
    import cut
    _root_state()
    util.save_json(cut.applied_path(), applied, mode=0o644)


def _desired() -> dict:
    import cut
    import report
    path = cut.desired_path()
    if not path.exists():
        exists, _ = _kernel_state()
        if exists or cut.applied_path().exists():
            raise RuntimeError("cutoff request is missing; retaining kernel rules")
        return {"want": "pass", "period_key": "", "armed": False}
    if path.lstat().st_size > 65536:
        raise ValueError("cutoff request too large")
    desired = util.load_json(path, strict=True)
    want = desired.get("want")
    if want not in {"cut", "pass"}:
        raise ValueError("invalid cutoff request")
    if want == "cut":
        day = desired.get("reset_day")
        cap = desired.get("cap")
        if type(day) is not int or not 1 <= day <= 31 or type(cap) is not int or cap <= 0:
            raise ValueError("invalid cutoff billing configuration")
        if desired.get("armed") is not True or desired.get("reset_set") is not True:
            raise ValueError("cutoff request is not armed")
        clock = util.parse_reset_time(desired.get("reset_time") or "00:00:00")
        start, _ = report.billing_period(report.utcnow(), day, clock)
        if desired.get("period_key") != start.strftime("%Y-%m-%dT%H:%M:%S"):
            desired = dict(desired, want="pass")
    return desired


def _reconcile() -> int:
    import cut
    import report
    previous = util.load_json(cut.applied_path(), strict=True) if cut.applied_path().exists() else {}
    desired = _desired()
    want = desired["want"]
    endpoints = collect_endpoints() if want == "cut" else {}
    exists, fingerprint = _kernel_state()
    valid = exists if want == "cut" else not exists
    unchanged = previous.get("want") == want and previous.get("ok") is True and previous.get("endpoints", {}) == endpoints
    if valid and unchanged and previous.get("kernel_digest", "") == fingerprint:
        print("cutctl reconcile noop", flush=True)
        return 0
    apply_rules(want, endpoints)
    exists, fingerprint = _kernel_state()
    if exists != (want == "cut"):
        raise RuntimeError("kernel cutoff verification failed")
    write_applied({"want": want, "ok": True, "error": "", "period_key": desired.get("period_key") or "", "ts": report.utcnow().isoformat(), "endpoints": endpoints, "kernel_digest": fingerprint})
    print("cutctl ok want=" + want, flush=True)
    return 0


def main() -> int:
    try:
        with _locked():
            try:
                return _reconcile()
            except Exception as exc:
                import cut
                import report
                previous = cut.read_applied()
                try:
                    exists, _ = _kernel_state()
                    previous["want"] = "cut" if exists else "pass"
                except Exception:
                    previous.setdefault("want", "pass")
                previous.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:500], ts=report.utcnow().isoformat())
                write_applied(previous)
                raise
    except Exception as exc:
        print(f"cutctl failed; protection retained: {type(exc).__name__}: {exc}", flush=True)
        return 1


def reconcile() -> int:
    return main()


if __name__ == "__main__":
    sys.exit(main())
