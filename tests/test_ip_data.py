import csv
import gzip
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import ip_data
import ip_enrichment
import update_ip_data as update


class LocalIPDataTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.env = patch.dict(
            os.environ,
            {
                "IP_DATA_DIR": str(self.root),
                "IP_CONTEXT_ONLINE": "0",
                "TRAFFIC_MONITOR_STATE_DIR": str(self.root),
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def csv(self, kind, rows):
        path = self.root / (kind + ".csv.gz")
        with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows(rows)
        return path

    def build(self, country="US", asn=14618, org="Amazon Data Services"):
        path = self.root / "new.sqlite3"
        with sqlite3.connect(path) as db:
            update.create_schema(db)
            update.import_ranges(
                db,
                self.csv(
                    "country",
                    [
                        ["1.1.1.0", "1.1.1.255", country],
                        [
                            "2001:4860::",
                            "2001:4860:ffff:ffff:ffff:ffff:ffff:ffff",
                            "US",
                        ],
                    ],
                ),
                "country",
            )
            update.import_ranges(
                db,
                self.csv(
                    "asn",
                    [
                        ["1.1.1.0", "1.1.1.255", str(asn), org],
                        [
                            "2001:4860::",
                            "2001:4860:ffff:ffff:ffff:ffff:ffff:ffff",
                            "15169",
                            "Google LLC",
                        ],
                    ],
                ),
                "asn",
            )
            db.execute("INSERT INTO metadata VALUES('month','2026-09')")
            db.commit()
        os.replace(path, self.root / "geo.sqlite3")

    def test_local_country_asn_and_ipv6_lookup_do_not_need_an_api(self):
        self.build()
        with (
            patch.object(ip_enrichment, "fetch_json") as external,
            patch.object(ip_enrichment, "reverse_name", return_value=("", False)),
        ):
            value = ip_enrichment.lookup("1.1.1.1", allow_geo=False)
        external.assert_not_called()
        self.assertEqual(value["country_code"], "US")
        self.assertEqual(value["asn"], 14618)
        self.assertEqual(value["provider"], "DB-IP Lite")
        self.assertEqual(value["network_type"], "hosting")
        self.assertEqual(ip_data.lookup("2001:4860::8888")["asn"], 15169)

    def test_gaps_do_not_inherit_the_preceding_range(self):
        self.build()
        self.assertEqual(ip_data.lookup("1.1.2.1"), {})

    def test_database_replacement_is_detected_without_a_process_restart(self):
        self.build()
        old = ip_data.version()
        self.build(country="SG", asn=123, org="New network")
        self.assertNotEqual(ip_data.version(), old)
        self.assertEqual(ip_data.lookup("1.1.1.1")["country_code"], "SG")

    def test_classifier_rules_are_external_and_can_change_without_code_changes(self):
        self.build()
        config = json.loads(ip_data.bundled_rules().read_text())
        config["scanner_domains"] = {"custom.example": "Custom scanner"}
        config["hosting_names"] = ["My hosting company"]
        config["scanner_networks"] = [
            {"cidr": "8.8.8.0/24", "service": "Published scanner"}
        ]
        (self.root / "rules.json").write_text(json.dumps(config))
        self.assertEqual(
            ip_data.classify({}, "1.1.1.1", "scanner.custom.example", True)["scanner"],
            "Custom scanner",
        )
        self.assertEqual(
            ip_data.classify({}, "8.8.8.8")["scanner"], "Published scanner"
        )
        self.assertEqual(
            ip_data.classify({"org": "My hosting company"}, "1.1.1.1")["network_type"],
            "hosting",
        )

    def test_range_import_rejects_overlaps_and_invalid_data(self):
        with sqlite3.connect(":memory:") as db:
            update.create_schema(db)
            path = self.csv(
                "bad", [["1.1.1.0", "1.1.1.20", "US"], ["1.1.1.10", "1.1.1.30", "US"]]
            )
            with self.assertRaises(ValueError):
                update.import_ranges(db, path, "country")

    def test_failed_update_does_not_remove_the_working_database(self):
        self.build()
        before = (self.root / "geo.sqlite3").read_bytes()
        with patch.object(update, "download", side_effect=OSError("offline")):
            # A new month triggers a real update attempt without touching old data.
            with patch.object(update, "current_month", return_value="2026-08"):
                with self.assertRaises(OSError):
                    update.update(self.root)
        self.assertEqual((self.root / "geo.sqlite3").read_bytes(), before)

    def test_new_data_invalidates_cached_dns_only_metadata(self):
        cache = ip_enrichment.IPContext()
        cache.get("1.1.1.1")
        with patch.object(
            ip_enrichment,
            "lookup",
            return_value={"status": "partial", "hostname": "host.example"},
        ):
            cache.work_one()
        self.build()
        cache.get("1.1.1.1")
        with patch.object(ip_enrichment, "reverse_name", return_value=("", False)):
            self.assertTrue(cache.work_one())
        self.assertEqual(cache.get("1.1.1.1")["asn"], 14618)

    def test_initial_database_also_refreshes_prior_lookup_failures(self):
        cache = ip_enrichment.IPContext()
        cache.get("1.1.1.1")
        with patch.object(
            ip_enrichment,
            "lookup",
            return_value={"status": "unavailable", "data_version": ""},
        ):
            cache.work_one()
        self.build()
        cache.get("1.1.1.1")
        with patch.object(ip_enrichment, "reverse_name", return_value=("", False)):
            self.assertTrue(cache.work_one())
        self.assertEqual(cache.get("1.1.1.1")["country_code"], "US")


if __name__ == "__main__":
    unittest.main()
