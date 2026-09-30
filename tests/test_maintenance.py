import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import ip_enrichment
import maintenance
import security_events


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"TRAFFIC_MONITOR_STATE_DIR": str(self.root)})
        self.env.start()
        self.now = time.time()
        self.old = self.now - 31 * 86400
        self.cutoff = self.now - 30 * 86400

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_idle_cleanup_preserves_thirty_day_boundary_and_billing_history(self):
        security_events.EventStore()
        security_events.EventReader(self.root / "events.jsonl")
        ip_enrichment.IPContext()
        with security_events.database("security-events.sqlite3") as db:
            for identity, observed, received in [
                ("old", self.old, self.now), ("recent", self.now, self.now),
                ("boundary", self.cutoff, self.cutoff),
            ]:
                db.execute("INSERT INTO events(node,id,observed,received,kind,severity,data,notification) VALUES('node',?,?,?,'probe','high','{}','pending')",
                           (identity, observed, received))
        with security_events.database("security-outbox.sqlite3") as db:
            db.execute("INSERT INTO outbox VALUES('old','{}',?)", (self.old,))
            db.execute("INSERT INTO outbox VALUES('fresh','{}',?)", (self.now,))
            db.execute("INSERT INTO cursors VALUES('old',0,'old',?,0)", (self.old,))
        with security_events.database("ip-context.sqlite3") as db:
            db.execute("INSERT INTO cache(ip,data,expires,used) VALUES('old','{}',?,?)", (self.old, self.old))
            db.execute("INSERT INTO cache(ip,data,expires,used) VALUES('fresh','{}',?,?)", (self.now, self.now))
        ledger = self.root / "traffic.sqlite3"
        ledger.write_bytes(b"billing history must survive")
        backups = self.root / "integration-replay-backups"
        backups.mkdir()
        old_backup = backups / "before-seconds-old.sqlite3"
        old_backup.touch()
        os.utime(old_backup, (self.old, self.old))
        other = backups / "unrelated.sqlite3"
        other.touch()
        os.utime(other, (self.old, self.old))
        result = maintenance.run(self.root, self.now)
        with sqlite3.connect(self.root / "security-events.sqlite3") as db:
            self.assertEqual(set(row[0] for row in db.execute("SELECT id FROM events")), {"recent", "boundary"})
        with sqlite3.connect(self.root / "security-outbox.sqlite3") as db:
            self.assertEqual(db.execute("SELECT id FROM outbox").fetchone()[0], "fresh")
            self.assertEqual(db.execute("SELECT value FROM reader_state WHERE key='retention_expired'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 0)
        self.assertEqual(result["databases"]["ip-context.sqlite3"]["cache"], 1)
        self.assertEqual(ledger.read_bytes(), b"billing history must survive")
        self.assertFalse(old_backup.exists())
        self.assertTrue(other.exists())
        self.assertEqual(json.loads((self.root / "maintenance-status.json").read_text())["errors"], {})
        self.assertEqual(maintenance.run(self.root, self.now)["databases"]["security-events.sqlite3"]["events"], 0)

    def test_symlink_database_is_rejected_without_modifying_its_target(self):
        target = self.root / "unrelated.sqlite3"
        target.write_bytes(b"untouched")
        (self.root / "security-events.sqlite3").symlink_to(target)
        with self.assertRaises(RuntimeError):
            maintenance.run(self.root, self.now)
        self.assertEqual(target.read_bytes(), b"untouched")
        self.assertEqual(json.loads((self.root / "maintenance-status.json").read_text())["errors"], {"security-events.sqlite3": "ValueError"})

    def test_deleted_history_returns_meaningful_disk_space(self):
        path = self.root / "security-events.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE events(observed REAL,received REAL,data TEXT)")
            db.executemany("INSERT INTO events VALUES(?,?,?)", ((self.old, self.old, "x" * 16384) for _ in range(1024)))
            db.execute("INSERT INTO events VALUES(?,?,?)", (self.now, self.now, "fresh"))
        before = path.stat().st_size
        result = maintenance.run(self.root, self.now)
        self.assertTrue(result["databases"][path.name]["vacuumed"])
        self.assertLess(path.stat().st_size, before // 10)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT data FROM events").fetchone()[0], "fresh")
