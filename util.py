#!/usr/bin/env python3
"""Shared helpers: env, state files, node names, caps, reset times. Stdlib only."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
CAP_RE = re.compile(r"^(\d+(?:\.\d+)?)([kmgt]i?b?)?$")
CAP_UNITS = {
    "k": 10**3, "kb": 10**3, "kib": 1024,
    "m": 10**6, "mb": 10**6, "mib": 1024**2,
    "g": 10**9, "gb": 10**9, "gib": 1024**3,
    "t": 10**12, "tb": 10**12, "tib": 1024**4,
}


def local_tz():
    """Timezone configured on this host (/etc/localtime)."""
    return datetime.now().astimezone().tzinfo


def env(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"missing environment variable {name}")
    return value


def env_opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def state_dir() -> Path:
    raw = os.environ.get("TRAFFIC_MONITOR_STATE_DIR") or os.environ.get("STATE_DIRECTORY")
    if raw:
        return Path(raw.split(":")[0])
    return Path("/var/lib/traffic-monitor")


class StateError(RuntimeError):
    """A required state file is missing, unreadable, or invalid."""


def load_json(path: Path, *, strict: bool = False) -> dict[str, Any]:
    if not path.is_file():
        if strict:
            raise StateError(f"missing state file: {path.name}")
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise StateError(f"not a regular state file: {path.name}")
            data = json.load(stream)
        if not isinstance(data, dict):
            raise StateError(f"invalid state object: {path.name}")
        return data
    except (OSError, ValueError, StateError) as exc:
        if strict:
            raise StateError(f"cannot read state file: {path.name}") from exc
        return {}


def save_json(path: Path, data: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def valid_node_name(name: str) -> bool:
    return bool(NODE_NAME_RE.match(name or ""))


def normalize_node_name(name: str) -> str:
    return (name or "").strip().lower()


def parse_reset_time(text: str) -> str:
    """HH, HH:MM, or HH:MM:SS. Omitted minutes and seconds are 0."""
    raw = (text or "").strip()
    if not raw:
        return "00:00:00"
    parts = raw.split(":")
    parts += ["0"] * (3 - len(parts))
    try:
        if len(parts) != 3:
            raise ValueError
        hour, minute, second = (int(part) for part in parts)
    except ValueError:
        raise ValueError(f"invalid reset time: {text}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"invalid reset time: {text}")
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def parse_reset(text: str) -> tuple[int, str]:
    """Parse 27, 27T08, 27T08:30, or 27T08:00:05 into (day, HH:MM:SS)."""
    raw = (text or "").strip()
    day_s, _, time_s = raw.partition("T")
    try:
        day = int(day_s)
    except ValueError:
        raise ValueError(f"invalid reset: {text}") from None
    if not 1 <= day <= 31:
        raise ValueError(f"invalid reset: {text}")
    return day, parse_reset_time(time_s)


def parse_cap(text: str) -> Optional[int]:
    """Decimal bytes (2T = 2*10**12), or None for unlimited."""
    raw = (text or "").strip().lower().replace(" ", "")
    if raw in {"", "0", "unlimited", "none"}:
        return None
    match = CAP_RE.match(raw)
    if not match or (match.group(2) or "g") not in CAP_UNITS:
        raise ValueError(f"invalid cap: {text}")
    value = int(float(match.group(1)) * CAP_UNITS[match.group(2) or "g"])
    return value or None
