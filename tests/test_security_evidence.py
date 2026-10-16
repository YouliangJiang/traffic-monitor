import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import i18n
import security_events as security
import security_formatters as formatting
from test_security_events import event


class SecurityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.env = patch.dict(os.environ, {
            "TRAFFIC_MONITOR_STATE_DIR": str(self.root), "UI_LANG": "zh",
            "NODE_NAME": "local", "BILLING_RESET_DAY": "1",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def evidence(self):
        return dict(event(), response_differential_observed=False,
                    response_evidence_incomplete=True, response_capture_gap_observed=True,
                    response_detail_retained=True, response_detail_write_failed=False,
                    response_observation_window_ms=3000, response_samples_omitted=32,
                    response_comparison_groups_omitted=4, distinct_source_ips_observed=66,
                    source_ips_truncated=True)

    def test_evidence_survives_reader_store_and_both_languages(self):
        value = self.evidence()
        (self.root / "events.jsonl").write_text(json.dumps(value) + "\n")
        reader = security.EventReader(self.root / "events.jsonl", self.root / "status.json")
        reader.scan()
        store = security.EventStore()
        store.ingest("local", reader.batch(), {})
        stored = store.detail("local", value["event_id"])
        for key in set(value) - set(event()):
            self.assertEqual(stored.get(key), value[key], key)
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.notification("local", stored)
            self.assertIn(i18n.t("security.response.unconfirmed"), text)
            self.assertIn(i18n.t("security.response.incomplete"), text)
            self.assertIn("66", text)
            self.assertNotIn("security.response.", text)

    def test_new_metadata_does_not_change_legacy_identity_and_rejects_bad_types(self):
        original = event()
        original.pop("event_id")
        richer = self.evidence()
        richer.pop("event_id")
        self.assertEqual(security.normalize(original)["event_id"], security.normalize(richer)["event_id"])
        for key in ["response_differential_observed", "response_evidence_incomplete", "source_ips_truncated"]:
            with self.assertRaises(ValueError):
                security.normalize(dict(event(), **{key: "false"}))
        with self.assertRaises(ValueError):
            security.normalize(dict(event(), response_samples_omitted=True))

    def test_sensor_quality_is_forwarded_and_unavailable_drops_are_not_zero(self):
        stats = {"Packets": 10, "ProbeResponseCaptureGaps": 2,
                 "ProbeResponseDetailWriteFailures": 1, "ReplayHistoryEntries": 2048,
                 "ReplayHistoryEvictions": 4000, "ProbeResponseRecordDrops": 3}
        raw = {"running": True, "updated_at": "2026-10-13T00:00:00Z",
               "stats": stats, "capture_dropped": 0, "capture_stats_available": False,
               "monitored_server_names": ["current.example", "previous.example"],
               "limits": {"max_tracked_flows": 1024, "max_replay_entries": 32768,
                          "replay_window_seconds": 21600}}
        (self.root / "status.json").write_text(json.dumps(raw))
        status = security.EventReader(self.root / "events.jsonl", self.root / "status.json").status()
        self.assertIs(status.get("capture_stats_available"), False)
        for key, value in stats.items():
            self.assertEqual(status["stats"].get(key), value, key)
        self.assertEqual(status.get("limits"), raw["limits"])
        self.assertEqual(status.get("monitored_server_names"), raw["monitored_server_names"])
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.diagnostics([{"name": "local", "security": status}])
            self.assertIn(i18n.t("security.capture_unknown"), text)
            self.assertIn("32768", text)
            self.assertIn("previous.example", text)
            self.assertNotIn("security.sensor.", text)

    def campaign(self, ended=False):
        return dict(
            event("c" * 32, "medium"), event="distributed_syn_without_clienthello_burst",
            evidence=["many_sources_open_connections_without_clienthello",
                      "campaign_ended" if ended else "campaign_active"],
            distinct_flows=48210, syn_without_clienthello_flows=47990,
            window_connections=912, window_sources=163, distinct_source_ips_observed=611,
            source_ips_truncated=True, probe_source_ips=["198.51.100.%d" % i for i in range(16)],
            connection_outcomes={"handshake_then_server_fin": 30111, "handshake_then_client_rst": 9000,
                                 "synack_without_client_ack": 8000, "future_outcome": 3},
            syn_fingerprints={"ttl128/win64240/mss1440/ws8/MNWNNS": 40000, "other": 900},
            syn_ttl_values={"113": 30000, "49": 9000},
            connection_samples=[{"source_ip": "198.51.100.1", "outcome": "handshake_then_server_fin"}],
        )

    def test_campaign_and_sni_summaries_survive_reader_store_and_both_languages(self):
        sni = dict(event("d" * 32, "low"), event="tls_unexpected_sni", confidence_kind="observation_only",
                   evidence=["clienthello_sni_outside_configured_reality_names"], distinct_flows=9,
                   sni_observations=[{"sni": "lookalike-cover.example", "hellos": 7, "sources": 2, "tls13_hellos": 7},
                                     {"sni": "<b>x</b>", "hellos": 2, "sources": 1, "tls13_hellos": 0}],
                   sni_names_omitted=3)
        (self.root / "events.jsonl").write_text(json.dumps(self.campaign()) + "\n" + json.dumps(sni) + "\n")
        reader = security.EventReader(self.root / "events.jsonl", self.root / "status.json")
        reader.scan()
        store = security.EventStore()
        store.ingest("local", reader.batch(), {})
        stored = store.detail("local", "c" * 32)
        for key in ["connection_outcomes", "syn_fingerprints", "syn_ttl_values", "window_connections", "window_sources"]:
            self.assertEqual(stored[key], self.campaign()[key], key)
        self.assertNotIn("connection_samples", stored)
        stored_sni = store.detail("local", "d" * 32)
        self.assertEqual(stored_sni["sni_observations"], sni["sni_observations"])
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.notification("local", stored)
            self.assertIn(i18n.t("security.behavior.distributed"), text)
            self.assertIn(i18n.t("security.campaign.active"), text)
            self.assertIn(i18n.t("security.outcome.handshake_then_server_fin") + " 30111", text)
            self.assertIn("ttl128/win64240/mss1440/ws8/MNWNNS", text)
            self.assertIn("611", text)
            self.assertIn("912", text)
            text = formatting.notification("local", stored_sni)
            self.assertIn(i18n.t("security.behavior.sni"), text)
            self.assertIn("lookalike-cover.example", text)
            self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
            self.assertNotIn("<b>x</b>", text)
            for rendered in [text, formatting.notification("local", security.normalize(self.campaign(True)))]:
                self.assertNotIn("security.", rendered)
            self.assertIn(i18n.t("security.campaign.ended"),
                          formatting.notification("local", security.normalize(self.campaign(True))))

    def test_escalated_campaign_notifies_once_with_reason(self):
        escalated = dict(self.campaign(), severity="high", window_ms=720000,
                         evidence=["many_sources_open_connections_without_clienthello", "campaign_active",
                                   "sustained_campaign_escalated"])
        update = dict(self.campaign(), event_id="e" * 32)
        store = security.EventStore()
        store.ingest("local", [security.normalize(escalated), security.normalize(update)], {})
        pending = store.pending()
        self.assertEqual(pending["id"], "c" * 32)
        store.notified("local", pending["id"], True)
        self.assertIsNone(store.pending())
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.notification("local", security.normalize(escalated))
            self.assertIn(i18n.t("security.campaign.escalated", minutes=12, sources=611), text)
            self.assertIn(i18n.t("security.signal.high"), text)
            self.assertNotIn("security.", text)

    def test_summary_shapes_are_validated(self):
        bad = [
            {"connection_outcomes": {"x": "1"}},
            {"connection_outcomes": {str(i): 1 for i in range(17)}},
            {"syn_fingerprints": ["ttl64"]},
            {"syn_ttl_values": {"": 1}},
            {"sni_observations": [{"sni": "a.example", "hellos": 1, "sources": 1}]},
            {"sni_observations": [{"sni": "a.example", "hellos": True, "sources": 1, "tls13_hellos": 0}]},
            {"sni_observations": [{"sni": "", "hellos": 1, "sources": 1, "tls13_hellos": 0}]},
            {"window_connections": -1},
        ]
        for extra in bad:
            with self.assertRaises(ValueError, msg=str(extra)):
                security.normalize(dict(self.campaign(), **extra))

    def test_observer_counters_reach_diagnostics(self):
        stats = {"Packets": 10, "MidstreamPacketsIgnored": 52311, "EmptyConnectionsFinalized": 48001,
                 "EmptyConnectionEvictions": 17, "DistributedSYNCampaigns": 2,
                 "ActiveDistributedSYNCampaigns": 1, "UnexpectedSNIHellos": 77}
        raw = {"running": True, "updated_at": "2026-10-15T00:00:00Z", "stats": stats}
        (self.root / "status.json").write_text(json.dumps(raw))
        status = security.EventReader(self.root / "events.jsonl", self.root / "status.json").status()
        self.assertEqual(status["stats"], stats)
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.diagnostics([{"name": "local", "security": status}])
            for value in ["52311", "48001", "17", "77"]:
                self.assertIn(value, text)
            self.assertNotIn("security.sensor.", text)

    def attribution_events(self):
        relay = dict(event("1" * 32, "medium"), event="tls_unexpected_sni", confidence_kind="heuristic_score",
                     evidence=["clienthello_sni_outside_configured_reality_names", "third_party_traffic_relayed_to_reality_target"],
                     distinct_flows=3, relayed_client_bytes=1_200_000, relayed_server_bytes=88_000_000,
                     sni_observations=[{"sni": "node.example", "hellos": 3, "sources": 2, "tls13_hellos": 3,
                                        "client_bytes": 1_200_000, "server_bytes": 88_000_000},
                                       {"sni": "scan.example", "hellos": 1, "sources": 1, "tls13_hellos": 1}])
        persistent = dict(event("2" * 32, "medium"), event="tls_unexpected_sni_persistent_source",
                          first_seen="2026-09-29T08:00:00Z", window_ms=86_400_000, windows_observed=288,
                          evidence=["same_source_repeats_unexpected_sni_across_windows"], distinct_flows=2016,
                          probe_source_ips=["188.253.112.225"],
                          sni_observations=[{"sni": "www.cloudflare-cn.com", "hellos": 2016, "sources": 1, "tls13_hellos": 2016}])
        unauth = dict(event("3" * 32, "medium"), event="reality_sni_unauthenticated",
                      evidence=["clienthello_uses_reality_server_name", "source_not_authenticated_by_xray"],
                      distinct_flows=4, distinct_source_ips_observed=3,
                      sni_observations=[{"sni": "www.cloudflare.com", "hellos": 4, "sources": 3, "tls13_hellos": 4}])
        interference = dict(event("4" * 32, "medium"), event="client_connection_interference",
                            evidence=["known_client_connections_without_clienthello"], distinct_flows=103,
                            syn_without_clienthello_flows=103, probe_source_ips=["120.229.67.54"],
                            connection_outcomes={"handshake_then_idle": 12})
        return relay, persistent, unauth, interference

    def test_attribution_and_relay_events_survive_reader_store_and_both_languages(self):
        values = self.attribution_events()
        (self.root / "events.jsonl").write_text("".join(json.dumps(value) + "\n" for value in values))
        reader = security.EventReader(self.root / "events.jsonl", self.root / "status.json")
        reader.scan()
        store = security.EventStore()
        store.ingest("local", reader.batch(), {})
        stored = [store.detail("local", value["event_id"]) for value in values]
        for value, kept in zip(values, stored):
            for key in ["event", "sni_observations", "relayed_client_bytes", "relayed_server_bytes", "windows_observed"]:
                self.assertEqual(kept.get(key), value.get(key), key)
        relay, persistent, unauth, interference = stored
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.notification("local", relay)
            self.assertIn(i18n.t("security.evidence.relay"), text)
            self.assertIn(i18n.t("security.relay", up="1.20 MB", down="88.00 MB"), text)
            self.assertIn(i18n.t("security.sni.item_bytes", name="node.example", hellos=3, sources=2, bytes="89.20 MB"), text)
            self.assertIn(i18n.t("security.sni.item", name="scan.example", hellos=1, sources=1), text)
            text = formatting.notification("local", persistent)
            self.assertIn(i18n.t("security.behavior.persistent"), text)
            self.assertIn(i18n.t("security.persistent.windows", windows=288, hours=24.0), text)
            self.assertIn("www.cloudflare-cn.com", text)
            self.assertIn(i18n.t("security.behavior.unauth"), formatting.notification("local", unauth))
            text = formatting.notification("local", interference)
            self.assertIn(i18n.t("security.behavior.interference"), text)
            self.assertIn(i18n.t("security.interference.hint"), text)
            for value in stored:
                self.assertNotIn("security.", formatting.notification("local", value))

    def test_relay_shapes_are_validated(self):
        relay = self.attribution_events()[0]
        item = relay["sni_observations"][0]
        for extra in [
            {"relayed_server_bytes": -1},
            {"relayed_client_bytes": True},
            {"relayed_server_bytes": 10**16},
            {"windows_observed": "288"},
            {"sni_observations": [dict(item, server_bytes="1")]},
            {"sni_observations": [dict(item, unknown=1)]},
        ]:
            with self.assertRaises(ValueError, msg=str(extra)):
                security.normalize(dict(relay, **extra))

    def test_client_attribution_reaches_diagnostics(self):
        stats = {"Packets": 10, "KnownClients": 3, "UnauthenticatedRealityHellos": 41,
                 "KnownClientSignalsSuppressed": 192, "PersistentSNISources": 7}
        raw = {"running": True, "updated_at": "2026-10-16T00:00:00Z", "stats": stats,
               "client_attribution": {"enabled": True, "available": False, "known_clients": 3,
                                      "accepted_lines": 9, "error": "open /opt/xray/data/access.log: permission denied"}}
        (self.root / "status.json").write_text(json.dumps(raw))
        status = security.EventReader(self.root / "events.jsonl", self.root / "status.json").status()
        self.assertEqual(status["stats"], stats)
        self.assertEqual(status["client_attribution"]["available"], False)
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.diagnostics([{"name": "local", "security": status}])
            self.assertIn(i18n.t("security.sensor.clients_unavailable"), text)
            for value in ["41", "192"]:
                self.assertIn(value, text)
            self.assertNotIn("security.sensor.", text)

    def test_old_response_flags_do_not_claim_matched_probe_evidence(self):
        value = dict(event(), response_differential_observed=True)
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.notification("local", security.normalize(value))
            self.assertIn(i18n.t("security.response.legacy"), text)
            self.assertNotIn(i18n.t("security.response.confirmed"), text)
