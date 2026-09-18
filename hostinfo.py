#!/usr/bin/env python3
"""On-demand host snapshot. Stdlib only, cheap enough for a command reply."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from report import fmt_bytes, h, host_label, progress_bar, tcp_443_open, utcnow

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
        return f"{days}天{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分{s}秒"


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


def sample_bandwidth(iface: str, seconds: float = 3.0) -> dict:
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
    net1 = _read_net_bytes(iface)
    procs1 = _proc_sample()
    time.sleep(interval)
    cpu2 = _read_cpu_times()
    net2 = _read_net_bytes(iface)
    procs2 = _proc_sample()

    cpu_total = cpu2[0] - cpu1[0]
    cpu_pct = 100.0 - _pct(cpu2[1] - cpu1[1], cpu_total)
    iowait_pct = _pct(cpu2[2] - cpu1[2], cpu_total)
    steal_pct = _pct(cpu2[3] - cpu1[3], cpu_total)
    elapsed = max(interval, 0.01)
    rx_bps = max(0, net2[0] - net1[0]) * 8 / elapsed
    tx_bps = max(0, net2[1] - net1[1]) * 8 / elapsed

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
        uptime_sec=_read_uptime(),
        xray_ok=tcp_443_open(),
        xray_rss=xray_rss,
        top_rss=rss_rows[:5],
        top_cpu=cpu_rows[:5],
    )


def _top_lines(rows: list[ProcUse], kind: str) -> str:
    if not rows:
        return "  (无)"
    lines = []
    for item in rows:
        if kind == "rss":
            lines.append(f"  {item.comm:<18} {fmt_mib(item.rss):>8}")
        else:
            lines.append(f"  {item.comm:<18} {item.cpu_pct:5.1f}%")
    return "\n".join(lines)


def format_cpu(info: HostInfo) -> str:
    return (
        f"🧮 <b>{h(host_label())} · CPU</b>\n\n"
        f"利用率 <b>{info.cpu_pct:.1f}%</b>  {progress_bar(info.cpu_pct)}\n"
        f"steal {info.steal_pct:.1f}% · iowait {info.iowait_pct:.1f}% · {info.nproc} 核\n"
        f"load {info.load1:.2f} / {info.load5:.2f} / {info.load15:.2f}\n\n"
        f"<pre>占用最高\n{_top_lines(info.top_cpu, 'cpu')}</pre>"
    )


def format_mem(info: HostInfo) -> str:
    used = max(0, info.mem_total - info.mem_available)
    pct = 100.0 * used / info.mem_total if info.mem_total else 0.0
    swap_used = max(0, info.swap_total - info.swap_free)
    swap_pct = 100.0 * swap_used / info.swap_total if info.swap_total else 0.0
    return (
        f"🧠 <b>{h(host_label())} · 内存</b>\n\n"
        f"RAM <b>{fmt_mib(used)} / {fmt_mib(info.mem_total)}</b>  ({pct:.0f}%)\n"
        f"{progress_bar(pct)}\n"
        f"可用 {fmt_mib(info.mem_available)} · swap {fmt_mib(swap_used)} / {fmt_mib(info.swap_total)} ({swap_pct:.0f}%)\n\n"
        f"<pre>RSS 最高\n{_top_lines(info.top_rss, 'rss')}</pre>"
    )


def format_disk(info: HostInfo) -> str:
    pct = 100.0 * info.disk_used / info.disk_total if info.disk_total else 0.0
    return (
        f"💾 <b>{h(host_label())} · 磁盘 /</b>\n\n"
        f"<b>{fmt_bytes(info.disk_used)} / {fmt_bytes(info.disk_total)}</b>  ({pct:.0f}%)\n"
        f"{progress_bar(pct)}\n"
        f"剩余 {fmt_bytes(info.disk_avail)}"
    )


def format_net(info: HostInfo, iface: str) -> str:
    return (
        f"🌐 <b>{h(host_label())} · 网卡 {h(iface)}</b>\n\n"
        f"瞬时  ↓ <b>{h(fmt_bps(info.net_rx_bps))}</b>  ↑ <b>{h(fmt_bps(info.net_tx_bps))}</b>\n"
        f"采样约 0.4 秒，含入站+出站。"
    )


def format_uptime(info: HostInfo) -> str:
    local = info.now.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"⏱ <b>{h(host_label())} · 运行时间</b>\n\n"
        f"已开机 <b>{h(fmt_duration(info.uptime_sec))}</b>\n"
        f"主机名 <code>{h(info.hostname)}</code>\n"
        f"北京时间 <code>{h(local)}</code>"
    )


def format_xray(info: HostInfo) -> str:
    port = "443 通" if info.xray_ok else "443 不通"
    mark = "✅" if info.xray_ok else "❌"
    rss = fmt_mib(info.xray_rss) if info.xray_rss else "未找到进程"
    return (
        f"🔌 <b>{h(host_label())} · Xray</b>\n\n"
        f"{mark} {h(port)}\n"
        f"进程 RSS {h(rss)}\n"
        f"host 网络，流量走 eth0。"
    )


def format_overview(info: HostInfo, traffic_line: str, iface: str) -> str:
    used = max(0, info.mem_total - info.mem_available)
    mem_pct = 100.0 * used / info.mem_total if info.mem_total else 0.0
    disk_pct = 100.0 * info.disk_used / info.disk_total if info.disk_total else 0.0
    local = info.now.astimezone(CST).strftime("%m-%d %H:%M")
    xray = "healthy" if info.xray_ok else "down"
    return (
        f"🖥 <b>{h(host_label())} 总览</b>  <code>{h(local)}</code>\n\n"
        f"CPU  <b>{info.cpu_pct:.0f}%</b>  {progress_bar(info.cpu_pct, 12)}  "
        f"load {info.load1:.2f}\n"
        f"MEM  <b>{fmt_mib(used)}/{fmt_mib(info.mem_total)}</b>  {progress_bar(mem_pct, 12)}  "
        f"swap {fmt_mib(max(0, info.swap_total - info.swap_free))}\n"
        f"DISK <b>{disk_pct:.0f}%</b>  {progress_bar(disk_pct, 12)}  "
        f"剩 {fmt_bytes(info.disk_avail)}\n"
        f"NET  ↓{h(fmt_bps(info.net_rx_bps))}  ↑{h(fmt_bps(info.net_tx_bps))}  {h(iface)}\n"
        f"XRAY {h(xray)}  rss {h(fmt_mib(info.xray_rss) if info.xray_rss else 'n/a')}  "
        f"up {h(fmt_duration(info.uptime_sec))}\n\n"
        f"{traffic_line}"
    )
