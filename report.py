#!/usr/bin/env python3
"""Low-memory Lightsail traffic reporter. Stdlib only, oneshot-friendly."""
from __future__ import annotations

import calendar
import html
import http.client
import json
import os
import resource
import socket
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import i18n
import util

UTC = timezone.utc
THRESHOLDS = (80,)
PROJECTION_COOLDOWN = timedelta(hours=24)


@dataclass
class DayBytes:
    day: date
    rx: int
    tx: int

    @property
    def total(self) -> int:
        return self.rx + self.tx


@dataclass
class Snapshot:
    iface: str
    period_start: datetime
    period_end: datetime
    period_key: str
    now: datetime
    today: DayBytes
    period_rx: int
    period_tx: int
    ledger_ok: bool
    bootstrap_applied: bool
    days: list[DayBytes]
    rate_period: float
    rate_recent: float
    projected: int


def utcnow() -> datetime:
    return datetime.now(UTC)




def reset_datetime(year: int, month: int, reset_day: int, reset_time: str = "00:00:00") -> datetime:
    last = calendar.monthrange(year, month)[1]
    day = min(max(1, int(reset_day or 1)), last)
    hour, minute, second = (int(x) for x in util.parse_reset_time(reset_time).split(":"))
    return datetime(year, month, day, hour, minute, second, tzinfo=util.local_tz())


def billing_period(
    now: datetime, reset_day: int, reset_time: str = "00:00:00"
) -> tuple[datetime, datetime]:
    tz = util.local_tz()
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    this = reset_datetime(now.year, now.month, reset_day, reset_time)
    if now >= this:
        start = this
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        end = reset_datetime(year, month, reset_day, reset_time)
    else:
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        start = reset_datetime(year, month, reset_day, reset_time)
        end = this
    return start, end


def fmt_period_bound(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=util.local_tz())
    else:
        parsed = parse_iso_datetime(value)
        if parsed is None:
            text = str(value or "")
            if len(text) >= 10 and text[4] == "-":
                return text[:10]
            return text
    if parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        return parsed.date().isoformat()
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def fmt_bytes(n: int) -> str:
    n = int(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 10**12:
        value = f"{n / 10**12:.2f} TB"
    elif n >= 10**9:
        value = f"{n / 10**9:.2f} GB"
    elif n >= 10**6:
        value = f"{n / 10**6:.2f} MB"
    elif n >= 10**3:
        value = f"{n / 10**3:.2f} KB"
    else:
        value = f"{n} B"
    return sign + value


def fmt_gb_num(n: int) -> str:
    """Right-aligned numeric GB, always 7 characters: '   0.00', ' 426.13'."""
    return f"{n / 1_000_000_000:7.2f}"


def fmt_gb(n: int) -> str:
    """Right-aligned GB for <pre> columns, e.g. '   0.00 GB' or ' 426.13 GB'."""
    return f"{fmt_gb_num(n)} GB"



def progress_bar(pct: float, width: int = 16) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    filled = min(width, max(0, filled))
    return "█" * filled + "░" * (width - filled)


def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def host_label() -> str:
    for key in ("HOST_LABEL", "NODE_NAME"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    try:
        name = socket.gethostname().strip()
    except OSError:
        name = ""
    return name or "host"


_tg_ctx = ssl.create_default_context()
# Handshake only. A dead address, v4 or v6, must fail fast so the next one is tried.
_CONNECT_TIMEOUT = 5.0


def _open_telegram(read_timeout: float) -> http.client.HTTPSConnection:
    """Open a socket for this call. The caller closes it before returning.

    The bot session is the update offset, not a TCP connection. Each API call
    dials, exchanges one request, and closes. getaddrinfo order is left to the
    system: create_connection tries every address and abandons one that does
    not complete the handshake within _CONNECT_TIMEOUT. The read timeout is
    applied only after the handshake, so a long poll can wait out Telegram's
    hold without letting a blackholed family block for that whole time.
    """
    if read_timeout <= 0:
        raise ValueError("telegram read timeout must be positive")
    conn = http.client.HTTPSConnection(
        "api.telegram.org",
        timeout=_CONNECT_TIMEOUT,
        context=_tg_ctx,
    )
    try:
        conn.connect()
        sock = conn.sock
        if sock is None:
            raise OSError("telegram connect produced no socket")
        sock.settimeout(read_timeout)
        return conn
    except BaseException:
        conn.close()
        raise


def _tg_exchange(conn: http.client.HTTPSConnection, http_method: str, path: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
    conn.request(http_method, path, body=data or None, headers=headers)
    resp = conn.getresponse()
    try:
        return int(resp.status), resp.read().decode("utf-8", errors="replace")
    finally:
        resp.close()


def telegram_call(
    token: str,
    method: str,
    payload: Optional[dict[str, Any]] = None,
    timeout: int = 20,
) -> dict[str, Any]:
    path = f"/bot{token}/{method}"
    data = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Connection": "close"}
    http_method = "POST" if payload is not None else "GET"
    raw = ""
    status = 0
    last_exc: Optional[Exception] = None
    for _attempt in range(2):
        try:
            conn = _open_telegram(timeout)
        except (OSError, TimeoutError) as exc:
            last_exc = exc
            continue
        try:
            status, raw = _tg_exchange(conn, http_method, path, data, headers)
            last_exc = None
            break
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            last_exc = exc
        finally:
            try:
                conn.close()
            except OSError:
                pass
    if last_exc is not None:
        raise RuntimeError(f"telegram {method}: {last_exc}") from last_exc
    if status >= 400:
        try:
            err = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            err = {}
        desc = err.get("description") or raw or f"HTTP {status}"
        raise RuntimeError(f"telegram {method} HTTP {status}: {desc}")
    body = json.loads(raw) if raw else {}
    if not body.get("ok"):
        raise RuntimeError(body)
    return body



def parse_iso_date(raw: Any) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def parse_iso_datetime(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=util.local_tz())
    return parsed


def bootstrap_extra(
    bootstrap: dict[str, Any], period_start: datetime, period_end: datetime
) -> tuple[int, int, bool]:
    captured = parse_iso_datetime(bootstrap.get("captured_at"))
    boot_time = parse_iso_datetime(bootstrap.get("boot_time"))
    if captured is None or boot_time is None:
        return 0, 0, False
    if not isinstance(period_start, datetime) or not isinstance(period_end, datetime):
        return 0, 0, False
    # The bootstrap counter is one lump from boot until install. Count it only when
    # that whole span sits inside this period. A boot that started before the reset
    # instant cannot be split, so it is left out instead of charging the previous period.
    if not (period_start <= captured < period_end):
        return 0, 0, False
    if boot_time < period_start:
        return 0, 0, False
    return int(bootstrap.get("rx_bytes") or 0), int(bootstrap.get("tx_bytes") or 0), True




def daily_rate(days: list[DayBytes], today: date, period_rx: int, period_tx: int, period_start: date) -> float:
    recent = [item for item in days if item.day <= today]
    recent.sort(key=lambda item: item.day)
    window = recent[-3:]
    if len(window) >= 2:
        total = sum(item.total for item in window)
        return total / len(window)
    elapsed = max(1, (today - period_start).days + 1)
    return (period_rx + period_tx) / elapsed


def collect_snapshot(
    iface: str,
    reset_day: int,
    bootstrap: dict[str, Any],
    now: Optional[datetime] = None,
    reset_time: str = "00:00:00",
) -> Snapshot:
    now = now or utcnow()
    period_start, period_end = billing_period(now, reset_day, reset_time)
    try:
        import counters

        counters.record_sample(iface, now)
        ledger_ok = True
        rx, tx = counters.sum_between(period_start, period_end)
        local_now = now if now.tzinfo else now.replace(tzinfo=util.local_tz())
        local_now = local_now.astimezone(util.local_tz())
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_rx, today_tx = counters.sum_between(today_start, today_start + timedelta(days=1))
        days = [DayBytes(day=day, rx=drx, tx=dtx) for day, drx, dtx in counters.day_rows()]
    except Exception:
        days = []
        ledger_ok = False
        rx = tx = 0
        today_rx = today_tx = 0
    start_d = period_start.date()
    end_d = (period_end - timedelta(microseconds=1)).date()
    extra_rx, extra_tx, applied = bootstrap_extra(bootstrap, period_start, period_end)
    rx += extra_rx
    tx += extra_tx
    today = DayBytes(day=now.date(), rx=today_rx, tx=today_tx)
    elapsed_days = max(1, int((min(now, period_end) - period_start).total_seconds() // 86400) + 1)
    rate_period = (rx + tx) / elapsed_days
    rate_recent = daily_rate(days, min(now.date(), end_d), rx, tx, start_d)
    days_left = max(0, int((period_end - now).total_seconds() // 86400))
    projected = int(rx + tx + rate_recent * days_left)
    return Snapshot(
        iface=iface,
        period_start=period_start,
        period_end=period_end,
        period_key=period_start.strftime("%Y-%m-%dT%H:%M:%S"),
        now=now,
        today=today,
        period_rx=rx,
        period_tx=tx,
        ledger_ok=ledger_ok,
        bootstrap_applied=applied,
        days=days,
        rate_period=rate_period,
        rate_recent=rate_recent,
        projected=projected,
    )







def send_telegram(token: str, chat_id: str, text: str) -> None:
    last_error: Optional[Exception] = None
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for _attempt in range(3):
        try:
            telegram_call(token, "sendMessage", payload, timeout=12)
            return
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
    raise SystemExit(f"telegram send failed: {last_error}")




