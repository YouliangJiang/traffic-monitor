#!/usr/bin/env python3
"""Decide whether this node should cut public traffic at 90% of cap."""
from __future__ import annotations

from typing import Any, Optional

import report
import util

ALERT_PCT = 80
CUT_PCT = 90
DESIRED_NAME = "cut-desired.json"
APPLIED_NAME = "cut-applied.json"


def armed(cap: Optional[int], reset_day: int, reset_set: bool = True) -> bool:
    if not reset_set:
        return False
    return bool(cap and int(cap) > 0 and int(reset_day or 0) >= 1)


def used_pct(used: int, cap: Optional[int]) -> Optional[float]:
    if not cap or cap <= 0:
        return None
    return (int(used) / cap) * 100.0


def should_cut(
    used: int,
    cap: Optional[int],
    reset_day: int,
    reset_set: bool = True,
) -> bool:
    pct = used_pct(used, cap)
    if pct is None or not armed(cap, reset_day, reset_set):
        return False
    return pct >= CUT_PCT


def desired_path():
    return util.state_dir() / DESIRED_NAME


def applied_path():
    return util.state_dir() / APPLIED_NAME


def read_desired() -> dict[str, Any]:
    data = util.load_json(desired_path())
    return data if isinstance(data, dict) else {}


def read_applied() -> dict[str, Any]:
    data = util.load_json(applied_path())
    return data if isinstance(data, dict) else {}


def force_pass(period_key: str = "") -> None:
    """Ask the root helper to remove the cutoff table."""
    prev = read_desired()
    payload = {
        "want": "pass",
        "period_key": period_key or str(prev.get("period_key") or ""),
        "armed": False,
        "reset_day": int(prev.get("reset_day") or 1),
        "reset_time": str(prev.get("reset_time") or "00:00:00"),
        "reset_set": bool(prev.get("reset_set", True)),
        "cap": prev.get("cap"),
        "ts": report.utcnow().isoformat(),
    }
    if (
        prev.get("want") == "pass"
        and prev.get("armed") is False
        and str(prev.get("period_key") or "") == payload["period_key"]
    ):
        return
    util.save_json(desired_path(), payload)


def sync_desired(
    used: int,
    cap: Optional[int],
    reset_day: int,
    period_key: str,
    reset_time: str = "00:00:00",
    reset_set: bool = True,
    allow_cut: bool = True,
) -> dict[str, Any]:
    try:
        reset_time = util.parse_reset_time(reset_time)
    except ValueError:
        reset_time = "00:00:00"
    is_armed = armed(cap, reset_day, reset_set) if allow_cut else False
    want = "cut" if allow_cut and should_cut(used, cap, reset_day, reset_set) else "pass"
    payload = {
        "want": want,
        "period_key": period_key or "",
        "armed": is_armed,
        "reset_day": int(reset_day or 1),
        "reset_time": reset_time,
        "reset_set": bool(reset_set),
        "cap": cap,
        "ts": report.utcnow().isoformat(),
    }
    prev = read_desired()
    same = (
        prev.get("want") == payload["want"]
        and prev.get("period_key") == payload["period_key"]
        and bool(prev.get("armed")) == payload["armed"]
        and int(prev.get("reset_day") or 0) == payload["reset_day"]
        and str(prev.get("reset_time") or "") == payload["reset_time"]
        and bool(prev.get("reset_set", True)) == payload["reset_set"]
        and prev.get("cap") == payload["cap"]
    )
    if not same:
        util.save_json(desired_path(), payload)
    return status(cap, reset_day, reset_set, allow_cut)


def status(
    cap: Optional[int] = None,
    reset_day: int = 0,
    reset_set: bool = True,
    allow_cut: bool = True,
) -> dict[str, Any]:
    desired = read_desired()
    applied = read_applied()
    want = str(desired.get("want") or "pass")
    got = str(applied.get("want") or "pass")
    if cap is None and reset_day == 0:
        is_armed = bool(desired.get("armed"))
    else:
        is_armed = armed(cap, reset_day, reset_set) and allow_cut
    return {
        "armed": is_armed,
        "want": want,
        "applied": got,
        "ok": applied.get("ok"),
        "error": str(applied.get("error") or ""),
        "period_key": str(desired.get("period_key") or ""),
        "alert_pct": ALERT_PCT,
        "cut_pct": CUT_PCT,
    }
