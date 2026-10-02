#!/usr/bin/env python3
"""Telegram sendMessage/getUpdates and small value formatters. Stdlib only."""
from __future__ import annotations

import html
import http.client
import json
import ssl
from typing import Any

_tg_ctx = ssl.create_default_context()
# Handshake only. A dead address, v4 or v6, must fail fast so the next one is tried.
_CONNECT_TIMEOUT = 5.0


def _call(token: str, method: str, payload: dict[str, Any], timeout: float) -> tuple[int, str]:
    """One Bot API call on its own socket: (HTTP status, response text)."""
    conn = http.client.HTTPSConnection("api.telegram.org", timeout=_CONNECT_TIMEOUT, context=_tg_ctx)
    try:
        conn.connect()
        conn.sock.settimeout(timeout)
        conn.request("POST", f"/bot{token}/{method}", body=json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json", "Connection": "close"})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", errors="replace")
    finally:
        conn.close()


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 15.0) -> None:
    """One HTTPS call per attempt; each attempt dials and closes its own socket."""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    error: Exception = RuntimeError("telegram send failed")
    for _attempt in range(3):
        try:
            status, raw = _call(token, "sendMessage", payload, timeout)
            if status == 200 and json.loads(raw).get("ok"):
                return
            error = RuntimeError(f"telegram HTTP {status}: {raw[:200]}")
            if 400 <= status < 500 and status != 429:
                break
        except (OSError, http.client.HTTPException, ValueError) as exc:
            error = exc
    raise RuntimeError(f"telegram send failed: {error}")


def get_updates(token: str, offset: int, wait: int = 50) -> list[dict[str, Any]]:
    """Long-poll for messages sent to the bot. One attempt; the caller paces retries."""
    payload = {"offset": offset, "timeout": wait, "allowed_updates": ["message"]}
    try:
        status, raw = _call(token, "getUpdates", payload, wait + 10)
        data = json.loads(raw) if status == 200 else {}
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise RuntimeError(f"telegram getUpdates failed: {exc}") from None
    result = data.get("result") if isinstance(data, dict) and data.get("ok") else None
    if not isinstance(result, list):
        raise RuntimeError(f"telegram getUpdates HTTP {status}: {raw[:200]}")
    return [update for update in result if isinstance(update, dict)]


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
