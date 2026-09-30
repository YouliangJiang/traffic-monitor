#!/usr/bin/env python3
"""Telegram sendMessage and small value formatters. Stdlib only."""
from __future__ import annotations

import html
import http.client
import json
import ssl
from typing import Any

_tg_ctx = ssl.create_default_context()
# Handshake only. A dead address, v4 or v6, must fail fast so the next one is tried.
_CONNECT_TIMEOUT = 5.0


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 15.0) -> None:
    """One HTTPS call per attempt; each attempt dials and closes its own socket."""
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    headers = {"Content-Type": "application/json", "Connection": "close"}
    error: Exception = RuntimeError("telegram send failed")
    for _attempt in range(3):
        conn = http.client.HTTPSConnection("api.telegram.org", timeout=_CONNECT_TIMEOUT, context=_tg_ctx)
        try:
            conn.connect()
            conn.sock.settimeout(timeout)
            conn.request("POST", f"/bot{token}/sendMessage", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status == 200 and json.loads(raw).get("ok"):
                return
            error = RuntimeError(f"telegram HTTP {resp.status}: {raw[:200]}")
            if 400 <= resp.status < 500 and resp.status != 429:
                break
        except (OSError, http.client.HTTPException, ValueError) as exc:
            error = exc
        finally:
            conn.close()
    raise RuntimeError(f"telegram send failed: {error}")


def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def fmt_bytes(n: float) -> str:
    """Decimal units, matching how most providers state plan caps."""
    n = float(n)
    for unit, size in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if abs(n) >= size:
            return f"{n / size:.2f} {unit}"
    return f"{n:.0f} B"


def fmt_rate(bytes_per_sec: float) -> str:
    bits = float(bytes_per_sec) * 8
    if bits >= 10**6:
        return f"{bits / 10**6:.1f} Mbps"
    if bits >= 10**3:
        return f"{bits / 10**3:.0f} Kbps"
    return f"{bits:.0f} bps"


def pct(used: float, total: float) -> float:
    return 100.0 * used / total if total > 0 else 0.0
