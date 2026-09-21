#!/usr/bin/env python3
"""Low-memory Lightsail traffic reporter. Stdlib only, oneshot-friendly."""
from __future__ import annotations

import html
import http.client
import json
import os
import resource
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import i18n

UTC = timezone.utc
THRESHOLDS = (50, 70, 85, 95, 100)
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
    period_start: date
    period_end: date
    period_key: str
    now: datetime
    today: DayBytes
    period_rx: int
    period_tx: int
    vnstat_ok: bool
    xray_ok: bool
    bootstrap_applied: bool
    days: list[DayBytes]
    rate_period: float
    rate_recent: float
    projected: int


def utcnow() -> datetime:
    return datetime.now(UTC)


def env(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"missing environment variable {name}")
    return value


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


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def billing_period(now: datetime, reset_day: int) -> tuple[date, date]:
    today = now.date()
    if today.day >= reset_day:
        start = date(today.year, today.month, reset_day)
    else:
        year, month = today.year, today.month - 1
        if month == 0:
            year, month = year - 1, 12
        start = date(year, month, reset_day)
    end_year, end_month = start.year, start.month + 1
    if end_month == 13:
        end_year, end_month = end_year + 1, 1
    end = date(end_year, end_month, reset_day) - timedelta(days=1)
    return start, end


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


def pre_traffic_table(snap: Snapshot, cap: int, include_today: bool = True) -> str:
    """Monospace table. CJK labels are the same width; numbers share one GB column."""
    used = snap.period_rx + snap.period_tx
    pct = (used / cap * 100.0) if cap else 0.0
    cap_gb = int(round(cap / 1_000_000_000))
    lines: list[str] = []
    if include_today:
        lines.extend(
            [
                i18n.t("report.today"),
                i18n.t("pre.row_in", label=i18n.t("report.in"), value=fmt_gb(snap.today.rx)),
                i18n.t("pre.row_out", label=i18n.t("report.out"), value=fmt_gb(snap.today.tx)),
                i18n.t("pre.row_total", label=i18n.t("report.total"), value=fmt_gb(snap.today.total)),
            ]
        )
    lines.extend(
        [
            i18n.t("report.period"),
            i18n.t("pre.row_in", label=i18n.t("report.in"), value=fmt_gb(snap.period_rx)),
            i18n.t("pre.row_out", label=i18n.t("report.out"), value=fmt_gb(snap.period_tx)),
            i18n.t("pre.row_total", label=i18n.t("report.total"), value=fmt_gb(used)),
            i18n.t("report.quota", used=fmt_gb_num(used), cap=cap_gb, pct=pct),
        ]
    )
    return "<pre>" + "\n".join(lines) + "</pre>"


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


_tg_lock = threading.Lock()
_tg_conn: Optional[http.client.HTTPSConnection] = None
_tg_ctx = ssl.create_default_context()


def _tg_close() -> None:
    global _tg_conn
    conn = _tg_conn
    _tg_conn = None
    if conn is None:
        return
    try:
        conn.close()
    except OSError:
        pass


def _tg_conn_get(timeout: int) -> http.client.HTTPSConnection:
    global _tg_conn
    conn = _tg_conn
    if conn is None:
        conn = http.client.HTTPSConnection(
            "api.telegram.org",
            timeout=timeout,
            context=_tg_ctx,
        )
        _tg_conn = conn
        return conn
    conn.timeout = timeout
    sock = conn.sock
    if sock is not None:
        sock.settimeout(timeout)
    return conn


def telegram_call(
    token: str,
    method: str,
    payload: Optional[dict[str, Any]] = None,
    timeout: int = 20,
) -> dict[str, Any]:
    path = f"/bot{token}/{method}"
    data = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Connection": "keep-alive"}
    http_method = "POST" if payload is not None else "GET"
    raw = ""
    status = 0
    last_exc: Optional[Exception] = None
    for _attempt in range(2):
        with _tg_lock:
            try:
                conn = _tg_conn_get(timeout)
                conn.request(http_method, path, body=data or None, headers=headers)
                resp = conn.getresponse()
                status = int(resp.status)
                raw = resp.read().decode("utf-8", errors="replace")
                last_exc = None
                break
            except (OSError, http.client.HTTPException, TimeoutError) as exc:
                last_exc = exc
                _tg_close()
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


def read_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def tcp_443_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 443), 2):
            return True
    except OSError:
        return False


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
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def bootstrap_extra(bootstrap: dict[str, Any], period_start: date, period_end: date) -> tuple[int, int, bool]:
    captured = parse_iso_datetime(bootstrap.get("captured_at"))
    boot_time = parse_iso_datetime(bootstrap.get("boot_time"))
    if captured is None or boot_time is None:
        return 0, 0, False
    if not (period_start <= captured.date() <= period_end):
        return 0, 0, False
    if boot_time.date() < period_start:
        return 0, 0, False
    return int(bootstrap.get("rx_bytes") or 0), int(bootstrap.get("tx_bytes") or 0), True


def sum_days(days: list[DayBytes], start: date, end: date) -> tuple[int, int]:
    rx = tx = 0
    for item in days:
        if start <= item.day <= end:
            rx += item.rx
            tx += item.tx
    return rx, tx


def today_bytes(days: list[DayBytes], today: date) -> DayBytes:
    for item in days:
        if item.day == today:
            return item
    return DayBytes(day=today, rx=0, tx=0)


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
    end_override: Optional[date] = None,
) -> Snapshot:
    now = now or utcnow()
    period_start, period_end = billing_period(now, reset_day)
    if end_override is not None:
        period_end = end_override
    try:
        import counters

        raw_days = counters.record_sample(iface, now)
        days = [DayBytes(day=day, rx=rx, tx=tx) for day, rx, tx in raw_days]
        traffic_ok = True
    except Exception:
        days = []
        traffic_ok = False
    rx, tx = sum_days(days, period_start, period_end)
    extra_rx, extra_tx, applied = bootstrap_extra(bootstrap, period_start, period_end)
    rx += extra_rx
    tx += extra_tx
    today = today_bytes(days, now.date())
    elapsed_days = max(1, (min(now.date(), period_end) - period_start).days + 1)
    rate_period = (rx + tx) / elapsed_days
    rate_recent = daily_rate(days, min(now.date(), period_end), rx, tx, period_start)
    days_left = max(0, (period_end - now.date()).days)
    projected = int(rx + tx + rate_recent * days_left)
    return Snapshot(
        iface=iface,
        period_start=period_start,
        period_end=period_end,
        period_key=period_start.isoformat(),
        now=now,
        today=today,
        period_rx=rx,
        period_tx=tx,
        vnstat_ok=traffic_ok,
        xray_ok=tcp_443_open(),
        bootstrap_applied=applied,
        days=days,
        rate_period=rate_period,
        rate_recent=rate_recent,
        projected=projected,
    )


def period_lines(snap: Snapshot, cap: int, include_today: bool = True) -> str:
    used = snap.period_rx + snap.period_tx
    pct = (used / cap * 100.0) if cap else 0.0
    remaining = cap - used
    elapsed_days = max(1, (min(snap.now.date(), snap.period_end) - snap.period_start).days + 1)
    days_left = max(0, (snap.period_end - snap.now.date()).days)
    xray = i18n.t("report.xray_ok") if snap.xray_ok else i18n.t("report.xray_down")
    if snap.bootstrap_applied:
        source = i18n.t("report.src_boot")
    elif snap.vnstat_ok:
        source = i18n.t("report.src_ok")
    else:
        source = i18n.t("report.src_fail")
    cap_note = i18n.t("report.over") if used >= cap else i18n.t("report.in_plan")
    return (
        i18n.t("report.cycle", start=h(snap.period_start.isoformat()), end=h(snap.period_end.isoformat()))
        + "\n"
        + i18n.t("report.host", host=h(host_label()), iface=h(snap.iface))
        + "\n"
        + i18n.t("report.source", source=h(source))
        + "\n\n"
        + f"{pre_traffic_table(snap, cap, include_today=include_today)}\n"
        + f"{progress_bar(pct)} {pct:.1f}%\n\n"
        + i18n.t("report.avg_period", rate=h(fmt_bytes(int(snap.rate_period))), elapsed=elapsed_days, left=days_left)
        + "\n"
        + i18n.t("report.avg_3d", rate=h(fmt_bytes(int(snap.rate_recent))))
        + "\n"
        + i18n.t("report.projected", value=h(fmt_bytes(int(snap.projected))))
        + "\n"
        + i18n.t("report.remain_cap", value=h(fmt_bytes(remaining)))
        + "\n"
        + cap_note
        + "\n"
        + i18n.t("report.xray", status=h(xray))
    )


def build_status_message(
    title: str, snap: Snapshot, cap: int, extra: str = "", include_today: bool = True
) -> str:
    body = (
        f"{title}\n\n"
        f"{period_lines(snap, cap, include_today=include_today)}"
    )
    if extra:
        body += "\n" + extra
    return body


def crossed_thresholds(prev: list[int], pct: float) -> list[int]:
    have = set(int(x) for x in prev)
    newly = []
    for mark in THRESHOLDS:
        if pct >= mark and mark not in have:
            newly.append(mark)
    return newly


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


def previous_period_snapshot(
    iface: str,
    reset_day: int,
    bootstrap: dict[str, Any],
    old_key: str,
) -> Optional[Snapshot]:
    try:
        start = date.fromisoformat(old_key)
    except ValueError:
        return None
    end_year, end_month = start.year, start.month + 1
    if end_month == 13:
        end_year, end_month = end_year + 1, 1
    end = date(end_year, end_month, reset_day) - timedelta(days=1)
    fake_now = datetime(end.year, end.month, end.day, 23, 59, tzinfo=UTC)
    return collect_snapshot(iface, reset_day, bootstrap, now=fake_now, end_override=end)


def main() -> int:
    iface = os.environ.get("TRAFFIC_IFACE", "eth0")
    reset_day = env_int("BILLING_RESET_DAY", 27)
    cap = env_int("MONTHLY_CAP_BYTES", 2_000_000_000_000)
    daily_hour = env_int("DAILY_REPORT_HOUR_UTC", 16)
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry_run = os.environ.get("TRAFFIC_MONITOR_DRY_RUN") == "1"
    force = os.environ.get("TRAFFIC_MONITOR_FORCE") == "1"

    state_path = state_dir() / "state.json"
    bootstrap_path = state_dir() / "bootstrap.json"
    state = load_json(state_path)
    bootstrap = load_json(bootstrap_path)
    snap = collect_snapshot(iface, reset_day, bootstrap)

    used = snap.period_rx + snap.period_tx
    pct = (used / cap * 100.0) if cap else 0.0
    days_left = max(0, (snap.period_end - snap.now.date()).days)
    projected = snap.projected
    print(
        f"traffic-monitor period={snap.period_key} used={used} pct={pct:.1f} "
        f"traffic={int(snap.vnstat_ok)} bootstrap={int(snap.bootstrap_applied)} "
        f"maxrss_kb={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}",
        flush=True,
    )

    if state.get("period_key") != snap.period_key:
        old_key = state.get("period_key")
        if old_key:
            prev = previous_period_snapshot(iface, reset_day, bootstrap, str(old_key))
            if prev is not None:
                msg = build_status_message(
                    i18n.t("report.close", host=h(host_label())),
                    prev,
                    cap,
                    include_today=False,
                )
                if dry_run:
                    print(msg)
                else:
                    send_telegram(token, chat_id, msg)
        state["period_key"] = snap.period_key
        state["fired_thresholds"] = []
        state["last_projection_sent_at"] = None
        state["last_daily_sent"] = None

    messages: list[str] = []
    extra_bits: list[str] = []
    vnstat_note = ""
    if not snap.vnstat_ok:
        vnstat_note = i18n.t("report.ledger_fail")
    boot_id = read_boot_id()
    if state.get("boot_id") and state["boot_id"] != boot_id:
        extra_bits.append(i18n.t("report.reboot"))
    state["boot_id"] = boot_id

    prev_marks = [int(x) for x in state.get("fired_thresholds") or []]
    newly = crossed_thresholds(prev_marks, pct)
    if newly:
        marks = i18n.t("sep.list").join(f"{mark}%" for mark in newly)
        extra_bits.append(i18n.t("report.crossed", marks=h(marks)))
        state["fired_thresholds"] = sorted(set(prev_marks + newly))

    last_proj = parse_iso_datetime(state.get("last_projection_sent_at"))
    if used < cap and projected > cap:
        if last_proj is None or snap.now - last_proj >= PROJECTION_COOLDOWN:
            extra_bits.append(
                i18n.t(
                    "report.projection",
                    projected=h(fmt_bytes(int(projected))),
                    left=days_left,
                    budget=h(fmt_bytes(int(max(0, cap - used) / max(1, days_left)))),
                )
            )
            state["last_projection_sent_at"] = snap.now.isoformat()

    extra = "\n".join([bit for bit in (vnstat_note, *extra_bits) if bit])
    startup_sent = bool(state.get("startup_sent"))
    last_daily = str(state.get("last_daily_sent") or "")
    today_s = snap.now.date().isoformat()
    daily_due = snap.now.hour >= daily_hour and last_daily != today_s

    if not startup_sent or force:
        messages.append(
            build_status_message(i18n.t("report.startup", host=h(host_label())), snap, cap, extra)
        )
        state["startup_sent"] = True
        if snap.now.hour >= daily_hour:
            state["last_daily_sent"] = today_s
    elif daily_due:
        messages.append(build_status_message(i18n.t("report.daily", host=h(host_label())), snap, cap, extra))
        state["last_daily_sent"] = today_s
    elif extra_bits:
        messages.append(build_status_message(i18n.t("report.alert", host=h(host_label())), snap, cap, extra))

    if dry_run:
        for msg in messages:
            print(msg)
            print("---")
        print(json.dumps({"used": used, "pct": round(pct, 2), "period": snap.period_key}, ensure_ascii=False))
        return 0

    for msg in messages:
        send_telegram(token, chat_id, msg)

    save_json(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
