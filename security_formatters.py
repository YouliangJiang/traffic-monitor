"""Plain, localized security summaries and bounded Telegram HTML."""

import i18n
import report
from security_events import SG


def severity_label(value):
    return i18n.t("security." + value)


def notification(node, event):
    glyph = {"high": "🔴", "medium": "🟠", "low": "⚪"}.get(event["severity"], "⚪")
    text = [
        glyph + " <b>" + i18n.t("security.alert") + " · " + report.h(node) + "</b>",
        report.h(i18n.t("security.kind." + event["event"])),
        i18n.t(
            "security.target",
            target=report.h(
                str(event.get("target_ip", "")) + ":" + str(event["target_port"])
            ),
        ),
        i18n.t(
            "security.signals",
            flows=event.get("distinct_flows", 0),
            score=event.get("score", 0),
            variants=event.get("session_id_variants", 0),
        ),
    ]
    if event.get("sni"):
        text.append("SNI: " + report.h(event["sni"]))
    evidence = event.get("evidence") or []
    if evidence:
        text.append(
            "<blockquote>" + report.h("\n".join(evidence[:4])) + "</blockquote>"
        )
    text.append(i18n.t("security.observation"))
    return "\n".join(text)


def sensor_line(node, status):
    label = (
        i18n.t("security.healthy")
        if status.get("available")
        else i18n.t("security.unavailable")
    )
    stats = status.get("stats") or {}
    mode = i18n.t(
        "security.mode."
        + ("reality" if status.get("mode") == "reality" else "tls_observation")
    )
    return i18n.t(
        "security.sensor",
        node=report.h(node),
        state=label,
        mode=mode,
        memory=report.fmt_bytes(status.get("rss_bytes", 0)),
        packets=stats.get("Packets", 0),
        drops=status.get("capture_dropped", 0),
        evictions=stats.get("FlowEvictions", 0),
        overflows=stats.get("BufferOverflows", 0),
        pending=status.get("pending", 0),
    )


def overview(rows, events):
    lines = ["<b>" + i18n.t("security.title") + "</b>"]
    for row in rows:
        lines.append(sensor_line(row["name"], row.get("security") or {}))
    lines.append("\n" + i18n.t("security.recent"))
    if not events:
        lines.append(i18n.t("security.empty"))
    for event in events[:6]:
        lines.append(
            report.h(event["node"])
            + " · "
            + severity_label(event["severity"])
            + " · "
            + report.h(i18n.t("security.kind." + event["event"]))
            + " · "
            + report.h(str(event.get("target_port", "")))
        )
    return "\n".join(lines)


def daily(rows, counts, start, end):
    lines = [
        "<b>" + i18n.t("security.daily") + "</b>",
        start.astimezone(SG).strftime("%Y-%m-%d")
        + " · "
        + i18n.t("security.daily_window"),
    ]
    totals = {row["name"]: {"high": 0, "medium": 0, "low": 0} for row in rows}
    for count in counts:
        totals.setdefault(count["node"], {"high": 0, "medium": 0, "low": 0})[
            count["severity"]
        ] += count["count"]
    for row in rows:
        c = totals[row["name"]]
        status = row.get("security") or {}
        mode = i18n.t(
            "security.mode."
            + ("reality" if status.get("mode") == "reality" else "tls_observation")
        )
        lines.append(
            i18n.t(
                "security.daily_node",
                node=report.h(row["name"]),
                high=c["high"],
                medium=c["medium"],
                low=c["low"],
                state=i18n.t("security.healthy")
                if status.get("available")
                else i18n.t("security.unavailable"),
                mode=mode,
            )
        )
        stats = status.get("stats") or {}
        if (
            status.get("capture_dropped")
            or stats.get("FlowEvictions")
            or stats.get("BufferOverflows")
        ):
            lines.append(
                i18n.t(
                    "security.loss",
                    drops=status.get("capture_dropped", 0),
                    evictions=stats.get("FlowEvictions", 0),
                    overflows=stats.get("BufferOverflows", 0),
                )
            )
    lines.append(i18n.t("security.received_window"))
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
                {"text": i18n.t("btn.refresh"), "callback_data": "go:" + node},
            ],
        ]
    }
