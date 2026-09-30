import ipaddress
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ip_enrichment as context
import security_events as security
import security_formatters as formatting


def ready():
    return {
        "status": "ready",
        "country": "United States",
        "country_code": "US",
        "region": "Virginia",
        "city": "Ashburn",
        "asn": 14618,
        "org": "Amazon Data Services",
        "isp": "Amazon.com",
        "hostname": "scanner.example.net",
        "forward_verified": True,
        "network_type": "hosting",
        "checked_at": 1,
    }


class IPContextTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "TRAFFIC_MONITOR_STATE_DIR": self.folder.name,
                "IP_DATA_DIR": str(Path(self.folder.name) / "geo"),
                "UI_LANG": "zh",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_reports_only_enqueue_and_never_perform_a_network_lookup(self):
        cache = context.IPContext()
        with patch.object(context, "lookup") as lookup:
            value = cache.get("1.1.1.1")
            self.assertEqual(value["status"], "pending")
            self.assertEqual(cache.get("1.1.1.1")["status"], "pending")
            lookup.assert_not_called()

    def test_worker_caches_and_reuses_the_result_across_restarts(self):
        cache = context.IPContext()
        cache.get("1.1.1.1")
        with patch.object(context, "lookup", return_value=ready()) as lookup:
            self.assertTrue(cache.work_one())
            self.assertEqual(context.IPContext().get("1.1.1.1")["asn"], 14618)
            self.assertFalse(context.IPContext().work_one())
            self.assertEqual(lookup.call_count, 1)

    def test_non_public_and_invalid_addresses_are_never_queried(self):
        cache = context.IPContext()
        for value in [
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "::1",
            "ff02::1",
            "198.51.100.5",
            "2001:db8::1",
            "bad/../../metadata",
            "2001:4860::1%eth0",
        ]:
            self.assertIn(cache.get(value)["status"], {"non_public", "invalid"})
        with patch.object(context, "lookup") as lookup:
            self.assertFalse(cache.work_one())
            lookup.assert_not_called()

    def test_ipv6_and_ipv4_mapped_addresses_are_canonicalized(self):
        cache = context.IPContext()
        cache.get("::ffff:1.1.1.1")
        cache.get("2001:4860:4860:0:0:0:0:8888")
        with context.database() as db:
            values = {row["ip"] for row in db.execute("SELECT ip FROM cache")}
        self.assertEqual(values, {"1.1.1.1", "2001:4860:4860::8888"})

    def test_cache_and_pending_queue_have_hard_bounds(self):
        with (
            patch.object(context, "MAX_CACHE", 3),
            patch.object(context, "MAX_PENDING", 2),
        ):
            cache = context.IPContext()
            for value in ["1.1.1.1", "1.1.1.2", "1.1.1.3", "1.1.1.4"]:
                cache.get(value)
            with context.database() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 2
                )
            with patch.object(context, "lookup", return_value=ready()):
                cache.work_one()
                cache.work_one()
            cache.get("1.1.1.3")
            cache.get("1.1.1.4")
            with context.database() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 3
                )
                self.assertLessEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM cache WHERE queued IS NOT NULL"
                    ).fetchone()[0],
                    2,
                )

    def test_failed_lookups_retry_later_without_erasing_cached_geo(self):
        cache = context.IPContext()
        cache.get("1.1.1.1")
        with patch.object(context, "lookup", return_value=ready()):
            cache.work_one()
        future = time.time() + context.CACHE_TTL + 1
        with patch.object(context.time, "time", return_value=future):
            self.assertTrue(cache.get("1.1.1.1")["stale"])
            with patch.object(
                context, "lookup", return_value={"status": "unavailable"}
            ):
                cache.work_one()
            value = cache.get("1.1.1.1")
            self.assertEqual(value["asn"], 14618)
            self.assertTrue(value["stale"])
            self.assertFalse(cache.work_one())

    def test_high_risk_sources_are_looked_up_first_and_budget_is_persistent(self):
        cache = context.IPContext()
        cache.get("1.1.1.1", priority=1)
        cache.get("8.8.8.8", priority=0)
        with (
            patch.dict(os.environ, {"IP_CONTEXT_ONLINE": "1"}),
            patch.object(context, "MAX_DAILY", 1),
            patch.object(context, "lookup", return_value=ready()) as lookup,
        ):
            cache.work_one()
            cache.work_one()
            self.assertEqual(lookup.call_args_list[0].args[0], "8.8.8.8")
            self.assertTrue(lookup.call_args_list[0].kwargs["allow_geo"])
            self.assertFalse(lookup.call_args_list[1].kwargs["allow_geo"])

    def test_ptr_without_forward_confirmation_does_not_claim_a_scanner(self):
        self.assertEqual(context.scanner_hint("scanner.reposify.net", False), "")
        self.assertEqual(
            context.scanner_hint("scanner.reposify.net.attacker.example", True), ""
        )
        self.assertEqual(context.scanner_hint("scanner.reposify.net", True), "Reposify")
        with patch.object(
            context,
            "dns_records",
            side_effect=[
                [{"type": 12, "data": "scanner.reposify.net."}],
                [{"type": 1, "data": "8.8.8.8"}],
            ],
        ):
            hostname, verified = context.reverse_name(ipaddress.ip_address("1.1.1.1"))
        self.assertEqual(hostname, "scanner.reposify.net")
        self.assertFalse(verified)

    def test_geo_failure_can_still_provide_verified_dns_context(self):
        with (
            patch.object(context, "fetch_json", side_effect=TimeoutError),
            patch.object(
                context, "reverse_name", return_value=("scanner.reposify.net", True)
            ),
        ):
            value = context.lookup("1.1.1.1")
        self.assertEqual(value["status"], "partial")
        self.assertEqual(value["scanner"], "Reposify")
        self.assertNotIn("country", value)

    def test_provider_result_must_match_the_requested_ip(self):
        wrong = {
            "ip": "8.8.8.8",
            "success": True,
            "country": "Fake country",
            "connection": {"asn": 123},
        }
        with (
            patch.object(context, "fetch_json", return_value=wrong),
            patch.object(context, "reverse_name", return_value=("", False)),
        ):
            value = context.lookup("1.1.1.1")
        self.assertEqual(value["status"], "unavailable")
        self.assertNotIn("country", value)

    def test_cloud_inference_uses_provider_org_without_changing_event_risk(self):
        raw = {
            "ip": "1.1.1.1",
            "success": True,
            "country": "United States",
            "country_code": "US",
            "connection": {
                "asn": 14618,
                "org": "Amazon Data Services",
                "isp": "Amazon.com",
            },
        }
        with (
            patch.object(context, "fetch_json", return_value=raw),
            patch.object(context, "reverse_name", return_value=("", False)),
        ):
            value = context.lookup("1.1.1.1")
        self.assertEqual(value["network_type"], "hosting")
        event = {
            "event_id": "a" * 32,
            "event": "non_tls_connection_burst",
            "severity": "medium",
            "last_seen": "2026-09-30T10:00:00Z",
            "target_port": 443,
            "probe_source_ips": ["1.1.1.1"],
            "source_info": {"1.1.1.1": value},
        }
        text = formatting.notification("agent-a", event)
        self.assertIn("美国", text)
        self.assertIn("AS14618", text)
        self.assertIn("推测", text)
        self.assertIn("可疑异常", text)
        self.assertNotIn("高危探测信号", text)

    def test_external_metadata_is_escaped_and_the_message_remains_bounded(self):
        ip = "1.1.1.1"
        value = dict(ready(), org="<script>", hostname="<ptr>", scanner="<name>")
        event = {
            "event_id": "a" * 32,
            "event": "reality_replay_differential_probe",
            "severity": "high",
            "last_seen": "2026-09-30T10:00:00Z",
            "target_port": 443,
            "probe_source_ips": [ip],
            "source_info": {ip: value},
            "evidence": ["exact_clienthello_replay"],
        }
        text = formatting.notification("agent-a", event)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;ptr&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertLess(len(text), 4096)

    def test_enrichment_does_not_accept_claims_from_agent_payloads(self):
        event = {
            "event_id": "a" * 32,
            "event": "non_tls_connection_burst",
            "severity": "medium",
            "last_seen": "2026-09-30T10:00:00Z",
            "target_port": 443,
            "probe_source_ips": ["1.1.1.1"],
            "source_info": {"1.1.1.1": {"scanner": "Forged"}},
        }
        self.assertNotIn("source_info", security.normalize(event))


if __name__ == "__main__":
    unittest.main()
