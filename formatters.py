#!/usr/bin/env python3
"""Telegram HTML formatters for fleet and node views."""
from __future__ import annotations

from typing import Any

import hostinfo
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


def fleet_overview(rows: list[dict[str, Any]], title: str = "🖥 机群总览") -> str:
    if not rows:
        return f"{title}\n\n还没有节点。用 /add 加入。"
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
    cap_line = "无限" if not capped else report.fmt_bytes(capped)
    return (
        f"{title}\n"
        f"在线 {online}/{len(rows)} · 有限额度合计 {cap_line} · 当前合计 {report.fmt_bytes(used_all)}\n\n"
        f"<pre>" + "\n".join(lines) + "</pre>\n\n"
        f"查一台：<code>/go 名字</code>  测带宽：<code>/bw 名字</code>"
    )


def node_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "覆盖范围内没有节点。"
    lines = []
    for row in rows:
        cap = util.format_cap(row.get("cap_bytes"))
        mark = "●" if row.get("online") else "○"
        flag = "" if row.get("enabled", True) else " [停用]"
        lines.append(
            f"{mark} <code>{report.h(row['name'])}</code>  {report.h(cap)}  "
            f"重置日 {int(row.get('reset_day') or 1)}{flag}"
        )
    return "📋 <b>覆盖范围</b>\n\n" + "\n".join(lines)


def node_detail(row: dict[str, Any]) -> str:
    snap = row.get("snapshot") or {}
    if not snap:
        return (
            f"🖥 <b>{report.h(row['name'])}</b>\n\n"
            f"还没有心跳。确认 Agent 已安装，且能访问 Hub。"
        )
    cap = row.get("cap_bytes")
    used = int(row.get("used") or 0)
    pct = row.get("pct")
    if cap:
        table = (
            f"流量\n"
            f"  ↓ 入站  {report.fmt_gb(int(snap.get('period_rx') or 0))}\n"
            f"  ↑ 出站  {report.fmt_gb(int(snap.get('period_tx') or 0))}\n"
            f"  ∑ 合计  {report.fmt_gb(used)}\n"
            f"套餐额度  {report.fmt_gb_num(used)} / {int(round(cap / 1_000_000_000))} GB  "
            f"({pct:.1f}%)"
        )
        bar = f"{report.progress_bar(pct or 0)} {(pct or 0):.1f}%\n"
        cap_note = "✅ 当前仍在套餐内。" if (pct or 0) < 100 else "⚠️ 已超过套餐额度。"
    else:
        table = (
            f"流量（无限额度）\n"
            f"  ↓ 入站  {report.fmt_gb(int(snap.get('period_rx') or 0))}\n"
            f"  ↑ 出站  {report.fmt_gb(int(snap.get('period_tx') or 0))}\n"
            f"  ∑ 合计  {report.fmt_gb(used)}"
        )
        bar = ""
        cap_note = "✅ 这台按无限流量记账。"
    mem_used = int(snap.get("mem_total") or 0) - int(snap.get("mem_available") or 0)
    mem_total = int(snap.get("mem_total") or 0)
    xray = "healthy" if snap.get("xray_ok") else "down"
    return (
        f"🖥 <b>{report.h(row['name'])}</b>  {_status(row)}\n"
        f"周期 <code>{report.h(snap.get('period_start'))}</code> → "
        f"<code>{report.h(snap.get('period_end'))}</code>  重置日 {int(row.get('reset_day') or 1)}\n"
        f"网卡 <code>{report.h(row.get('iface') or snap.get('iface'))}</code>\n\n"
        f"<pre>{table}</pre>\n"
        f"{bar}"
        f"{cap_note}\n\n"
        f"CPU {float(snap.get('cpu_pct') or 0):.0f}%  "
        f"MEM {hostinfo.fmt_mib(mem_used)}/{hostinfo.fmt_mib(mem_total)}  "
        f"DISK {report.fmt_bytes(int(snap.get('disk_used') or 0))}\n"
        f"NET ↓{hostinfo.fmt_bps(float(snap.get('net_rx_bps') or 0))}  "
        f"↑{hostinfo.fmt_bps(float(snap.get('net_tx_bps') or 0))}\n"
        f"XRAY {xray}  up {hostinfo.fmt_duration(float(snap.get('uptime_sec') or 0))}"
    )


def metric_block(row: dict[str, Any], kind: str) -> str:
    snap = row.get("snapshot") or {}
    name = row["name"]
    if not snap:
        return f"{report.h(name)} 还没有数据。"
    if kind == "cpu":
        return (
            f"🧮 <b>{report.h(name)} · CPU</b>\n\n"
            f"{float(snap.get('cpu_pct') or 0):.1f}%  "
            f"{report.progress_bar(float(snap.get('cpu_pct') or 0))}\n"
            f"load {float(snap.get('load1') or 0):.2f} · {int(snap.get('nproc') or 1)} 核 · "
            f"steal {float(snap.get('steal_pct') or 0):.1f}%"
        )
    if kind == "mem":
        total = int(snap.get("mem_total") or 0)
        used = total - int(snap.get("mem_available") or 0)
        pct = _mem_pct(snap)
        swap_used = int(snap.get("swap_total") or 0) - int(snap.get("swap_free") or 0)
        return (
            f"🧠 <b>{report.h(name)} · 内存</b>\n\n"
            f"{hostinfo.fmt_mib(used)} / {hostinfo.fmt_mib(total)}  ({pct:.0f}%)\n"
            f"{report.progress_bar(pct)}\n"
            f"swap {hostinfo.fmt_mib(swap_used)} / {hostinfo.fmt_mib(int(snap.get('swap_total') or 0))}"
        )
    if kind == "disk":
        total = int(snap.get("disk_total") or 0) or 1
        used = int(snap.get("disk_used") or 0)
        pct = 100.0 * used / total
        return (
            f"💾 <b>{report.h(name)} · 磁盘</b>\n\n"
            f"{report.fmt_bytes(used)} / {report.fmt_bytes(total)}  ({pct:.0f}%)\n"
            f"{report.progress_bar(pct)}\n"
            f"剩余 {report.fmt_bytes(int(snap.get('disk_avail') or 0))}"
        )
    if kind == "net":
        return (
            f"🌐 <b>{report.h(name)} · 瞬时网速</b>\n\n"
            f"↓ {hostinfo.fmt_bps(float(snap.get('net_rx_bps') or 0))}\n"
            f"↑ {hostinfo.fmt_bps(float(snap.get('net_tx_bps') or 0))}\n"
            f"这是心跳时约 0.4 秒采样。要更准请用 <code>/bw {report.h(name)}</code>。"
        )
    if kind == "today":
        return (
            f"📅 <b>{report.h(name)} · 今日</b>\n\n"
            f"<pre>"
            f"  ↓ 入站  {report.fmt_gb(int(snap.get('today_rx') or 0))}\n"
            f"  ↑ 出站  {report.fmt_gb(int(snap.get('today_tx') or 0))}\n"
            f"  ∑ 合计  {report.fmt_gb(int(snap.get('today_total') or 0))}"
            f"</pre>"
        )
    if kind == "xray":
        ok = bool(snap.get("xray_ok"))
        return (
            f"🔌 <b>{report.h(name)} · Xray</b>\n\n"
            f"{'✅ 443 通' if ok else '❌ 443 不通'}\n"
            f"RSS {hostinfo.fmt_mib(int(snap.get('xray_rss') or 0))}"
        )
    if kind == "uptime":
        return (
            f"⏱ <b>{report.h(name)} · 运行时间</b>\n\n"
            f"{hostinfo.fmt_duration(float(snap.get('uptime_sec') or 0))}\n"
            f"<code>{report.h(snap.get('hostname') or '')}</code>"
        )
    return node_detail(row)


def bw_result(name: str, data: dict[str, Any]) -> str:
    seconds = float(data.get("seconds") or 0)
    rx_bps = float(data.get("rx_bps") or 0)
    tx_bps = float(data.get("tx_bps") or 0)
    return (
        f"📡 <b>{report.h(name)} 实时带宽</b>  {seconds:.1f}s\n\n"
        f"<pre>"
        f"  ↓ 入  {hostinfo.fmt_bps(rx_bps):>12}  {report.fmt_bytes(int(data.get('rx_bytes') or 0))}\n"
        f"  ↑ 出  {hostinfo.fmt_bps(tx_bps):>12}  {report.fmt_bytes(int(data.get('tx_bytes') or 0))}\n"
        f"  ∑     {hostinfo.fmt_bps(rx_bps + tx_bps):>12}"
        f"</pre>\n"
        f"被动采样网卡计数，不主动打流。"
    )


def add_help(name: str, hub_url: str, cap_text: str, reset_day: int) -> str:
    return (
        f"已纳入覆盖：<code>{report.h(name)}</code>\n"
        f"额度 {report.h(cap_text)} · 重置日 {reset_day}\n\n"
        f"在目标机执行：\n"
        f"<pre>sudo ./install-host.sh --role agent \\\n"
        f"  --hub {report.h(hub_url)} \\\n"
        f"  --name {report.h(name)}</pre>\n"
        f"Fleet token 与 Hub 相同，写在那台机的 "
        f"<code>/etc/traffic-monitor.env</code>。"
    )
