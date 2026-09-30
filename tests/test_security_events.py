import io, json, os, sqlite3, tempfile, time, unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import security_events as security
import security_formatters
import hub
import bot
import util


def event(identity="a" * 32, severity="high"):
    return {
        "event_id": identity,
        "schema_version": 1,
        "event": "reality_replay_differential_probe",
        "severity": severity,
        "last_seen": "2026-09-30T08:00:00Z",
        "target_ip": "203.0.113.10",
        "target_port": 443,
        "score": 80,
        "distinct_flows": 14,
        "session_id_variants": 2,
        "evidence": ["replay"],
        "probe_source_ips": ["198.51.100.5"],
    }


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "TRAFFIC_MONITOR_STATE_DIR": str(self.root),
                "ADMIN_TOKEN": "admin-key",
                "NODE_NAME": "local",
                "BILLING_RESET_DAY": "1",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def reader(self):
        return security.EventReader(
            self.root / "events.jsonl", self.root / "status.json"
        )

    def test_checkpoint_and_ack_are_durable(self):
        log = self.root / "events.jsonl"
        log.write_text(json.dumps(event()) + "\n")
        reader = self.reader()
        reader.scan()
        batch = reader.batch()
        self.assertEqual(len(batch), 1)
        self.assertTrue(batch[0]["backfill"])
        restarted = self.reader()
        restarted.scan()
        self.assertEqual(len(restarted.batch()), 1)
        restarted.acknowledge([batch[0]["event_id"]])
        self.assertEqual(restarted.batch(), [])
        with log.open("a") as output:
            output.write(json.dumps(event("b" * 32)) + "\n")
        restarted.scan()
        self.assertFalse(restarted.batch()[0]["backfill"])

    def test_partial_lines_wait_and_rename_rotation_is_consumed(self):
        log = self.root / "events.jsonl"
        line = json.dumps(event())
        log.write_text(line[:40])
        reader = self.reader()
        reader.scan()
        self.assertEqual(reader.batch(), [])
        with log.open("a") as out:
            out.write(line[40:] + "\n")
        log.rename(self.root / "events.jsonl.1")
        log.write_text(json.dumps(event("b" * 32)) + "\n")
        reader.scan()
        self.assertEqual(len(reader.batch()), 2)

    def test_invalid_and_oversized_lines_do_not_block_later_events(self):
        (self.root / "events.jsonl").write_text(
            "{invalid}\n"
            + "x" * (security.MAX_EVENT_BYTES + 100)
            + "\n"
            + json.dumps(event())
            + "\n"
        )
        reader = self.reader()
        reader.scan()
        self.assertEqual(len(reader.batch()), 1)
        self.assertEqual(reader.status()["reader_counters"]["malformed"], 2)

    def test_dated_rotation_keeps_the_inode_cursor_and_unread_evidence(self):
        log = self.root / "events.jsonl"
        log.write_text(json.dumps(event()) + "\n")
        reader = self.reader()
        reader.scan()
        reader.acknowledge(["a" * 32])
        with log.open("a") as output:
            output.write(json.dumps(event("b" * 32)) + "\n")
        log.rename(self.root / "events.jsonl-20261001-000000")
        log.write_text(json.dumps(event("c" * 32)) + "\n")
        reader.scan()
        self.assertEqual({item["event_id"] for item in reader.batch()}, {"b" * 32, "c" * 32})

    def test_backfill_marker_survives_multiple_scans(self):
        (self.root / "events.jsonl").write_text(
            "".join(json.dumps(event(f"{i:032x}")) + "\n" for i in range(200))
        )
        reader = self.reader()
        reader.scan()
        reader.scan()
        with security.database("security-outbox.sqlite3") as db:
            rows = db.execute("SELECT data FROM outbox").fetchall()
        self.assertEqual(len(rows), 200)
        self.assertTrue(all(json.loads(row["data"])["backfill"] for row in rows))

    def test_full_outbox_resumes_without_losing_the_next_line(self):
        (self.root / "events.jsonl").write_text(
            json.dumps(event()) + "\n" + json.dumps(event("b" * 32)) + "\n"
        )
        reader = self.reader()
        with patch.object(security, "MAX_OUTBOX", 1):
            reader.scan()
            self.assertEqual(reader.batch()[0]["event_id"], "a" * 32)
            reader.acknowledge(["a" * 32])
            reader.scan()
            self.assertEqual(reader.batch()[0]["event_id"], "b" * 32)

    def test_hub_deduplicates_and_notifies_only_live_high(self):
        store = security.EventStore()
        value = event()
        store.ingest("agent-a", [value], {})
        store.ingest("agent-a", [value], {})
        self.assertEqual(len(store.recent()), 1)
        item = store.pending()
        self.assertEqual(item["node"], "agent-a")
        store.notified(item["node"], item["id"], True)
        self.assertIsNone(store.pending())
        store.ingest(
            "agent-a",
            [dict(event("b" * 32), backfill=True), event("c" * 32, "medium")],
            {},
        )
        self.assertIsNone(store.pending())
        self.assertEqual(len(store.recent()), 3)

    def test_retry_is_persistent(self):
        store = security.EventStore()
        store.ingest("agent-a", [event()], {})
        store.notified("agent-a", "a" * 32, False)
        self.assertIsNone(store.pending())
        with patch.object(security.time, "time", return_value=time.time() + 301):
            self.assertEqual(security.EventStore().pending()["attempts"], 1)

    def test_summary_counts_receipt_window(self):
        store = security.EventStore()
        now = time.time()
        store.ingest("agent-a", [event()], {})
        result = store.summary(now - 1, now + 1)
        self.assertEqual(result[0]["count"], 1)
        self.assertEqual(store.summary(0, 1), [])

    def test_sensor_staleness_and_safe_html(self):
        reader = self.reader()
        self.assertFalse(reader.status()["available"])
        util.save_json(
            self.root / "status.json",
            {
                "running": True,
                "updated_at": "2000-01-01T00:00:00Z",
                "stats": {"Packets": 10},
            },
        )
        self.assertFalse(reader.status()["available"])
        text = security_formatters.notification(
            "agent-a", dict(event(), sni="<script>")
        )
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)

    def test_legacy_go_fractions_are_accepted_and_exported_as_seconds(self):
        for width in range(1, 10):
            fraction = "123456789"[:width]
            stamp = "2026-09-30T08:00:00." + fraction + "Z"
            parsed = security.timestamp(stamp)
            expected = datetime(
                2026,
                9,
                30,
                8,
                0,
                0,
                int(fraction[:6].ljust(6, "0")),
                tzinfo=timezone.utc,
            ).timestamp()
            self.assertAlmostEqual(parsed, expected, places=6)
            self.assertEqual(
                security.normalize(dict(event(), last_seen=stamp))["last_seen"],
                "2026-09-30T08:00:00Z",
            )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        util.save_json(
            self.root / "status.json",
            {"running": True, "updated_at": now + ".123456789Z"},
        )
        status = self.reader().status()
        self.assertTrue(status["available"])
        self.assertEqual(status["updated_at"], now + "Z")

    def test_legacy_events_inside_one_second_keep_distinct_ids(self):
        value = event()
        value.pop("event_id")
        a = security.normalize(dict(value, last_seen="2026-09-30T08:00:00.123456789Z"))
        b = security.normalize(dict(value, last_seen="2026-09-30T08:00:00.234567891Z"))
        self.assertEqual(a["last_seen"], b["last_seen"])
        self.assertNotEqual(a["event_id"], b["event_id"])

    def test_daily_report_uses_previous_singapore_day_and_deduplicates(self):
        now = datetime(2026, 10, 1, 0, 0, 0, tzinfo=security.SG)
        calls = []

        def response(method, url, token, **kwargs):
            calls.append(url)
            return (
                {"nodes": []}
                if url.endswith("/v1/nodes")
                else {"report": {"counts": [], "events": []}}
            )

        with (
            patch.dict(
                os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}
            ),
            patch.object(hub, "datetime") as clock,
            patch.object(util, "http_json", side_effect=response),
            patch.object(hub.report, "send_telegram") as sender,
            patch("sys.stdout", new=io.StringIO()),
        ):
            clock.now.return_value = now
            hub.daily_report()
            hub.daily_report()
            self.assertEqual(sender.call_count, 1)
        saved = util.load_json(self.root / "daily-sent.json")
        self.assertEqual(saved["report_day"], "2026-09-30")
        self.assertEqual(saved["sent_at"], "2026-10-01T00:00:00+08:00")
        self.assertTrue(
            any(
                "start=" + str((now - timedelta(days=1)).timestamp()) in call
                for call in calls
            )
        )

    def test_daily_preview_does_not_send_or_mark_delivered(self):
        with (
            patch.dict(
                os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}
            ),
            patch.object(
                util,
                "http_json",
                side_effect=[{"nodes": []}, {"report": {"counts": [], "events": []}}],
            ),
            patch.object(hub.report, "send_telegram") as sender,
            patch("sys.stdout", new=io.StringIO()) as output,
        ):
            hub.daily_report(preview=True)
            sender.assert_not_called()
            self.assertTrue(output.getvalue())
        self.assertFalse((self.root / "daily-sent.json").exists())

    def test_schema_rejects_unknown_events(self):
        with self.assertRaises(ValueError):
            security.normalize(dict(event(), event="arbitrary"))
        with self.assertRaises(ValueError):
            security.normalize(dict(event(), event_id="bad"))
        value = event()
        value.pop("last_seen")
        with self.assertRaises(ValueError):
            security.normalize(value)

    def test_notification_worker_recovers_after_a_database_failure(self):
        instance = hub.Hub()
        instance.security_store.ingest("agent-a", [event()], {})
        pending = instance.security_store.pending
        responses = [sqlite3.OperationalError("temporary lock"), pending()]

        def send(*args):
            instance.stop.set()

        with (
            patch.object(instance.security_store, "pending", side_effect=responses),
            patch.object(instance.stop, "wait"),
            patch.object(hub.traceback, "print_exc"),
            patch.dict(
                os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}
            ),
            patch.object(hub.report, "send_telegram", side_effect=send) as sender,
        ):
            instance.security_notify_loop()
            self.assertEqual(sender.call_count, 1)
        self.assertIsNone(pending())

    def test_invalid_summary_range_returns_bad_request(self):
        instance = hub.Hub()
        handler = object.__new__(hub.HubHandler)
        handler.path = "/v1/security/summary?start=invalid"
        handler._auth = lambda: True
        result = []
        handler._send = lambda code, data: result.append((code, data))
        with patch.object(hub, "HUB", instance):
            handler.do_GET()
        self.assertEqual(result[0][0], 400)

    def test_malformed_security_callback_does_not_raise(self):
        self.assertEqual(bot.parse_callback("se:agent-a"), ("", []))
        self.assertEqual(bot.parse_callback("se:agent-a:invalid"), ("", []))
        self.assertEqual(
            bot.parse_callback("se:agent-a:" + "a" * 16),
            ("security_event", ["agent-a", "a" * 16]),
        )

    def test_agent_events_are_bound_to_identity(self):
        auth = self.root / "agents.json"
        util.save_json(auth, {"agents": {"agent-a": "agent-key"}})
        with patch.dict(os.environ, {"AGENT_AUTH_FILE": str(auth)}):
            instance = hub.Hub()
        instance.add_node("agent-a", None, 1, "eth0")
        instance.add_node("agent-b", None, 1, "eth0")

        def request(name):
            body = json.dumps(
                {"name": name, "events": [event()], "status": {}}
            ).encode()
            handler = object.__new__(hub.HubHandler)
            handler.path = "/v1/events"
            handler.headers = {
                "Authorization": "Bearer agent-key",
                "Content-Length": str(len(body)),
            }
            handler.rfile = io.BytesIO(body)
            result = []
            handler._send = lambda code, data: result.append((code, data))
            with patch.object(hub, "HUB", instance):
                handler.do_POST()
            return result[0]

        self.assertEqual(request("agent-b")[0], 403)
        good = request("agent-a")
        self.assertEqual(good[0], 200)
        self.assertEqual(good[1]["ack"], ["a" * 32])


if __name__ == "__main__":
    unittest.main()
