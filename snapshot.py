#!/usr/bin/env python3
"""One node snapshot: host resources plus billing-period traffic. Shared by agent and hub."""
from __future__ import annotations

import math
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

import counters
import hostinfo
import util

INT_KEYS = (
    "mem_total", "mem_available", "disk_total", "disk_used", "disk_avail", "nproc",
    "period_rx", "period_tx", "today_rx", "today_tx", "yesterday_rx", "yesterday_tx",
)
NUM_KEYS = ("uptime_sec", "cpu_pct", "load1", "net_rx_rate", "net_tx_rate")
TRAFFIC_KEYS = ("period_rx", "period_tx", "today_rx", "today_tx", "yesterday_rx", "yesterday_tx")


def config_from_env() -> dict[str, Any]:
    """Billing settings for this host from /etc/traffic-monitor.env."""
    return {
        "iface": util.env_opt("TRAFFIC_IFACE", "eth0"),
        "cap_bytes": util.env_int("MONTHLY_CAP_BYTES", 0) or None,
        "reset_day": util.env_int("BILLING_RESET_DAY", 1),
        "reset_time": util.parse_reset_time(util.env_opt("BILLING_RESET_TIME", "00:00:00")),
    }


def build(config: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """Never raises for accounting failures: traffic_ok=False carries the error instead,
    so the hub still gets resource metrics and can alert on the broken ledger."""
    snap: dict[str, Any] = {"ts": (now or datetime.now(timezone.utc)).isoformat(), "iface": config["iface"]}
    snap.update(hostinfo.collect(config["iface"]))
    snap.update(cap_bytes=config["cap_bytes"], reset_day=config["reset_day"], reset_time=config["reset_time"])
    try:
        snap.update(counters.usage(config["iface"], config["reset_day"], config["reset_time"], now))
        snap.update(traffic_ok=True, traffic_error="")
    except Exception as exc:
        traceback.print_exc()
        snap.update({key: 0 for key in TRAFFIC_KEYS})
        snap.update(period_start="", period_end="", traffic_ok=False, traffic_error=f"{type(exc).__name__}: {exc}"[:200])
    return snap


def validate(value: Any) -> dict[str, Any]:
    """Reject malformed agent input before it reaches hub state or Telegram text."""
    if not isinstance(value, dict):
        raise ValueError("snapshot must be an object")
    for key in INT_KEYS:
        if type(value.get(key)) is not int or not 0 <= value[key] <= 2**63 - 1:
            raise ValueError(f"invalid {key}")
    for key in NUM_KEYS:
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise ValueError(f"invalid {key}")
    cap = value.get("cap_bytes")
    if cap is not None and (type(cap) is not int or cap <= 0):
        raise ValueError("invalid cap_bytes")
    if type(value.get("reset_day")) is not int or not 1 <= value["reset_day"] <= 31:
        raise ValueError("invalid reset_day")
    if type(value.get("traffic_ok")) is not bool:
        raise ValueError("invalid traffic_ok")
    for key in ("ts", "iface", "hostname", "period_start", "period_end", "traffic_error", "reset_time"):
        if not isinstance(value.get(key), str) or len(value[key]) > 200:
            raise ValueError(f"invalid {key}")
    if value["traffic_ok"] and not value["period_start"]:
        raise ValueError("missing billing period")
    return value
