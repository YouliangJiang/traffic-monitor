#!/usr/bin/env python3
"""Shared helpers: caps, node names, JSON, HTTP auth. Stdlib only."""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
MAX_SVC = 4
MAX_SVC_PORTS = 8
CAP_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]i?b?)?$", re.I)


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


def parse_reset_time(text: str) -> str:
    """HH, HH:MM, or HH:MM:SS. Omitted minutes and seconds are 0."""
    raw = (text or "").strip()
    if not raw:
        return "00:00:00"
    parts = raw.replace(".", ":").split(":")
    if len(parts) == 1:
        parts += ["0", "0"]
    elif len(parts) == 2:
        parts.append("0")
    if len(parts) != 3:
        import i18n
        raise ValueError(i18n.t("error.bad_reset", text=text))
    try:
        hour, minute, second = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        import i18n
        raise ValueError(i18n.t("error.bad_reset", text=text)) from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        import i18n
        raise ValueError(i18n.t("error.bad_reset", text=text))
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def parse_reset(text: str) -> tuple[int, str]:
    """Parse 27, 27T08, 27T08:30, or 27T08:00:05. Missing minute and second are 0."""
    raw = (text or "").strip()
    if not raw:
        import i18n
        raise ValueError(i18n.t("error.bad_reset", text=text))
    if "T" in raw:
        day_s, time_s = raw.split("T", 1)
    elif " " in raw:
        day_s, time_s = raw.split(None, 1)
    else:
        day_s, time_s = raw, "00:00:00"
    try:
        day = int(day_s.strip())
    except ValueError:
        import i18n
        raise ValueError(i18n.t("error.bad_reset", text=text)) from None
    if day < 1 or day > 31:
        import i18n
        raise ValueError(i18n.t("error.bad_reset", text=text))
    return day, parse_reset_time(time_s)


def format_reset(day: int, time_s: str = "00:00:00") -> str:
    try:
        time_s = parse_reset_time(time_s)
    except ValueError:
        time_s = "00:00:00"
    day = int(day or 1)
    if time_s == "00:00:00":
        return str(day)
    return f"{day}T{time_s}"


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


def parse_svc_name(text: str) -> str:
    name = normalize_node_name(text)
    if not valid_node_name(name):
        import i18n

        raise ValueError(i18n.t("error.bad_svc", text=text))
    return name


def parse_ports(text: str) -> list[int]:
    raw = (text or "").replace("，", ",").replace(";", ",")
    ports: list[int] = []
    for part in raw.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            port = int(part)
        except ValueError:
            import i18n

            raise ValueError(i18n.t("error.bad_ports", text=text)) from None
        if port < 1 or port > 65535:
            import i18n

            raise ValueError(i18n.t("error.bad_ports", text=text))
        if port not in ports:
            ports.append(port)
    if not ports or len(ports) > MAX_SVC_PORTS:
        import i18n

        raise ValueError(i18n.t("error.bad_ports", text=text))
    return ports


def normalize_svc_list(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            name = parse_svc_name(str(item.get("name") or ""))
            ports = [int(p) for p in (item.get("ports") or [])]
            ports = parse_ports(",".join(str(p) for p in ports))
        except (ValueError, TypeError):
            continue
        proc = normalize_node_name(str(item.get("proc") or name))
        if not valid_node_name(proc):
            proc = name
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "ports": ports, "proc": proc})
        if len(out) >= MAX_SVC:
            break
    return out


def cap_pct(used: int, cap: Optional[int]) -> Optional[float]:
    if cap is None or cap <= 0:
        return None
    return used / cap * 100.0


class HubError(RuntimeError):
    def __init__(self, code: int, detail: str) -> None:
        super().__init__(f"hub HTTP {code}: {detail}")
        self.code = int(code)
        self.detail = detail


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
        raise HubError(int(exc.code), detail) from exc
    if not body:
        return {}
    return json.loads(body)
