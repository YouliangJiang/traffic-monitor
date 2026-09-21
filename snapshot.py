#!/usr/bin/env python3
"""Build a compact JSON snapshot for hub heartbeats."""
from __future__ import annotations

from typing import Any, Optional

import hostinfo
import report


def build_snapshot(
    iface: str,
    reset_day: int,
    services: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    bootstrap = report.load_json(report.state_dir() / "bootstrap.json")
    snap = report.collect_snapshot(iface, reset_day, bootstrap)
    host = hostinfo.collect_host(iface)
    used = snap.period_rx + snap.period_tx
    return {
        "iface": iface,
        "ts": snap.now.isoformat(),
        "period_start": snap.period_start.isoformat(),
        "period_end": snap.period_end.isoformat(),
        "period_rx": snap.period_rx,
        "period_tx": snap.period_tx,
        "period_total": used,
        "today_rx": snap.today.rx,
        "today_tx": snap.today.tx,
        "today_total": snap.today.total,
        "vnstat_ok": snap.vnstat_ok,
        "bootstrap_applied": snap.bootstrap_applied,
        "cpu_pct": host.cpu_pct,
        "steal_pct": host.steal_pct,
        "load1": host.load1,
        "nproc": host.nproc,
        "mem_total": host.mem_total,
        "mem_available": host.mem_available,
        "swap_total": host.swap_total,
        "swap_free": host.swap_free,
        "disk_total": host.disk_total,
        "disk_used": host.disk_used,
        "disk_avail": host.disk_avail,
        "net_rx_bps": host.net_rx_bps,
        "net_tx_bps": host.net_tx_bps,
        "net_window_sec": host.net_window_sec,
        "uptime_sec": host.uptime_sec,
        "svc": hostinfo.probe_services(services or []),
        "hostname": host.hostname,
    }
