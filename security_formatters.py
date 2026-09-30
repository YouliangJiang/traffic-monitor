"""Risk-first, localized Telegram summaries with bounded, escaped evidence."""

from datetime import datetime
import i18n
import report
from security_events import SG, timestamp

WINDOWS = {1: "1h", 24: "24h", 168: "7d"}
SIGNAL_KINDS = {
    "reality_replay_differential_probe": "replay",
    "tls_clienthello_delayed_replay": "delayed",
    "non_tls_connection_burst": "non_tls",
    "tcp_syn_without_clienthello_burst": "syn",
    "unclassified_first_flight": "background",
}
EVIDENCE_KEYS = {
    "exact_clienthello_replay": "replay",
    "session_id_differential_cluster": "session",
    "session_variants_have_different_server_outcomes": "response",
    "serverhello_then_malformed_tls_record": "malformed",
    "concurrent_short_connection_cluster": "burst",
    "bundled_probe_payload_pattern": "pattern",
    "regular_close_after_abnormal_data": "close",
    "exact_tls13_clienthello_reobserved_beyond_short_window": "delayed",
    "repeated_non_tls_connections_on_monitored_tls_entry": "non_tls",
    "many_distinct_syn_connections_without_clienthello_after_grace": "syn",
    "bounded_first_flight_without_parsed_clienthello": "background",
}


def moment(value):
    try:
        seconds = timestamp(value) if isinstance(value, str) else float(value)
        return datetime.fromtimestamp(seconds, SG).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return i18n.t("security.unknown_time")


def behavior(event):
    return i18n.t("security.behavior." + SIGNAL_KINDS.get(event["event"], "unknown"))


def evidence(event):
    labels = []
    for code in event.get("evidence") or []:
        key = EVIDENCE_KEYS.get(code)
        if key:
            label = i18n.t("security.evidence." + key)
            if label not in labels:
                labels.append(label)
    return labels[:4] or [behavior(event)]


def sources(event, limit=3):
    values = list(dict.fromkeys(event.get("probe_source_ips") or []))
    if not values:
        return i18n.t("security.source_missing")
    text = ", ".join(report.h(value[:64]) for value in values[:limit])
    if len(values) > limit:
        text += i18n.t("security.source_more", count=len(values))
    return text


def duration(event):
    try:
        milliseconds = event.get("window_ms")
        seconds = (
            milliseconds / 1000
            if milliseconds is not None
            else timestamp(event["last_seen"]) - timestamp(event["first_seen"])
        )
        return max(1, round(seconds))
    except (ValueError, KeyError, TypeError):
        return 0


def signal_facts(event):
    seconds = duration(event)
    return i18n.t(
        "security.signal_facts" if seconds else "security.signal_facts_unknown",
        flows=event.get("distinct_flows", 0),
        seconds=seconds,
        port=event["target_port"],
    )


def notification(node, event):
    severity = event["severity"]
    glyph = {"high": "🔴", "medium": "🟠", "low": "⚪"}[severity]
    lines = [
        glyph
        + " <b>"
        + i18n.t("security.signal." + severity)
        + " · "
        + report.h(node)
        + "</b>",
        "<b>" + report.h(behavior(event)) + "</b>",
        i18n.t("security.occurred", time=moment(event["last_seen"])),
        signal_facts(event),
        i18n.t("security.sources", sources=sources(event, 4)),
        "\n" + i18n.t("security.why"),
    ]
    lines.extend("• " + report.h(label) for label in evidence(event))
    if event.get("sni"):
        lines.append("SNI: " + report.h(event["sni"][:120]))
    if event.get("backfill"):
        lines.append(i18n.t("security.backfilled"))
    if severity != "low":
        lines.append("\n" + i18n.t("security.action." + severity))
    return "\n".join(lines)


def totals(rows, counts):
    by_node = {row["name"]: {"high": 0, "medium": 0, "low": 0} for row in rows}
    for item in counts:
        by_node.setdefault(item["node"], {"high": 0, "medium": 0, "low": 0})[
            item["severity"]
        ] += item["count"]
    overall = {
        level: sum(values[level] for values in by_node.values())
        for level in ["high", "medium", "low"]
    }
    return by_node, overall


def verdict(overall, complete=True):
    high, medium = overall["high"], overall["medium"]
    if high:
        return "🔴 <b>" + i18n.t("security.verdict.high", count=high) + "</b>"
    if not complete:
        return "⚪ <b>" + i18n.t("security.verdict.incomplete") + "</b>"
    if medium:
        return "🟠 <b>" + i18n.t("security.verdict.medium", count=medium) + "</b>"
    return "🟢 <b>" + i18n.t("security.verdict.clear") + "</b>"


def node_lines(rows, by_node):
    ordered = sorted(
        rows,
        key=lambda row: (
            -by_node[row["name"]]["high"],
            -by_node[row["name"]]["medium"],
            row["name"],
        ),
    )
    lines = []
    for row in ordered:
        values = by_node[row["name"]]
        if values["high"]:
            text = i18n.t(
                "security.node.high", high=values["high"], medium=values["medium"]
            )
            glyph = "🔴"
        elif values["medium"]:
            text = i18n.t("security.node.medium", count=values["medium"])
            glyph = "🟠"
        else:
            text = i18n.t("security.node.clear")
            glyph = "🟢"
        if not (row.get("security") or {}).get("available"):
            text += " · " + i18n.t("security.node.offline")
            if not values["high"] and not values["medium"]:
                glyph = "⚪"
        lines.append(glyph + " " + report.h(row["name"]) + " · " + text)
    return lines


def focus_lines(events, compact=False):
    if not events:
        return []
    lines = ["\n<b>" + i18n.t("security.focus") + "</b>"]
    for event in events[: 2 if compact else 3]:
        glyph = "🔴" if event["severity"] == "high" else "🟠"
        lines.append(
            glyph
            + " <b>"
            + report.h(event["node"])
            + " · "
            + report.h(behavior(event))
            + "</b>"
        )
        lines.append(moment(event["last_seen"]) + " · " + signal_facts(event))
        lines.append(i18n.t("security.sources", sources=sources(event, 2)))
        if not compact:
            lines.append(i18n.t("security.reason", reason=report.h(evidence(event)[0])))
    return lines


def coverage(rows):
    online = sum(bool((row.get("security") or {}).get("available")) for row in rows)
    text = i18n.t("security.coverage", online=online, total=len(rows))
    missing = [
        row["name"] for row in rows if not (row.get("security") or {}).get("available")
    ]
    if missing:
        text += "\n" + i18n.t(
            "security.coverage_gap", nodes=", ".join(report.h(name) for name in missing)
        )
    return text


def overview(rows, risk):
    by_node, overall = totals(rows, risk["counts"])
    complete = all((row.get("security") or {}).get("available") for row in rows)
    hours = round((risk["end"] - risk["start"]) / 3600)
    window = (
        i18n.t("security.window." + str(hours))
        if hours in WINDOWS
        else i18n.t("security.selected_window")
    )
    lines = [
        "<b>" + i18n.t("security.title", window=window) + "</b>",
        verdict(overall, complete),
    ]
    lines.append(
        i18n.t("security.window", start=moment(risk["start"]), end=moment(risk["end"]))
    )
    hour = risk.get("last_hour") or {}
    lines.append(
        i18n.t(
            "security.last_hour", high=hour.get("high", 0), medium=hour.get("medium", 0)
        )
    )
    lines.append("")
    lines.extend(node_lines(rows, by_node))
    lines.extend(focus_lines(risk["events"]))
    if overall["high"]:
        lines.append("\n" + i18n.t("security.action.high"))
    elif overall["medium"]:
        lines.append("\n" + i18n.t("security.action.medium"))
    lines.append("\n" + coverage(rows))
    if overall["low"]:
        lines.append(i18n.t("security.background", count=overall["low"]))
    return "\n".join(lines)


def daily(rows, risk, start, end):
    by_node, overall = totals(rows, risk["counts"])
    lines = [
        "<b>" + i18n.t("security.daily") + "</b>",
        start.astimezone(SG).strftime("%Y-%m-%d")
        + " · "
        + i18n.t("security.daily_window"),
        verdict(
            overall, all((row.get("security") or {}).get("available") for row in rows)
        ),
    ]
    lines.extend(node_lines(rows, by_node))
    lines.extend(focus_lines(risk["events"], compact=True))
    lines.append(i18n.t("security.observed_window"))
    return "\n".join(lines)


def diagnostics(rows):
    lines = ["<b>" + i18n.t("security.diagnostics") + "</b>"]
    for row in rows:
        status = row.get("security") or {}
        stats = status.get("stats") or {}
        mode = i18n.t(
            "security.mode."
            + ("reality" if status.get("mode") == "reality" else "tls_observation")
        )
        lines.append(
            i18n.t(
                "security.sensor",
                node=report.h(row["name"]),
                state=i18n.t("security.healthy")
                if status.get("available")
                else i18n.t("security.unavailable"),
                mode=mode,
                memory=report.fmt_bytes(status.get("rss_bytes", 0)),
                packets=stats.get("Packets", 0),
                drops=status.get("capture_dropped", 0),
                evictions=stats.get("FlowEvictions", 0),
                overflows=stats.get("BufferOverflows", 0),
                pending=status.get("pending", 0),
            )
        )
    return "\n".join(lines)


def markup(node, identity):
    return {
        "inline_keyboard": [
            [
                {
                    "text": i18n.t("security.details"),
                    "callback_data": "se:" + node + ":" + identity[:16],
                }
            ],
            [
                {
                    "text": i18n.t("security.recent"),
                    "callback_data": "security:" + node,
                },
                {"text": i18n.t("btn.refresh"), "callback_data": "security:" + node},
            ],
        ]
    }
