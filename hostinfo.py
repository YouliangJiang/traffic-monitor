#!/usr/bin/env python3
"""Host resource sample from /proc: CPU, load, memory, disk, NIC rate, uptime."""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Any


def _cpu_times() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as stream:
        fields = [int(x) for x in stream.readline().split()[1:9]]
    return sum(fields), fields[3] + fields[4]


def _meminfo() -> dict[str, int]:
    data: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as stream:
        for line in stream:
            key, _, rest = line.partition(":")
            fields = rest.split()
            if fields:
                data[key] = int(fields[0]) * 1024
    return data


def _net_bytes(iface: str) -> tuple[int, int]:
    with open("/proc/net/dev", encoding="utf-8") as stream:
        for line in stream:
            label, _, rest = line.partition(":")
            if label.strip() == iface:
                fields = rest.split()
                return int(fields[0]), int(fields[8])
    return 0, 0


_last_net: dict[str, tuple[float, int, int]] = {}


def _net_rate(iface: str) -> tuple[float, float]:
    """Bytes/sec since the previous call for this iface."""
    rx, tx = _net_bytes(iface)
    now = time.monotonic()
    previous = _last_net.get(iface)
    _last_net[iface] = (now, rx, tx)
    if previous is None or now - previous[0] < 0.2:
        return 0.0, 0.0
    elapsed = now - previous[0]
    return max(0, rx - previous[1]) / elapsed, max(0, tx - previous[2]) / elapsed


def collect(iface: str, interval: float = 0.5) -> dict[str, Any]:
    total1, idle1 = _cpu_times()
    time.sleep(interval)
    total2, idle2 = _cpu_times()
    busy = total2 - total1
    cpu_pct = 100.0 * (busy - (idle2 - idle1)) / busy if busy > 0 else 0.0
    mem = _meminfo()
    disk = os.statvfs("/")
    rx_rate, tx_rate = _net_rate(iface)
    return {
        "hostname": socket.gethostname(),
        "uptime_sec": float(Path("/proc/uptime").read_text().split()[0]),
        "cpu_pct": round(max(0.0, min(100.0, cpu_pct)), 1),
        "load1": float(Path("/proc/loadavg").read_text().split()[0]),
        "nproc": os.cpu_count() or 1,
        "mem_total": mem.get("MemTotal", 0),
        "mem_available": mem.get("MemAvailable", 0),
        "disk_total": disk.f_frsize * disk.f_blocks,
        "disk_used": disk.f_frsize * (disk.f_blocks - disk.f_bfree),
        "disk_avail": disk.f_frsize * disk.f_bavail,
        "net_rx_rate": round(rx_rate, 1),
        "net_tx_rate": round(tx_rate, 1),
    }
