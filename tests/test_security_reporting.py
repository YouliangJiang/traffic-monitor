import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import bot
import hub
import security_events as security
import security_formatters as formatting


NOW = security.timestamp("2026-09-30T10:00:00Z")


def signal(identity, severity="high", age=600, node="agent-a"):
    return {
        "event_id": f"{identity:032x}",
        "event": "reality_replay_differential_probe"
        if severity == "high"
        else "non_tls_connection_burst"
        if severity == "medium"
        else "unclassified_first_flight",
        "severity": severity,
        "first_seen": datetime.fromtimestamp(NOW - age - 5, security.SG).isoformat(
            timespec="seconds"
        ),
        "last_seen": datetime.fromtimestamp(NOW - age, security.SG).isoformat(
            timespec="seconds"
        ),
        "target_ip": "203.0.113.10",
        "target_port": 443,
        "distinct_flows": 14 if severity == "high" else 5,
        "session_id_variants": 2,
        "probe_source_ips": ["198.51.100.5"],
        "evidence": [
            "exact_clienthello_replay",
            "serverhello_then_malformed_tls_record",
        ]
        if severity == "high"
        else ["repeated_non_tls_connections_on_monitored_tls_entry"],
        "node": node,
    }


class SecurityReportingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.env = patch.dict(
            os.environ,
            {
                "TRAFFIC_MONITOR_STATE_DIR": str(self.root),
                "UI_LANG": "zh",
                "ADMIN_TOKEN": "admin",
                "NODE_NAME": "local",
                "BILLING_RESET_DAY": "1",
            },
        )
        self.env.start()
        self.rows = [
            {
                "name": "agent-a",
                "security": {
                    "available": True,
                    "rss_bytes": 12345,
                    "stats": {"Packets": 123456},
                },
            }
        ]

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def store(self, values):
        store = security.EventStore()
        with patch.object(security.time, "time", return_value=NOW):
            for value in values:
                store.ingest(value["node"], [value], {})
        return store

    def test_high_signal_is_not_hidden_by_newer_background_observations(self):
        values = [signal(1, age=1800)] + [
            signal(i, "low", age=30) for i in range(2, 60)
        ]
        risk = self.store(values).risk_report(NOW - 86400, NOW)
        self.assertEqual([value["severity"] for value in risk["events"]], ["high"])
        self.assertEqual(risk["last_hour"]["high"], 1)
        text = formatting.overview(self.rows, risk)
        self.assertIn("发现 1 次高危探测信号", text)
        self.assertIn("畸形", formatting.notification("agent-a", risk["events"][0]))
        self.assertNotIn("RSS", text)
        self.assertNotIn("抓包", text)
        self.assertNotIn("GFW", text)

    def test_backfill_is_filtered_by_observation_time_not_receipt_time(self):
        old = dict(signal(1, age=2 * 86400), backfill=True)
        recent = signal(2, "medium", age=7200)
        risk = self.store([old, recent]).risk_report(NOW - 86400, NOW)
        self.assertEqual(len(risk["events"]), 1)
        self.assertEqual(risk["events"][0]["severity"], "medium")
        self.assertEqual(risk["last_hour"].get("medium", 0), 0)
        text = formatting.overview(self.rows, risk)
        self.assertIn("未发现高危信号；有 1 次可疑异常", text)

    def test_background_observations_do_not_become_suspicious(self):
        risk = self.store([signal(1, "low")]).risk_report(NOW - 86400, NOW)
        text = formatting.overview(self.rows, risk)
        self.assertIn("未发现高危探测信号，也未见可疑异常", text)
        self.assertNotIn("需要关注的信号", text)
        self.assertEqual(risk["events"], [])

    def test_offline_sensor_never_gets_an_unqualified_clear_verdict(self):
        risk = self.store([]).risk_report(NOW - 86400, NOW)
        self.rows[0]["security"]["available"] = False
        text = formatting.overview(self.rows, risk)
        self.assertIn("监测不完整", text)
        self.assertIn("0/1", text)
        self.assertNotIn("也未见可疑异常", text)

    def test_high_priority_and_node_filter_apply_to_the_entire_window(self):
        values = [
            signal(1, age=8000),
            signal(2, "medium", age=20),
            signal(3, node="agent-b"),
        ]
        risk = self.store(values).risk_report(NOW - 86400, NOW, "agent-a")
        self.assertEqual(
            [value["severity"] for value in risk["events"]], ["high", "medium"]
        )
        self.assertTrue(all(value["node"] == "agent-a" for value in risk["counts"]))

    def test_detail_uses_human_evidence_and_escapes_external_strings(self):
        value = signal(1)
        value.update(sni="<script>", probe_source_ips=["<source>"])
        text = formatting.notification("<node>", value)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;source&gt;", text)
        self.assertIn("相同的加密握手", text)
        self.assertIn("端口 443", text)
        self.assertNotIn("exact_clienthello_replay", text)
        self.assertNotIn("规则评分", text)
        self.assertNotIn("归属", text)

    def test_report_window_callbacks_and_refresh_preserve_selection(self):
        risk = self.store([signal(1)]).risk_report(NOW - 3600, NOW)
        with (
            patch.object(bot, "nodes", return_value=self.rows),
            patch.object(bot, "hub_call", return_value={"report": risk}) as api,
            patch.object(bot.time, "time", return_value=NOW),
        ):
            reply = bot.cmd_security(["all", "1h"], {})
        self.assertIn("start=" + str(NOW - 3600), api.call_args[0][1])
        data = [
            button["callback_data"]
            for row in reply.markup["inline_keyboard"]
            for button in row
        ]
        self.assertIn("security:all:1", data)
        self.assertIn("sd:all", data)
        self.assertTrue(all(len(value.encode()) <= 64 for value in data))
        self.assertEqual(
            bot.parse_callback("security:agent-a:168"), ("security", ["agent-a", "7d"])
        )
        self.assertEqual(bot.parse_callback("security:all:100000"), ("", []))
        self.assertEqual(
            bot.parse_callback("sd:all"), ("security_diagnostics", ["all"])
        )

    def test_api_rejects_invalid_windows(self):
        instance = hub.Hub()
        for query in ["start=NaN", "start=0&end=99999999999", "start=oops"]:
            handler = object.__new__(hub.HubHandler)
            handler.path = "/v1/security/overview?" + query
            handler._auth = lambda: True
            result = []
            handler._send = lambda code, data: result.append((code, data))
            with patch.object(hub, "HUB", instance):
                handler.do_GET()
            self.assertEqual(result[0][0], 400)

    def test_extra_details_do_not_change_legacy_event_identity(self):
        base = signal(1)
        base.pop("event_id")
        richer = dict(base, window_ms=5000, payload_class="http_plaintext")
        self.assertEqual(
            security.normalize(base)["event_id"], security.normalize(richer)["event_id"]
        )

    def test_daily_and_diagnostics_have_separate_purposes(self):
        risk = self.store([signal(1), signal(2, "medium")]).risk_report(
            NOW - 86400, NOW
        )
        day = datetime.fromtimestamp(NOW, security.SG)
        text = formatting.daily(self.rows, risk, day, day)
        self.assertIn("高危探测信号", text)
        self.assertIn("198.51.100.5", text)
        self.assertNotIn("抓包", text)
        self.assertIn("抓包 123456", formatting.diagnostics(self.rows))


if __name__ == "__main__":
    unittest.main()
