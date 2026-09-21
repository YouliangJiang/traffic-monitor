#!/usr/bin/env python3
"""Shared helpers: caps, node names, JSON, HTTP auth. Stdlib only."""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
CAP_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]i?b?)?$", re.I)


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
    raw = os.environ.get("STATE_DIRECTORY") or os.environ.get("TRAFFIC_MONITOR_STATE_DIR")
    if raw:
        return Path(raw.split(":")[0])
    return Path("/var/lib/traffic-monitor")


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def valid_node_name(name: str) -> bool:
    return bool(NODE_NAME_RE.match(name or ""))


def normalize_node_name(name: str) -> str:
    return (name or "").strip().lower()


def parse_cap(text: str) -> Optional[int]:
    """Return bytes, or None for unlimited."""
    raw = (text or "").strip().lower().replace(" ", "")
    if raw in {"unlimited", "inf", "infinite", "none", "∞", "unlimit", "nolimit"}:
        return None
    if raw in {"0", "0b", "0g", "0t"}:
        return None
    match = CAP_RE.match(raw)
    if not match:
        import i18n
        raise ValueError(i18n.t("error.bad_cap", text=text))
    amount = float(match.group(1))
    unit = (match.group(2) or "g").lower()
    multipliers = {
        "k": 1_000,
        "kb": 1_000,
        "kib": 1024,
        "m": 1_000_000,
        "mb": 1_000_000,
        "mib": 1024**2,
        "g": 1_000_000_000,
        "gb": 1_000_000_000,
        "gib": 1024**3,
        "t": 1_000_000_000_000,
        "tb": 1_000_000_000_000,
        "tib": 1024**4,
    }
    if unit not in multipliers:
        import i18n
        raise ValueError(i18n.t("error.bad_cap_unit", text=text))
    return int(amount * multipliers[unit])


def format_cap(cap: Optional[int]) -> str:
    if cap is None or cap <= 0:
        import i18n

        return i18n.t("cap.unlimited")
    from report import fmt_bytes

    return fmt_bytes(cap)


def cap_pct(used: int, cap: Optional[int]) -> Optional[float]:
    if cap is None or cap <= 0:
        return None
    return used / cap * 100.0


def http_json(
    method: str,
    url: str,
    token: str,
    payload: Optional[dict[str, Any]] = None,
    timeout: int = 30,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        context: Optional[ssl.SSLContext] = None
        if url.startswith("https://"):
            import tlsutil

            context = tlsutil.client_context()
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"hub HTTP {exc.code}: {detail}") from exc
    if not body:
        return {}
    return json.loads(body)
