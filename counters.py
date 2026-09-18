#!/usr/bin/env python3
"""Persistent per-day eth counters. Replaces vnstat; stdlib only."""
from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import util


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


def _seed_from_vnstat(iface: str) -> dict[str, dict[str, int]]:
    """One-time import if vnstat is already on the box from an older install."""
    days: dict[str, dict[str, int]] = {}
    try:
        proc = subprocess.run(
            ["vnstat", "--json", "d", "-i", iface],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return days
    if proc.returncode != 0 or not proc.stdout.strip():
        return days
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return days
    for item in payload.get("interfaces") or []:
        if item.get("name") != iface:
            continue
        for entry in (item.get("traffic") or {}).get("day") or []:
            raw = entry.get("date") or {}
            try:
                key = date(int(raw["year"]), int(raw["month"]), int(raw["day"])).isoformat()
            except (KeyError, TypeError, ValueError):
                continue
            days[key] = {"rx": int(entry.get("rx") or 0), "tx": int(entry.get("tx") or 0)}
    return days


def record_sample(iface: str, now: Optional[datetime] = None) -> list[tuple[date, int, int]]:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
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
            "days": _seed_from_vnstat(iface),
        }
        data["days"].setdefault(today, {"rx": 0, "tx": 0})
        _save(data)
        return _as_tuples(data)
    days = data.setdefault("days", {})
    days.setdefault(today, {"rx": 0, "tx": 0})
    same_boot = boot and boot == data.get("boot_id")
    last_rx = int(data.get("last_rx") or 0)
    last_tx = int(data.get("last_tx") or 0)
    if same_boot and rx >= last_rx and tx >= last_tx:
        days[today]["rx"] = int(days[today].get("rx") or 0) + (rx - last_rx)
        days[today]["tx"] = int(days[today].get("tx") or 0) + (tx - last_tx)
    cutoff = (now.date() - timedelta(days=400)).isoformat()
    data["days"] = {key: value for key, value in days.items() if key >= cutoff}
    data["iface"] = iface
    data["boot_id"] = boot
    data["last_rx"] = rx
    data["last_tx"] = tx
    data["last_ts"] = now.isoformat()
    _save(data)
    return _as_tuples(data)


def _as_tuples(data: dict[str, Any]) -> list[tuple[date, int, int]]:
    rows: list[tuple[date, int, int]] = []
    for key, entry in (data.get("days") or {}).items():
        try:
            day = date.fromisoformat(key)
        except ValueError:
            continue
        rows.append((day, int(entry.get("rx") or 0), int(entry.get("tx") or 0)))
    rows.sort()
    return rows


def load_days(iface: str) -> list[tuple[date, int, int]]:
    data = _load()
    if not data:
        return record_sample(iface)
    return _as_tuples(data)
