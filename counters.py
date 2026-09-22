#!/usr/bin/env python3
"""One NIC timeline. Each bucket is one UTC minute."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import util

MINUTE_FMT = "%Y-%m-%dT%H:%M"


def _iface_bytes(iface: str) -> tuple[int, int]:
    with open("/proc/net/dev", encoding="utf-8") as fh:
        for line in fh:
            label, _, rest = line.partition(":")
            if label.strip() == iface:
                parts = rest.split()
                return int(parts[0]), int(parts[8])
    raise RuntimeError(f"interface {iface} not found")


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _path() -> Path:
    return util.state_dir() / "traffic.json"


def _load() -> dict[str, Any]:
    return util.load_json(_path())


def _save(data: dict[str, Any]) -> None:
    util.save_json(_path(), data)


def _minute_key(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    return when.strftime(MINUTE_FMT)


def _parse_minute(key: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(key), MINUTE_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fold_legacy_days(data: dict[str, Any]) -> None:
    """Move old per-day totals into the minute timeline once, then drop them.

    A day total has no clock time. It is placed at 00:00 UTC, which is the
    same default used when a reset is configured as a day only.
    """
    days = data.get("days")
    if not isinstance(days, dict) or not days:
        data.pop("days", None)
        return
    minutes: dict[str, Any] = data.setdefault("minutes", {})
    covered: dict[str, tuple[int, int]] = {}
    for key, entry in minutes.items():
        text = str(key)
        if len(text) < 10:
            continue
        day = text[:10]
        drx = int((entry or {}).get("rx") or 0)
        dtx = int((entry or {}).get("tx") or 0)
        prev = covered.get(day, (0, 0))
        covered[day] = (prev[0] + drx, prev[1] + dtx)
    for day_key, entry in days.items():
        day = str(day_key)
        day_rx = int((entry or {}).get("rx") or 0)
        day_tx = int((entry or {}).get("tx") or 0)
        got = covered.get(day)
        extra_rx = day_rx if got is None else day_rx - got[0]
        extra_tx = day_tx if got is None else day_tx - got[1]
        if extra_rx <= 0 and extra_tx <= 0:
            continue
        slot_key = f"{day}T00:00"
        slot = minutes.setdefault(slot_key, {"rx": 0, "tx": 0})
        slot["rx"] = int(slot.get("rx") or 0) + max(0, extra_rx)
        slot["tx"] = int(slot.get("tx") or 0) + max(0, extra_tx)
    data.pop("days", None)


def record_sample(iface: str, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    rx, tx = _iface_bytes(iface)
    boot = _boot_id()
    data = _load()
    if not data:
        data = {
            "iface": iface,
            "boot_id": boot,
            "last_rx": rx,
            "last_tx": tx,
            "last_ts": now.isoformat(),
            "minutes": {},
        }
        _save(data)
        return
    _fold_legacy_days(data)
    minutes: dict[str, Any] = data.setdefault("minutes", {})
    same_boot = boot and boot == data.get("boot_id")
    last_rx = int(data.get("last_rx") or 0)
    last_tx = int(data.get("last_tx") or 0)
    if same_boot and rx >= last_rx and tx >= last_tx:
        key = _minute_key(now)
        slot = minutes.setdefault(key, {"rx": 0, "tx": 0})
        slot["rx"] = int(slot.get("rx") or 0) + (rx - last_rx)
        slot["tx"] = int(slot.get("tx") or 0) + (tx - last_tx)
    cutoff = (now.date() - timedelta(days=400)).isoformat()
    data["minutes"] = {key: value for key, value in minutes.items() if str(key) >= cutoff}
    data["iface"] = iface
    data["boot_id"] = boot
    data["last_rx"] = rx
    data["last_tx"] = tx
    data["last_ts"] = now.isoformat()
    _save(data)


def _as_utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def sum_between(start: datetime, end: datetime) -> tuple[int, int]:
    """Bytes whose minute starts in [start, end)."""
    start = _as_utc(start)
    end = _as_utc(end)
    rx = tx = 0
    for key, entry in (_load().get("minutes") or {}).items():
        ts = _parse_minute(str(key))
        if ts is None or not (start <= ts < end):
            continue
        rx += int((entry or {}).get("rx") or 0)
        tx += int((entry or {}).get("tx") or 0)
    return rx, tx


def day_rows() -> list[tuple[date, int, int]]:
    """Per local-day totals. Minute keys stay absolute UTC instants."""
    totals: dict[date, list[int]] = {}
    for key, entry in (_load().get("minutes") or {}).items():
        ts = _parse_minute(str(key))
        if ts is None:
            continue
        bucket = totals.setdefault(ts.astimezone(util.local_tz()).date(), [0, 0])
        bucket[0] += int((entry or {}).get("rx") or 0)
        bucket[1] += int((entry or {}).get("tx") or 0)
    return [(day, rx, tx) for day, (rx, tx) in sorted(totals.items())]
