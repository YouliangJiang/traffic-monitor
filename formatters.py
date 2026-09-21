#!/usr/bin/env python3
"""Telegram HTML formatters for fleet and node views."""
from __future__ import annotations

from typing import Any, Optional

import hostinfo
import i18n
import report
import util


def _short_bytes(n: int) -> str:
    n = int(n)
    if n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:5.2f}T"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:5.1f}G"
    if n >= 1_000_000:
        return f"{n / 1_000_000:5.0f}M"
    return f"{n:5d}B"


def _mem_pct(snap: dict[str, Any]) -> float:
    total = float(snap.get("mem_total") or 0)
    avail = float(snap.get("mem_available") or 0)
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (total - avail) / total))


def _status(row: dict[str, Any]) -> str:
    if not row.get("enabled", True):
        return "off"
    if row.get("online"):
        return "on"
    return "stale"


def fleet_overview(rows: list[dict[str, Any]], title: Optional[str] = None) -> str:
    title = title or i18n.t("fleet.title")
    if not rows:
        return i18n.t("fleet.empty", title=title)
    lines = [
        f"{'node':<14} {'used':>7} {'cap':>6} {'pct':>5} {'cpu':>4} {'mem':>4} st",
        "-" * 50,
    ]
    used_all = 0
    capped = 0
    online = 0
    for row in rows:
        snap = row.get("snapshot") or {}
        used = int(row.get("used") or 0)
        used_all += used
        cap = row.get("cap_bytes")
        if cap:
            capped += cap
        pct = row.get("pct")
        pct_s = f"{pct:4.0f}%" if pct is not None else "   --"
        cpu = float(snap.get("cpu_pct") or 0)
        mem = _mem_pct(snap)
        cap_s = "    ∞" if not cap else _short_bytes(cap)
        lines.append(
            f"{row['name'][:14]:<14} {_short_bytes(used):>7} {cap_s:>6} {pct_s:>5} "
            f"{cpu:3.0f}% {mem:3.0f}% {_status(row):>4}"
        )
        if row.get("online") and row.get("enabled", True):
            online += 1
    cap_line = i18n.t("fleet.unlimited") if not capped else report.fmt_bytes(capped)
    return (
        f"{title}\n"
        f"{i18n.t('fleet.summary', online=online, total=len(rows), cap=cap_line, used=report.fmt_bytes(used_all))}\n\n"
        f"<pre>" + "\n".join(lines) + "</pre>\n\n"
        f"{i18n.t('fleet.hint')}"
    )


def node_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return i18n.t("fleet.coverage_empty")
    lines = []
    for row in rows:
        cap = util.format_cap(row.get("cap_bytes"))
        mark = "●" if row.get("online") else "○"
        flag = "" if row.get("enabled", True) else i18n.t("fleet.disabled_flag")
        lines.append(
            i18n.t(
                "fleet.coverage_line",
                mark=mark,
                name=report.h(row["name"]),
                cap=report.h(cap),
                reset=int(row.get("reset_day") or 1),
                flag=flag,
            )
        )
    return f"{i18n.t('fleet.coverage_title')}\n\n" + "\n".join(lines) + "\n\n" + i18n.t("fleet.coverage_hint")


def node_detail(row: dict[str, Any]) -> str:
    snap = row.get("snapshot") or {}
    if not snap:
        return i18n.t("node.no_heartbeat", name=report.h(row["name"]))
    cap = row.get("cap_bytes")
    used = int(row.get("used") or 0)
    pct = row.get("pct")
    if cap:
        table = "\n".join(
            [
                i18n.t("node.traffic"),
                i18n.t("pre.row_in", label=i18n.t("node.in"), value=report.fmt_gb(int(snap.get("period_rx") or 0))),
                i18n.t("pre.row_out", label=i18n.t("node.out"), value=report.fmt_gb(int(snap.get("period_tx") or 0))),
                i18n.t("pre.row_total", label=i18n.t("node.total"), value=report.fmt_gb(used)),
                i18n.t(
                    "report.quota",
                    used=report.fmt_gb_num(used),
                    cap=int(round(cap / 1_000_000_000)),
                    pct=pct or 0,
                ),
            ]
        )
        bar = f"{report.progress_bar(pct or 0)} {(pct or 0):.1f}%\n"
        cap_note = i18n.t("node.in_plan") if (pct or 0) < 100 else i18n.t("node.over_plan")
    else:
        table = "\n".join(
            [
                i18n.t("node.traffic_unlimited"),
                i18n.t("pre.row_in", label=i18n.t("node.in"), value=report.fmt_gb(int(snap.get("period_rx") or 0))),
                i18n.t("pre.row_out", label=i18n.t("node.out"), value=report.fmt_gb(int(snap.get("period_tx") or 0))),
                i18n.t("pre.row_total", label=i18n.t("node.total"), value=report.fmt_gb(used)),
            ]
        )
        bar = ""
        cap_note = i18n.t("node.unlimited_note")
    mem_used = int(snap.get("mem_total") or 0) - int(snap.get("mem_available") or 0)
    mem_total = int(snap.get("mem_total") or 0)
    xray = "healthy" if snap.get("xray_ok") else "down"
    net_extra = ""
    if snap.get("net_window_sec"):
        net_extra = "  " + str(int(round(float(snap.get("net_window_sec") or 0)))) + "s"
    return (
        i18n.t("node.header", name=report.h(row["name"]), status=_status(row)) + "\n"
        + i18n.t(
            "node.meta",
            period=i18n.t("node.period"),
            start=report.h(snap.get("period_start")),
            end=report.h(snap.get("period_end")),
            reset_label=i18n.t("node.reset"),
            reset=int(row.get("reset_day") or 1),
            nic=i18n.t("node.nic"),
            iface=report.h(row.get("iface") or snap.get("iface")),
        )
        + "\n\n"
        + f"<pre>{table}</pre>\n"
        + bar
        + f"{cap_note}\n\n"
        + f"CPU {float(snap.get('cpu_pct') or 0):.0f}%  "
        + f"MEM {hostinfo.fmt_mib(mem_used)}/{hostinfo.fmt_mib(mem_total)}  "
        + f"DISK {report.fmt_bytes(int(snap.get('disk_used') or 0))}\n"
        + f"NET ↓{hostinfo.fmt_bps(float(snap.get('net_rx_bps') or 0))}  "
        + f"↑{hostinfo.fmt_bps(float(snap.get('net_tx_bps') or 0))}"
        + f"{net_extra}\n"
        + f"XRAY {xray}  up {hostinfo.fmt_duration(float(snap.get('uptime_sec') or 0))}"
    )


def metric_block(row: dict[str, Any], kind: str) -> str:
    snap = row.get("snapshot") or {}
    name = row["name"]
    if not snap:
        return i18n.t("node.no_data", name=report.h(name))
    if kind == "cpu":
        return i18n.t(
            "metric.cpu",
            name=report.h(name),
            pct=float(snap.get("cpu_pct") or 0),
            bar=report.progress_bar(float(snap.get("cpu_pct") or 0)),
            load=float(snap.get("load1") or 0),
            nproc=int(snap.get("nproc") or 1),
            steal=float(snap.get("steal_pct") or 0),
        )
    if kind == "mem":
        total = int(snap.get("mem_total") or 0)
        used = total - int(snap.get("mem_available") or 0)
        pct = _mem_pct(snap)
        swap_used = int(snap.get("swap_total") or 0) - int(snap.get("swap_free") or 0)
        return i18n.t(
            "metric.mem",
            name=report.h(name),
            used=hostinfo.fmt_mib(used),
            total=hostinfo.fmt_mib(total),
            pct=pct,
            bar=report.progress_bar(pct),
            swap_used=hostinfo.fmt_mib(swap_used),
            swap_total=hostinfo.fmt_mib(int(snap.get("swap_total") or 0)),
        )
    if kind == "disk":
        total = int(snap.get("disk_total") or 0) or 1
        used = int(snap.get("disk_used") or 0)
        pct = 100.0 * used / total
        return i18n.t(
            "metric.disk",
            name=report.h(name),
            used=report.fmt_bytes(used),
            total=report.fmt_bytes(total),
            pct=pct,
            bar=report.progress_bar(pct),
            avail=report.fmt_bytes(int(snap.get("disk_avail") or 0)),
        )
    if kind == "net":
        window = float(snap.get("net_window_sec") or 0)
        window_s = i18n.t("metric.net_avg", sec=window) if window >= 1 else i18n.t("metric.net_avg_none")
        return i18n.t(
            "metric.net",
            name=report.h(name),
            rx=hostinfo.fmt_bps(float(snap.get("net_rx_bps") or 0)),
            tx=hostinfo.fmt_bps(float(snap.get("net_tx_bps") or 0)),
            window=f"{window_s}. {i18n.t('metric.net_hint')}",
        )
    if kind == "today":
        return i18n.t(
            "metric.today",
            name=report.h(name),
            rx=report.fmt_gb(int(snap.get("today_rx") or 0)),
            tx=report.fmt_gb(int(snap.get("today_tx") or 0)),
            total=report.fmt_gb(int(snap.get("today_total") or 0)),
        )
    if kind == "xray":
        ok = bool(snap.get("xray_ok"))
        return i18n.t(
            "metric.xray",
            name=report.h(name),
            status=i18n.t("metric.xray_ok") if ok else i18n.t("metric.xray_down"),
            rss=hostinfo.fmt_mib(int(snap.get("xray_rss") or 0)),
        )
    if kind == "uptime":
        return i18n.t(
            "metric.uptime",
            name=report.h(name),
            uptime=hostinfo.fmt_duration(float(snap.get("uptime_sec") or 0)),
            hostname=report.h(snap.get("hostname") or ""),
        )
    return node_detail(row)


def nic_result(name: str, data: dict[str, Any], row: Optional[dict[str, Any]] = None) -> str:
    seconds = float(data.get("seconds") or 0)
    iface = str(data.get("iface") or (row or {}).get("iface") or "eth0")
    snap = (row or {}).get("snapshot") or {}
    window = float(snap.get("net_window_sec") or 0)
    avg = ""
    if window >= 1:
        avg = i18n.t(
            "nic.avg",
            sec=window,
            rx=hostinfo.fmt_bps(float(snap.get("net_rx_bps") or 0)),
            tx=hostinfo.fmt_bps(float(snap.get("net_tx_bps") or 0)),
        )
    return (
        i18n.t("nic.title", name=report.h(name), seconds=seconds, iface=report.h(iface))
        + "\n\n<pre>"
        + i18n.t(
            "nic.row_in",
            bps=hostinfo.fmt_bps(float(data.get("rx_bps") or 0)),
            nbytes=report.fmt_bytes(int(data.get("rx_bytes") or 0)),
        )
        + "\n"
        + i18n.t(
            "nic.row_out",
            bps=hostinfo.fmt_bps(float(data.get("tx_bps") or 0)),
            nbytes=report.fmt_bytes(int(data.get("tx_bytes") or 0)),
        )
        + f"</pre>{avg}\n"
        + i18n.t("nic.hint")
    )


def bw_result(name: str, data: dict[str, Any]) -> str:
    rx_bps = float(data.get("rx_bps") or 0)
    tx_bps = float(data.get("tx_bps") or 0)
    down_sec = float(data.get("down_sec") or data.get("seconds") or 0)
    up_sec = float(data.get("up_sec") or 0)
    extra = ""
    if data.get("up_error") or (tx_bps <= 0 and up_sec <= 0):
        reason = data.get("up_error") or i18n.t("speed.up_timeout")
        extra = i18n.t("speed.up_fail", reason=report.h(reason))
    return (
        i18n.t("speed.title", name=report.h(name))
        + "\n\n<pre>"
        + i18n.t(
            "speed.row_down",
            bps=hostinfo.fmt_bps(rx_bps),
            nbytes=report.fmt_bytes(int(data.get("rx_bytes") or 0)),
            sec=down_sec,
        )
        + "\n"
        + i18n.t(
            "speed.row_up",
            bps=hostinfo.fmt_bps(tx_bps),
            nbytes=report.fmt_bytes(int(data.get("tx_bytes") or 0)),
            sec=up_sec,
        )
        + "\n</pre>\n"
        + i18n.t("speed.hint")
        + extra
    )


def add_help(name: str, hub_url: str, cap_text: str, reset_day: int) -> str:
    return i18n.t(
        "add.done",
        name=report.h(name),
        cap=report.h(cap_text),
        reset=reset_day,
        hub=report.h(hub_url),
    )
