#!/usr/bin/env python3
"""On-demand host snapshot. Stdlib only, cheap enough for a command reply."""
from __future__ import annotations

import http.client
import os
import re
import socket
import ssl
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from report import utcnow

import i18n

@dataclass
class HostInfo:
    now: datetime
    hostname: str
    nproc: int
    load1: float
    load5: float
    load15: float
    cpu_pct: float
    steal_pct: float
    mem_total: int
    mem_available: int
    swap_total: int
    swap_free: int
    disk_total: int
    disk_used: int
    disk_avail: int
    net_rx_bps: float
    net_tx_bps: float
    net_window_sec: float
    uptime_sec: float


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    if days:
        return i18n.t("dur.dh", days=days, hours=hours, minutes=minutes)
    if hours:
        return i18n.t("dur.hm", hours=hours, minutes=minutes)
    return i18n.t("dur.ms", minutes=minutes, seconds=s)


def fmt_mib(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MiB"


def fmt_bps(n: float) -> str:
    n = max(0.0, float(n))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} Mbps"
    if n >= 1_000:
        return f"{n / 1_000:.1f} Kbps"
    return f"{n:.0f} bps"


def _read_cpu_times() -> tuple[int, int, int, int]:
    with open("/proc/stat", encoding="utf-8") as fh:
        parts = fh.readline().split()
    nums = [int(x) for x in parts[1:11]]
    idle = nums[3]
    iowait = nums[4]
    steal = nums[7]
    total = sum(nums[:8])
    return total, idle, iowait, steal


def _read_net_bytes(iface: str) -> tuple[int, int]:
    with open("/proc/net/dev", encoding="utf-8") as fh:
        for line in fh:
            label, _, rest = line.partition(":")
            if label.strip() != iface:
                continue
            parts = rest.split()
            return int(parts[0]), int(parts[8])
    return 0, 0


_last_net: dict[str, tuple[float, int, int]] = {}


def iface_bitrate(iface: str) -> tuple[float, float, float]:
    """Bits/sec since the previous call for this iface. (rx, tx, window_sec)."""
    rx, tx = _read_net_bytes(iface)
    now = time.monotonic()
    prev = _last_net.get(iface)
    _last_net[iface] = (now, rx, tx)
    if prev is None:
        return 0.0, 0.0, 0.0
    dt = now - prev[0]
    if dt < 0.2:
        return 0.0, 0.0, dt
    return max(0, rx - prev[1]) * 8 / dt, max(0, tx - prev[2]) * 8 / dt, dt


_CF_HOST = "speed.cloudflare.com"
_CF_UA = {"User-Agent": "Mozilla/5.0"}
_CF_DOWN = "/__down?bytes=50000000"
_CF_CHUNK = 65536
_cf_ssl = None


def _cf_ctx() -> ssl.SSLContext:
    global _cf_ssl
    if _cf_ssl is None:
        _cf_ssl = ssl.create_default_context()
    return _cf_ssl


def _cf_conn(timeout: float) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(_CF_HOST, timeout=timeout, context=_cf_ctx())


def _cf_download(seconds: float) -> tuple[int, float]:
    conn = _cf_conn(max(15.0, seconds + 10.0))
    got = 0
    t0 = None
    try:
        while True:
            conn.request("GET", _CF_DOWN, headers=_CF_UA)
            resp = conn.getresponse()
            if resp.status != 200:
                resp.read()
                raise RuntimeError(f"download HTTP {resp.status}")
            if conn.sock is not None:
                conn.sock.settimeout(1.0)
            while True:
                try:
                    chunk = resp.read(_CF_CHUNK)
                except (TimeoutError, socket.timeout):
                    if t0 is not None and time.monotonic() >= t0 + seconds:
                        break
                    continue
                if not chunk:
                    break
                got += len(chunk)
                if t0 is None:
                    t0 = time.monotonic()
                if time.monotonic() >= t0 + seconds:
                    return got, max(0.001, time.monotonic() - t0)
            if t0 is not None and time.monotonic() >= t0 + seconds:
                break
    finally:
        try:
            conn.close()
        except OSError:
            pass
    if t0 is None or got < 64_000:
        raise RuntimeError("download too little data")
    return got, max(0.001, time.monotonic() - t0)


def _cf_upload(seconds: float) -> tuple[int, float]:
    chunk = b"x" * _CF_CHUNK
    hdr = f"{len(chunk):X}\r\n".encode("ascii")
    conn = _cf_conn(20.0)
    sent = 0
    t0 = None
    send_dt = 0.001
    try:
        conn.putrequest("POST", "/__up")
        conn.putheader("User-Agent", "Mozilla/5.0")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("Host", _CF_HOST)
        conn.endheaders()
        if conn.sock is not None:
            conn.sock.settimeout(2.0)
        t0 = time.monotonic()
        deadline = t0 + seconds
        while time.monotonic() < deadline:
            try:
                conn.send(hdr + chunk + b"\r\n")
            except (TimeoutError, socket.timeout, OSError) as exc:
                if sent < 64_000:
                    raise RuntimeError(i18n.t("upload.send_timeout", err=exc)) from exc
                break
            sent += len(chunk)
        send_dt = max(0.001, time.monotonic() - t0)
        try:
            conn.send(b"0\r\n\r\n")
            if conn.sock is not None:
                conn.sock.settimeout(2.0)
            resp = conn.getresponse()
            resp.read()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
    if t0 is None:
        raise RuntimeError(i18n.t("upload.connect_timeout"))
    if sent < 64_000:
        raise RuntimeError(i18n.t("upload.too_little"))
    return sent, send_dt


def sample_bandwidth(iface: str, seconds: float = 3.0) -> dict:
    """Active public-internet speed test, not idle NIC occupancy."""
    seconds = max(1.0, min(15.0, float(seconds)))
    rx, dt_rx = _cf_download(seconds)
    tx, dt_tx = 0, 0.0
    up_error = ""
    try:
        tx, dt_tx = _cf_upload(min(seconds, 3.0))
    except Exception as exc:
        up_error = str(exc)
    return {
        "iface": iface,
        "seconds": round(dt_rx + dt_tx, 3),
        "down_sec": round(dt_rx, 3),
        "up_sec": round(dt_tx, 3),
        "rx_bytes": rx,
        "tx_bytes": tx,
        "rx_bps": rx * 8 / max(dt_rx, 0.001),
        "tx_bps": (tx * 8 / dt_tx) if dt_tx else 0.0,
        "target": "cloudflare",
        "method": "speedtest",
        "up_error": up_error,
    }


def sample_nic(iface: str, seconds: float = 3.0) -> dict:
    """Passive NIC occupancy over a few seconds. No generated traffic."""
    seconds = max(1.0, min(15.0, float(seconds)))
    rx1, tx1 = _read_net_bytes(iface)
    t0 = time.monotonic()
    time.sleep(seconds)
    rx2, tx2 = _read_net_bytes(iface)
    dt = max(0.001, time.monotonic() - t0)
    rx = max(0, rx2 - rx1)
    tx = max(0, tx2 - tx1)
    return {
        "iface": iface,
        "seconds": round(dt, 3),
        "rx_bytes": rx,
        "tx_bytes": tx,
        "rx_bps": rx * 8 / dt,
        "tx_bps": tx * 8 / dt,
        "method": "nic",
    }


def _read_meminfo() -> dict[str, int]:
    data: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            fields = rest.split()
            if not fields:
                continue
            data[key] = int(fields[0]) * 1024
    return data


def _read_load() -> tuple[float, float, float, int]:
    parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
    nproc = os.cpu_count() or 1
    return float(parts[0]), float(parts[1]), float(parts[2]), nproc


def _read_uptime() -> float:
    return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])


def _disk(path: str = "/") -> tuple[int, int, int]:
    st = os.statvfs(path)
    total = st.f_frsize * st.f_blocks
    avail = st.f_frsize * st.f_bavail
    used = total - (st.f_frsize * st.f_bfree)
    return total, used, avail


def _proc_sample() -> dict[int, tuple[str, int, int]]:
    found: dict[int, tuple[str, int, int]] = {}
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            base = f"/proc/{pid}"
            try:
                comm = Path(base, "comm").read_text(encoding="utf-8").strip()
                stat = Path(base, "stat").read_text(encoding="utf-8")
                close = stat.rfind(")")
                fields = stat[close + 2 :].split()
                cpu = int(fields[11]) + int(fields[12])
                rss_pages = int(fields[21])
            except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError, OSError):
                continue
            found[pid] = (comm[:18], rss_pages * 4096, cpu)
    return found


def _pct(delta: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * delta / total))



def listening_ports() -> set[int]:
    """TCP ports in LISTEN state, any local address, from /proc/net/tcp{,6}."""
    found: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            _addr, _sep, hexport = parts[1].rpartition(":")
            try:
                found.add(int(hexport, 16))
            except ValueError:
                continue
    return found


def tcp_port_open(port: int, timeout: float = 0.4, listening: Optional[set[int]] = None) -> bool:
    ports = listening if listening is not None else listening_ports()
    return int(port) in ports


def _rss_in_sample(procs: dict[int, tuple[str, int, int]], comm: str) -> int:
    want = (comm or "").strip()[:18]
    if not want:
        return 0
    total = 0
    for _pid, (name, rss, _cpu) in procs.items():
        if name == want:
            total += rss
    return total


def probe_services(specs: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    listening = listening_ports()
    procs = _proc_sample()
    for spec in specs or []:
        ports = []
        all_ok = True
        for port in spec.get("ports") or []:
            ok = tcp_port_open(int(port), listening=listening)
            ports.append({"port": int(port), "ok": ok})
            if not ok:
                all_ok = False
        if not ports:
            all_ok = False
        rss = _rss_in_sample(procs, str(spec.get("proc") or spec.get("name") or ""))
        out.append({"name": spec.get("name"), "ports": ports, "ok": all_ok, "rss": rss})
    return out


def collect_host(iface: str = "eth0", interval: float = 0.35) -> HostInfo:
    cpu1 = _read_cpu_times()
    time.sleep(interval)
    cpu2 = _read_cpu_times()
    rx_bps, tx_bps, net_window_sec = iface_bitrate(iface)
    cpu_total = cpu2[0] - cpu1[0]
    cpu_pct = 100.0 - _pct(cpu2[1] - cpu1[1], cpu_total)
    steal_pct = _pct(cpu2[3] - cpu1[3], cpu_total)
    mem = _read_meminfo()
    load1, load5, load15, nproc = _read_load()
    disk_total, disk_used, disk_avail = _disk("/")
    try:
        hostname = Path("/proc/sys/kernel/hostname").read_text(encoding="utf-8").strip()
    except OSError:
        hostname = os.uname().nodename
    return HostInfo(
        now=utcnow(),
        hostname=hostname,
        nproc=nproc,
        load1=load1,
        load5=load5,
        load15=load15,
        cpu_pct=cpu_pct,
        steal_pct=steal_pct,
        mem_total=int(mem.get("MemTotal") or 0),
        mem_available=int(mem.get("MemAvailable") or 0),
        swap_total=int(mem.get("SwapTotal") or 0),
        swap_free=int(mem.get("SwapFree") or 0),
        disk_total=disk_total,
        disk_used=disk_used,
        disk_avail=disk_avail,
        net_rx_bps=rx_bps,
        net_tx_bps=tx_bps,
        net_window_sec=net_window_sec,
        uptime_sec=_read_uptime(),
    )


JOB_TYPES = {"bw", "nic", "rtt"}
RTT_SAMPLES = 3
_PING_WAIT = 1
# Unicast addresses. A hostname, or an anycast resolver such as 1.1.1.1,
# is answered from nearby, so the RTT would not be to that region.
REGIONS = (
    ("eu", "194.150.168.168"),
    ("us", "132.163.97.1"),
    ("cn", "202.96.134.133"),
    ("sea", "203.116.1.78"),
)


def _ping_count(samples: int) -> int:
    try:
        count = int(samples)
    except (TypeError, ValueError):
        count = RTT_SAMPLES
    return max(3, min(5, count))


def _ping_ip(ip: str, count: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["ping", "-n", "-c", str(count), "-W", str(_PING_WAIT), ip],
            capture_output=True,
            text=True,
            timeout=count * (_PING_WAIT + 1),
            env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"samples": count, "received": 0, "loss_pct": 100}
    output = proc.stdout or ""
    stats = re.search(r"(\d+) packets transmitted,\s+(\d+) received", output)
    if not stats:
        return {"samples": count, "received": 0, "loss_pct": 100}
    sent, got = int(stats.group(1)), int(stats.group(2))
    row: dict[str, Any] = {
        "samples": sent,
        "received": got,
        "loss_pct": round(100 * (sent - got) / sent) if sent else 100,
    }
    rtt = re.search(r"=\s*([\d.]+)/([\d.]+)/", output)
    if rtt and got:
        row["min_ms"] = round(float(rtt.group(1)), 1)
        row["avg_ms"] = round(float(rtt.group(2)), 1)
    return row


def sample_region_rtt(samples: int = RTT_SAMPLES) -> dict[str, Any]:
    """Ping one unicast address in each region, in parallel."""
    count = _ping_count(samples)

    def run(item: tuple[str, str]) -> dict[str, Any]:
        region, ip = item
        row = _ping_ip(ip, count)
        row["id"] = region
        return row

    with ThreadPoolExecutor(max_workers=len(REGIONS)) as pool:
        regions = list(pool.map(run, REGIONS))
    return {"regions": regions}


def _job_seconds(params: dict[str, Any]) -> float:
    try:
        return float(params.get("seconds") or 3)
    except (TypeError, ValueError):
        return 3.0


def _job_samples(params: dict[str, Any]) -> int:
    try:
        return int(params.get("samples") or RTT_SAMPLES)
    except (TypeError, ValueError):
        return RTT_SAMPLES


def run_sample(job_type: str, params: dict[str, Any], iface: str, *, cutting: bool) -> dict[str, Any]:
    """One measurement job. Bandwidth is refused while the cap cutoff is active."""
    if not isinstance(params, dict):
        params = {}
    if job_type == "bw":
        if cutting:
            raise RuntimeError("cutoff active")
        return sample_bandwidth(iface, _job_seconds(params))
    if job_type == "nic":
        return sample_nic(iface, _job_seconds(params))
    if job_type == "rtt":
        return sample_region_rtt(_job_samples(params))
    raise RuntimeError(f"unknown job {job_type}")
