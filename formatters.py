#!/usr/bin/env python3
"""Telegram HTML text for the daily summary and anomaly alerts."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import i18n
import report
import util
from report import fmt_bytes, h


def duration(seconds: float) -> str:
    s = int(max(0, seconds))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    if days:
        return i18n.t("dur.d", days=days, hours=hours)
    if hours:
        return i18n.t("dur.h", hours=hours, minutes=s // 60)
    return i18n.t("dur.m", minutes=s // 60)


def mem_pct(snap: dict[str, Any]) -> float:
    return report.pct(snap["mem_total"] - snap["mem_available"], snap["mem_total"])


def disk_pct(snap: dict[str, Any]) -> float:
    # Same basis as df: used / (used + available to unprivileged users).
    return report.pct(snap["disk_used"], snap["disk_used"] + snap["disk_avail"])


def period_used(snap: dict[str, Any]) -> int:
    return snap["period_rx"] + snap["period_tx"]


def _bound(text: str) -> str:
    try:
        return datetime.fromisoformat(text).astimezone(util.local_tz()).strftime("%m-%d %H:%M")
    except ValueError:
        return text


def node_block(row: dict[str, Any]) -> str:
    name, snap, age = h(row["name"]), row.get("snapshot"), row.get("age")
    if not snap:
        return i18n.t("node.never", name=name)
    head = i18n.t("node.online", name=name) if row["online"] else i18n.t("node.offline", name=name, ago=duration(age or 0))
    lines = [
        head,
        i18n.t("node.resources", cpu=f"{snap['cpu_pct']:.0f}", mem=f"{mem_pct(snap):.0f}",
               disk=f"{disk_pct(snap):.0f}", uptime=duration(snap["uptime_sec"])),
    ]
    if not snap["traffic_ok"]:
        lines.append(i18n.t("node.traffic_error", error=h(snap["traffic_error"])))
        return "\n".join(lines)
    lines.append(i18n.t("node.traffic", yesterday=fmt_bytes(snap["yesterday_rx"] + snap["yesterday_tx"]),
                        today=fmt_bytes(snap["today_rx"] + snap["today_tx"])))
    used, cap = period_used(snap), snap.get("cap_bytes")
    if cap:
        lines.append(i18n.t("node.period_capped", used=fmt_bytes(used), cap=fmt_bytes(cap), pct=f"{report.pct(used, cap):.1f}"))
    else:
        lines.append(i18n.t("node.period_unlimited", used=fmt_bytes(used)))
    lines.append(i18n.t("node.period_range", start=_bound(snap["period_start"]), end=_bound(snap["period_end"])))
    return "\n".join(lines)


def daily(rows: list[dict[str, Any]], today: str) -> str:
    title = i18n.t("daily.title", date=today)
    if not rows:
        return f"{title}\n\n{i18n.t('daily.empty')}"
    online = sum(1 for row in rows if row["online"])
    blocks = "\n\n".join(node_block(row) for row in rows)
    return f"{title}\n{i18n.t('daily.summary', online=online, total=len(rows))}\n\n{blocks}"


def alert(key: str, name: str, **extra: Any) -> str:
    return i18n.t(key, name=h(name), **extra)
