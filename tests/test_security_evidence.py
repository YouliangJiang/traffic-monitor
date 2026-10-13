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

    def test_old_response_flags_do_not_claim_matched_probe_evidence(self):
        value = dict(event(), response_differential_observed=True)
        for code in ["zh", "en"]:
            i18n.set_lang(code)
            text = formatting.notification("local", security.normalize(value))
            self.assertIn(i18n.t("security.response.legacy"), text)
            self.assertNotIn(i18n.t("security.response.confirmed"), text)
