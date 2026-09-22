#!/usr/bin/env python3
"""Root helper: apply or remove the generic cap-cutoff nftables table."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
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
    raw = util.load_json(util.state_dir() / "cut-applied.json")
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


def build_nft(want_cut: bool) -> str:
    if not want_cut:
        return ""
    ssh = ", ".join(str(p) for p in ssh_ports()) or "22"
    endpoints = collect_endpoints()
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
            "    ct state established,related accept",
            "  }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def apply_rules(want: str) -> None:
    want_cut = want == "cut"
    delete_table()
    if not want_cut:
        return
    text = build_nft(True)
    proc = subprocess.run(
        ["nft", "-f", "-"],
        input=text,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "nft failed").strip()[:500])


def _chown_trafficmon(path: Path) -> None:
    try:
        import grp
        import pwd

        uid = pwd.getpwnam("trafficmon").pw_uid
        gid = grp.getgrnam("trafficmon").gr_gid
        os.chown(path, uid, gid)
    except Exception:
        pass


def write_applied(applied: dict) -> None:
    path = util.state_dir() / "cut-applied.json"
    util.save_json(path, applied)
    _chown_trafficmon(path)


def main() -> int:
    desired = util.load_json(util.state_dir() / "cut-desired.json")
    if not isinstance(desired, dict):
        desired = {}
    want = str(desired.get("want") or "pass")
    if want not in {"cut", "pass"}:
        want = "pass"
    applied = {
        "want": want,
        "ok": False,
        "error": "",
        "period_key": str(desired.get("period_key") or ""),
        "ts": "",
    }
    try:
        import report

        applied["ts"] = report.utcnow().isoformat()
    except Exception:
        pass
    try:
        if not shutil_which_nft():
            raise RuntimeError("nft not found")
        apply_rules(want)
        applied["ok"] = True
        if want == "cut":
            applied["endpoints"] = collect_endpoints()
    except Exception as exc:
        applied["error"] = str(exc)[:500]
        try:
            delete_table()
        except Exception:
            pass
        write_applied(applied)
        print(f"cutctl fail want={want} err={applied['error']}", flush=True)
        return 1
    write_applied(applied)
    print(f"cutctl ok want={want}", flush=True)
    return 0


def shutil_which_nft() -> bool:
    from shutil import which

    return bool(which("nft"))


def reconcile() -> int:
    """Drop a cutoff that belongs to a previous period, and refresh allowlist addresses."""
    path = util.state_dir() / "cut-desired.json"
    desired = util.load_json(path)
    if not isinstance(desired, dict) or not desired:
        print("cutctl reconcile noop", flush=True)
        return 0
    import cut as cutmod
    import report

    reset_day = int(desired.get("reset_day") or util.env_int("BILLING_RESET_DAY", 1))
    try:
        reset_time = util.parse_reset_time(
            str(desired.get("reset_time") or util.env_opt("BILLING_RESET_TIME", "00:00:00"))
        )
    except ValueError:
        reset_time = "00:00:00"
    reset_set = bool(desired.get("reset_set", True))
    if "cap" in desired:
        raw_cap = desired.get("cap")
    else:
        raw_cap = util.env_opt("MONTHLY_CAP_BYTES", "0")
    if raw_cap in (None, "", 0, "0"):
        cap = None
    else:
        try:
            cap = int(raw_cap)
        except (TypeError, ValueError):
            cap = None
    is_armed = cutmod.armed(cap, reset_day, reset_set)
    start, _end = report.billing_period(report.utcnow(), reset_day, reset_time)
    period_key = start.strftime("%Y-%m-%dT%H:%M:%S")
    want = str(desired.get("want") or "pass")
    if want not in {"cut", "pass"}:
        want = "pass"
    rolled = want == "cut" and (not is_armed or str(desired.get("period_key") or "") != period_key)
    if rolled:
        desired.update(
            {
                "want": "pass",
                "period_key": period_key,
                "armed": is_armed,
                "reset_day": reset_day,
                "reset_time": reset_time,
                "reset_set": reset_set,
                "cap": cap,
                "ts": report.utcnow().isoformat(),
            }
        )
        util.save_json(path, desired)
        _chown_trafficmon(path)
        print("cutctl reconcile restore period=%s" % period_key, flush=True)
    applied_now = util.load_json(util.state_dir() / "cut-applied.json")
    if not isinstance(applied_now, dict):
        applied_now = {}
    endpoints = collect_endpoints() if str(desired.get("want") or "pass") == "cut" else {}
    endpoints_changed = str(desired.get("want") or "") == "cut" and (applied_now.get("endpoints") or {}) != endpoints
    applied_want = str(applied_now.get("want") or "")
    current_want = str(desired.get("want") or "pass")
    if (
        not rolled
        and not endpoints_changed
        and applied_want == current_want
        and applied_now.get("ok") is True
    ):
        print("cutctl reconcile noop", flush=True)
        return 0
    return main()


if __name__ == "__main__":
    if "--reconcile" in sys.argv:
        sys.exit(reconcile())
    sys.exit(main())
