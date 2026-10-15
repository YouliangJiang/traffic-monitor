"""Risk-first, localized Telegram summaries with bounded, escaped evidence."""

from datetime import datetime
import i18n
import report
import ip_data
from security_events import SG, timestamp

WINDOWS = {1: "1h", 24: "24h", 168: "7d"}
SIGNAL_KINDS = {
    "reality_replay_differential_probe": "replay",
    "tls_clienthello_delayed_replay": "delayed",
    "non_tls_connection_burst": "non_tls",
    "tcp_syn_without_clienthello_burst": "syn",
    "distributed_syn_without_clienthello_burst": "distributed",
    "tls_unexpected_sni": "sni",
    "unclassified_first_flight": "background",
}
OUTCOMES = {
    "handshake_then_idle", "handshake_then_client_rst", "handshake_then_client_fin",
    "handshake_then_server_rst", "handshake_then_server_fin", "synack_without_client_ack",
    "syn_without_server_synack", "syn_then_client_rst", "syn_then_client_fin",
    "syn_then_server_rst", "syn_then_server_fin", "tracking_evicted",
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
    "many_sources_open_connections_without_clienthello": "distributed",
    "clienthello_sni_outside_configured_reality_names": "sni",
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


def ip_context(info):
    status = info.get("status")
    if status in {"pending", "unavailable", "invalid", "non_public"}:
        return i18n.t("security.ip." + status)
    values = []
    if info.get("scanner"):
        values.append(i18n.t("security.ip.scanner", name=info["scanner"]))
    country = info.get("country", "")
    if i18n.lang() == "zh":
        country = (
            ip_data.country_name(info.get("country_code"), "zh")
            if info.get("country_code")
            else country
        )
    place = list(
        dict.fromkeys(
            value for value in [country, info.get("region"), info.get("city")] if value
        )
    )
    if place:
        values.append(i18n.t("security.ip.location", place=" / ".join(place)[:140]))
    network = ("AS" + str(info["asn"]) + " " if info.get("asn") else "") + (
        info.get("org") or info.get("isp") or ""
    )
    if network:
        values.append(network[:100])
    if not info.get("scanner") and info.get("network_type") == "hosting":
        values.append(i18n.t("security.ip.hosting"))
    if info.get("stale"):
        values.append(i18n.t("security.ip.stale"))
    return " · ".join(values)[:300] or i18n.t("security.ip.unavailable")


def sources(event, limit=3):
    values = list(dict.fromkeys(event.get("probe_source_ips") or []))
    if not values:
        return i18n.t("security.source_missing")
    details = event.get("source_info") or {}
    parts = []
    for value in values[:limit]:
        line = report.h(value[:64])
        if value in details:
            line += " · " + report.h(ip_context(details[value]))
        parts.append(line)
    text = "\n".join(parts)
    observed = max(len(values), event.get("distinct_source_ips_observed", 0))
    if observed > len(values):
        text += i18n.t("security.source_observed", count=observed)
    elif len(values) > limit:
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


def ranked(counts, limit):
    items = [(label, count) for label, count in (counts or {}).items() if type(count) is int]
    return sorted(items, key=lambda item: (-item[1], item[0]))[:limit]


def connection_lines(event):
    lines = []
    kind = event.get("event")
    if kind == "distributed_syn_without_clienthello_burst":
        ended = "campaign_ended" in (event.get("evidence") or [])
        lines.append(i18n.t("security.campaign." + ("ended" if ended else "active")))
        if not ended and "window_connections" in event:
            lines.append(i18n.t(
                "security.campaign.window", connections=event["window_connections"],
                sources=event.get("window_sources", 0)))
    outcomes = ranked(event.get("connection_outcomes"), 3)
    if outcomes:
        items = [
            i18n.t("security.outcome." + label if label in OUTCOMES else "security.outcome.other")
            + " " + str(count)
            for label, count in outcomes
        ]
        lines.append(i18n.t("security.outcomes", items=" · ".join(items)))
    fingerprints = [item for item in ranked(event.get("syn_fingerprints"), 2) if item[0] != "other"]
    if fingerprints:
        ttls = ", ".join(report.h(label[:8]) for label, _ in ranked(event.get("syn_ttl_values"), 3) if label != "other")
        lines.append(i18n.t(
            "security.fingerprint", value=report.h(fingerprints[0][0][:80]),
            count=fingerprints[0][1], ttls=ttls or "-"))
    names = [item for item in event.get("sni_observations") or [] if isinstance(item, dict)]
    if names:
        items = [
            i18n.t("security.sni.item", name=report.h(str(item.get("sni", ""))[:80]),
                   hellos=item.get("hellos", 0), sources=item.get("sources", 0))
            for item in names[:5]
        ]
        lines.append("SNI: " + " · ".join(items))
        omitted = len(names) - 5 + event.get("sni_names_omitted", 0)
        if omitted > 0:
            lines.append(i18n.t("security.sni.omitted", count=omitted))
    return lines


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
    lines.extend(connection_lines(event))
    if event.get("event") == "reality_replay_differential_probe":
        observed = event.get("response_differential_observed")
        if type(observed) is bool:
            key = "security.response.unconfirmed"
            if observed:
                key = "security.response.confirmed" if event.get("response_observation_window_ms", 0) > 0 else "security.response.legacy"
            lines.append(i18n.t(key))
        if event.get("response_evidence_incomplete"):
            lines.append(i18n.t("security.response.incomplete"))
        if event.get("response_capture_gap_observed"):
            lines.append(i18n.t("security.response.capture_gap"))
        if event.get("response_detail_write_failed"):
            lines.append(i18n.t("security.response.detail_failed"))
        elif event.get("response_detail_retained"):
            lines.append(i18n.t("security.response.detail_retained"))
    details = event.get("source_info") or {}
    for ip in list(dict.fromkeys(event.get("probe_source_ips") or []))[:2]:
        info = details.get(ip) or {}
        if info.get("hostname"):
            lines.append(
                i18n.t(
                    "security.ip.ptr",
                    ip=report.h(ip[:64]),
                    hostname=report.h(info["hostname"][:160]),
                    check=i18n.t("security.ip.verified")
                    if info.get("forward_verified")
                    else i18n.t("security.ip.unverified"),
                )
            )
        if info.get("isp") and info.get("isp") != info.get("org"):
            lines.append(i18n.t("security.ip.isp", name=report.h(info["isp"][:100])))
    if any(info.get("status") in {"ready", "partial"} for info in details.values()):
        lines.append(i18n.t("security.ip.note"))
    if any(info.get("provider") == "DB-IP Lite" for info in details.values()):
        lines.append(i18n.t("security.ip.dbip"))
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
    if any(
        info.get("provider") == "DB-IP Lite"
        for event in risk["events"]
        for info in (event.get("source_info") or {}).values()
    ):
        lines.append(i18n.t("security.ip.dbip"))
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
    if any(
        info.get("provider") == "DB-IP Lite"
        for event in risk["events"]
        for info in (event.get("source_info") or {}).values()
    ):
        lines.append(i18n.t("security.ip.dbip"))
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
                drops=status.get("capture_dropped", 0)
                if status.get("capture_stats_available") is True
                else i18n.t("security.capture_unknown"),
                evictions=stats.get("FlowEvictions", 0),
                overflows=stats.get("BufferOverflows", 0),
                pending=status.get("pending", 0),
            )
        )
        limits = status.get("limits") or {}
        if limits:
            lines.append(i18n.t(
                "security.sensor.capacity", flows=stats.get("TrackedFlows", 0),
                max_flows=limits.get("max_tracked_flows", "?"),
                history=stats.get("ReplayHistoryEntries", 0),
                max_history=limits.get("max_replay_entries", "?"),
                hours=round(limits.get("replay_window_seconds", 0) / 3600, 1),
                evictions=stats.get("ReplayHistoryEvictions", 0),
            ))
        if "ProbeResponseCaptureGaps" in stats:
            lines.append(i18n.t(
                "security.sensor.responses", gaps=stats.get("ProbeResponseCaptureGaps", 0),
                omitted=stats.get("ProbeResponseRecordDrops", 0),
                failures=stats.get("ProbeResponseDetailWriteFailures", 0),
            ))
        if "EmptyConnectionsFinalized" in stats:
            lines.append(i18n.t(
                "security.sensor.observer", midstream=stats.get("MidstreamPacketsIgnored", 0),
                empty=stats.get("EmptyConnectionsFinalized", 0),
                evicted=stats.get("EmptyConnectionEvictions", 0),
                campaigns=stats.get("DistributedSYNCampaigns", 0),
                active=stats.get("ActiveDistributedSYNCampaigns", 0),
                sni=stats.get("UnexpectedSNIHellos", 0),
            ))
        if "monitored_server_names" in status:
            names = status["monitored_server_names"]
            lines.append(i18n.t("security.sensor.scope", names=report.h(", ".join(names)[:500]) if names else i18n.t("security.sensor.all_sni")))
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
