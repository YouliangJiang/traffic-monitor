#!/usr/bin/env python3
"""On-demand host snapshot. Stdlib only, cheap enough for a command reply."""
from __future__ import annotations

import http.client
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from report import fmt_bytes, h, host_label, progress_bar, tcp_443_open, utcnow

import i18n

CST = timezone(timedelta(hours=8))


@dataclass
class ProcUse:
    comm: str
    rss: int
    cpu_pct: float = 0.0


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
    iowait_pct: float
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
    xray_ok: bool
    xray_rss: int
    top_rss: list[ProcUse] = field(default_factory=list)
    top_cpu: list[ProcUse] = field(default_factory=list)


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


def collect_host(iface: str = "eth0", interval: float = 0.35) -> HostInfo:
    cpu1 = _read_cpu_times()
    procs1 = _proc_sample()
    time.sleep(interval)
    cpu2 = _read_cpu_times()
    procs2 = _proc_sample()
    rx_bps, tx_bps, net_window_sec = iface_bitrate(iface)

    cpu_total = cpu2[0] - cpu1[0]
    cpu_pct = 100.0 - _pct(cpu2[1] - cpu1[1], cpu_total)
    iowait_pct = _pct(cpu2[2] - cpu1[2], cpu_total)
    steal_pct = _pct(cpu2[3] - cpu1[3], cpu_total)

    cpu_rows: list[ProcUse] = []
    rss_rows: list[ProcUse] = []
    for pid, (comm, rss, cpu_now) in procs2.items():
        rss_rows.append(ProcUse(comm=comm, rss=rss))
        prev = procs1.get(pid)
        if prev is None:
            continue
        cpu_rows.append(ProcUse(comm=comm, rss=rss, cpu_pct=_pct(cpu_now - prev[2], cpu_total)))
    rss_rows.sort(key=lambda item: item.rss, reverse=True)
    cpu_rows.sort(key=lambda item: item.cpu_pct, reverse=True)

    mem = _read_meminfo()
    load1, load5, load15, nproc = _read_load()
    disk_total, disk_used, disk_avail = _disk("/")
    xray_rss = 0
    for item in rss_rows:
        if item.comm == "xray":
            xray_rss += item.rss
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
        iowait_pct=iowait_pct,
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
        xray_ok=tcp_443_open(),
        xray_rss=xray_rss,
        top_rss=rss_rows[:5],
        top_cpu=cpu_rows[:5],
    )


def _top_lines(rows: list[ProcUse], kind: str) -> str:
    if not rows:
        return i18n.t("host.none")
    lines = []
    for item in rows:
        if kind == "rss":
            lines.append(f"  {item.comm:<18} {fmt_mib(item.rss):>8}")
        else:
            lines.append(f"  {item.comm:<18} {item.cpu_pct:5.1f}%")
    return "\n".join(lines)


def format_cpu(info: HostInfo) -> str:
    return i18n.t(
        "host.cpu",
        name=h(host_label()),
        pct=info.cpu_pct,
        bar=progress_bar(info.cpu_pct),
        steal=info.steal_pct,
        iowait=info.iowait_pct,
        nproc=info.nproc,
        load1=info.load1,
        load5=info.load5,
        load15=info.load15,
        top_title=i18n.t("host.top_cpu"),
        top=_top_lines(info.top_cpu, "cpu"),
    )


def format_mem(info: HostInfo) -> str:
    used = max(0, info.mem_total - info.mem_available)
    pct = 100.0 * used / info.mem_total if info.mem_total else 0.0
    swap_used = max(0, info.swap_total - info.swap_free)
    swap_pct = 100.0 * swap_used / info.swap_total if info.swap_total else 0.0
    return i18n.t(
        "host.mem",
        name=h(host_label()),
        used=fmt_mib(used),
        total=fmt_mib(info.mem_total),
        pct=pct,
        bar=progress_bar(pct),
        avail=fmt_mib(info.mem_available),
        swap_used=fmt_mib(swap_used),
        swap_total=fmt_mib(info.swap_total),
        swap_pct=swap_pct,
        top_title=i18n.t("host.top_rss"),
        top=_top_lines(info.top_rss, "rss"),
    )


def format_disk(info: HostInfo) -> str:
    pct = 100.0 * info.disk_used / info.disk_total if info.disk_total else 0.0
    return i18n.t(
        "host.disk",
        name=h(host_label()),
        used=fmt_bytes(info.disk_used),
        total=fmt_bytes(info.disk_total),
        pct=pct,
        bar=progress_bar(pct),
        avail=fmt_bytes(info.disk_avail),
    )


def format_net(info: HostInfo, iface: str) -> str:
    return i18n.t(
        "host.net",
        name=h(host_label()),
        iface=h(iface),
        rx=h(fmt_bps(info.net_rx_bps)),
        tx=h(fmt_bps(info.net_tx_bps)),
    )


def format_uptime(info: HostInfo) -> str:
    local = info.now.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    return i18n.t(
        "host.uptime",
        name=h(host_label()),
        uptime=h(fmt_duration(info.uptime_sec)),
        hostname=h(info.hostname),
        local=h(local),
    )


def format_xray(info: HostInfo) -> str:
    port = i18n.t("metric.xray_ok") if info.xray_ok else i18n.t("metric.xray_down")
    mark = "✅" if info.xray_ok else "❌"
    rss = fmt_mib(info.xray_rss) if info.xray_rss else i18n.t("host.no_proc")
    return i18n.t(
        "host.xray",
        name=h(host_label()),
        mark=mark,
        port=h(port),
        rss=h(rss),
    )


def format_overview(info: HostInfo, traffic_line: str, iface: str) -> str:
    used = max(0, info.mem_total - info.mem_available)
    mem_pct = 100.0 * used / info.mem_total if info.mem_total else 0.0
    disk_pct = 100.0 * info.disk_used / info.disk_total if info.disk_total else 0.0
    local = info.now.astimezone(CST).strftime("%m-%d %H:%M")
    xray = "healthy" if info.xray_ok else "down"
    return i18n.t(
        "host.overview",
        name=h(host_label()),
        local=h(local),
        cpu=info.cpu_pct,
        cpu_bar=progress_bar(info.cpu_pct, 12),
        load=info.load1,
        mem_used=fmt_mib(used),
        mem_total=fmt_mib(info.mem_total),
        mem_bar=progress_bar(mem_pct, 12),
        swap=fmt_mib(max(0, info.swap_total - info.swap_free)),
        disk_pct=disk_pct,
        disk_bar=progress_bar(disk_pct, 12),
        disk_avail=fmt_bytes(info.disk_avail),
        rx=h(fmt_bps(info.net_rx_bps)),
        tx=h(fmt_bps(info.net_tx_bps)),
        iface=h(iface),
        xray=h(xray),
        rss=h(fmt_mib(info.xray_rss) if info.xray_rss else "n/a"),
        uptime=h(fmt_duration(info.uptime_sec)),
        traffic=traffic_line,
    )
