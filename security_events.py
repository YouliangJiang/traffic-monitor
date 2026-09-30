"""Durable, bounded security-event ingestion independent of traffic accounting."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import util
import ip_enrichment

MAX_EVENT_BYTES = 16384
MAX_BATCH_BYTES = 65536
MAX_BATCH = 16
MAX_OUTBOX = 4096
PROVIDER = "xray_honeypot"
KINDS = {
    "reality_replay_differential_probe",
    "tls_clienthello_delayed_replay",
    "unclassified_first_flight",
    "non_tls_connection_burst",
    "tcp_syn_without_clienthello_burst",
}
SG = timezone(timedelta(hours=8), "Asia/Singapore")


def enabled():
    return util.env_opt("SECURITY_PROVIDER") == PROVIDER


def timestamp(text):
    if not isinstance(text, str) or len(text) > 64:
        raise ValueError("invalid event timestamp")
    # Go's old RFC3339Nano output has 1..9 fractional digits. Python 3.9
    # accepts only certain widths, so pad/truncate legacy fractions first.
    compatible = re.sub(
        r"\.(\d{1,9})(?=Z$|[+-]\d{2}:\d{2}$)",
        lambda match: "." + match.group(1)[:6].ljust(6, "0"),
        text,
    )
    parsed = datetime.fromisoformat(compatible.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("event timestamp requires timezone")
    return parsed.timestamp()


def normalize(value):
    if (
        not isinstance(value, dict)
        or value.get("event") not in KINDS
        or value.get("severity") not in {"low", "medium", "high"}
    ):
        raise ValueError("invalid security event")
    result = {
        key: value[key]
        for key in [
            "event",
            "severity",
            "first_seen",
            "last_seen",
            "target_ip",
            "target_port",
            "sni",
            "score",
            "confidence_kind",
            "distinct_flows",
            "session_id_variants",
            "malformed_record_count",
            "normalized_clienthello_hash",
            "exact_clienthello_hash",
            "evidence",
            "probe_source_ips",
            "payload_class",
            "window_ms",
            "replay_delay_ms",
            "syn_without_clienthello_flows",
            "detector_version",
        ]
        if key in value
    }
    timestamp(result.get("last_seen"))
    if "first_seen" in result:
        timestamp(result["first_seen"])
    for key in [
        "score",
        "distinct_flows",
        "session_id_variants",
        "malformed_record_count",
        "window_ms",
        "replay_delay_ms",
        "syn_without_clienthello_flows",
    ]:
        if key in result and (
            type(result[key]) is not int or not 0 <= result[key] <= 10**12
        ):
            raise ValueError("invalid event metric")
    port = result.get("target_port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid event port")
    for key in [
        "target_ip",
        "sni",
        "confidence_kind",
        "normalized_clienthello_hash",
        "exact_clienthello_hash",
        "detector_version",
        "payload_class",
    ]:
        if key in result and (
            not isinstance(result[key], str) or len(result[key]) > 512
        ):
            raise ValueError("invalid event text")
    for key in ["evidence", "probe_source_ips"]:
        values = result.get(key, [])
        if (
            not isinstance(values, list)
            or len(values) > 32
            or any(not isinstance(x, str) or len(x) > 512 for x in values)
        ):
            raise ValueError("invalid event evidence")
    identity = value.get("event_id")
    if identity is None:
        # Preserve the legacy fingerprint when adding optional display fields.
        identity_fields = {
            key: item
            for key, item in result.items()
            if key
            not in {
                "payload_class",
                "window_ms",
                "replay_delay_ms",
                "syn_without_clienthello_flows",
            }
        }
        identity = hashlib.sha256(
            json.dumps(identity_fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    if not isinstance(identity, str) or not re.fullmatch("[a-f0-9]{32,64}", identity):
        raise ValueError("invalid event identity")
    # Compute legacy identity before reducing precision: separate observations
    # inside one second must keep separate IDs. The wire representation is UTC
    # RFC3339 at second precision, including when replaying older logs.
    for key in ["first_seen", "last_seen"]:
        if key in result:
            result[key] = (
                datetime.fromtimestamp(timestamp(result[key]), timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
    result.update(
        event_id=identity,
        schema_version=1,
        provider=PROVIDER,
        backfill=value.get("backfill") is True,
    )
    if len(json.dumps(result).encode()) > MAX_EVENT_BYTES:
        raise ValueError("security event too large")
    return result


@contextmanager
def database(name):
    path = util.state_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise util.StateError("security database must not be a symlink")
    db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA cache_size=-1024")
        yield db
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


class EventReader:
    """Checkpoint JSONL positions and queue events in one transaction."""

    def __init__(self, path=None, status_path=None):
        self.path = Path(
            path
            or util.env_opt("SECURITY_EVENT_LOG", "/var/log/xray-honeypot/events.jsonl")
        )
        self.status_path = Path(
            status_path
            or util.env_opt(
                "SECURITY_STATUS_FILE", "/var/log/xray-honeypot/status.json"
            )
        )
        with database("security-outbox.sqlite3") as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cursors(file_id TEXT PRIMARY KEY, offset INTEGER NOT NULL, path TEXT, seen REAL NOT NULL, initial_end INTEGER NOT NULL DEFAULT 0)"
            )
            if "initial_end" not in {
                row["name"] for row in db.execute("PRAGMA table_info(cursors)")
            }:
                db.execute(
                    "ALTER TABLE cursors ADD COLUMN initial_end INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reader_state(key TEXT PRIMARY KEY,value INTEGER NOT NULL)"
            )

    def scan(self):
        files = (list(self.path.parent.glob(self.path.name + ".[0-9]*"))
                 + list(self.path.parent.glob(self.path.name + "-[0-9]*")) + [self.path])
        files = [
            p
            for p in files
            if p.is_file() and not p.is_symlink() and not p.name.endswith(".gz")
        ]
        files.sort(key=lambda p: p.stat().st_mtime_ns)
        remaining = 128
        with database("security-outbox.sqlite3") as db:
            for path in files:
                if remaining <= 0:
                    break
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    key = str(metadata.st_dev) + ":" + str(metadata.st_ino)
                    db.execute("BEGIN IMMEDIATE")
                    previous = db.execute(
                        "SELECT offset,initial_end FROM cursors WHERE file_id=?", (key,)
                    ).fetchone()
                    offset = (
                        previous["offset"]
                        if previous and previous["offset"] <= metadata.st_size
                        else 0
                    )
                    initial_end = (
                        previous["initial_end"]
                        if previous
                        else (
                            metadata.st_size
                            if not db.execute(
                                "SELECT 1 FROM reader_state WHERE key='initialized'"
                            ).fetchone()
                            else 0
                        )
                    )
                    stream.seek(offset)
                    while remaining > 0:
                        position = stream.tell()
                        line = stream.readline(MAX_EVENT_BYTES + 1)
                        if not line or not line.endswith(b"\n"):
                            if len(line) > MAX_EVENT_BYTES:
                                while line and not line.endswith(b"\n"):
                                    line = stream.readline(MAX_EVENT_BYTES + 1)
                                offset = stream.tell()
                                self._count(db, "malformed")
                                remaining -= 1
                                continue
                            stream.seek(position)
                            break
                        try:
                            event = normalize(json.loads(line))
                            event["backfill"] = position < initial_end
                        except (ValueError, KeyError, UnicodeError):
                            self._count(db, "malformed")
                            offset = stream.tell()
                            remaining -= 1
                            continue
                        count = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                        if count >= MAX_OUTBOX:
                            # Backpressure: leave the file position intact until delivery resumes.
                            stream.seek(position)
                            self._count(db, "queue_full")
                            break
                        db.execute(
                            "INSERT OR IGNORE INTO outbox VALUES(?,?,?)",
                            (
                                event["event_id"],
                                json.dumps(event, separators=(",", ":")),
                                time.time(),
                            ),
                        )
                        offset = stream.tell()
                        remaining -= 1
                    db.execute(
                        "INSERT OR REPLACE INTO cursors VALUES(?,?,?,?,?)",
                        (key, offset, str(path), time.time(), initial_end),
                    )
                    db.execute("COMMIT")
            if files:
                db.execute("INSERT OR IGNORE INTO reader_state VALUES('initialized',1)")
            db.execute("DELETE FROM cursors WHERE seen<?", (time.time() - 30 * 86400,))

    @staticmethod
    def _count(db, key):
        db.execute(
            "INSERT INTO reader_state VALUES(?,1) ON CONFLICT(key) DO UPDATE SET value=value+1",
            (key,),
        )

    def batch(self):
        result = []
        size = 0
        with database("security-outbox.sqlite3") as db:
            for row in db.execute(
                "SELECT data FROM outbox ORDER BY created,id LIMIT ?", (MAX_BATCH,)
            ):
                if size + len(row["data"].encode()) > MAX_BATCH_BYTES:
                    break
                result.append(json.loads(row["data"]))
                size += len(row["data"].encode())
        return result

    def acknowledge(self, identities):
        with database("security-outbox.sqlite3") as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "DELETE FROM outbox WHERE id=?",
                ((identity,) for identity in identities),
            )
            db.execute("COMMIT")

    def status(self):
        raw = util.load_json(self.status_path)
        status = {
            "provider": PROVIDER,
            "available": False,
            "updated_at": raw.get("updated_at"),
            "version": raw.get("version"),
            "running": raw.get("running") is True,
            "mode": raw.get("mode", "tls_observation"),
        }
        try:
            status["updated_at"] = (
                datetime.fromtimestamp(timestamp(raw["updated_at"]), timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
            status["available"] = (
                status["running"]
                and -30 <= time.time() - timestamp(raw["updated_at"]) < 90
            )
        except (ValueError, KeyError):
            pass
        stats = raw.get("stats") or {}
        allowed = [
            "Packets",
            "EntryPackets",
            "Alerts",
            "TrackedFlows",
            "TLS13ClientHellos",
            "FlowEvictions",
            "BufferOverflows",
            "ParseWarnings",
            "RateLimitedAlerts",
            "AlertWriteFailures",
            "ReplayHistoryEvictions",
            "AnomalySourceEvictions",
            "AnomalySampleDrops",
        ]
        status["stats"] = {
            key: stats[key]
            for key in allowed
            if type(stats.get(key)) is int and stats[key] >= 0
        }
        status["capture_dropped"] = (
            raw.get("capture_dropped", 0)
            if type(raw.get("capture_dropped", 0)) is int
            else 0
        )
        status["rss_bytes"] = (
            raw.get("rss_bytes", 0) if type(raw.get("rss_bytes", 0)) is int else 0
        )
        with database("security-outbox.sqlite3") as db:
            status["pending"] = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            status["reader_counters"] = {
                row["key"]: row["value"]
                for row in db.execute(
                    "SELECT key,value FROM reader_state WHERE key!='initialized'"
                )
            }
        return status


class EventStore:
    def __init__(self):
        with database("security-events.sqlite3") as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS events(node TEXT NOT NULL,id TEXT NOT NULL,observed REAL NOT NULL,received REAL NOT NULL,kind TEXT NOT NULL,severity TEXT NOT NULL,data TEXT NOT NULL,notification TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,PRIMARY KEY(node,id))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS events_received ON events(received)")
            db.execute("CREATE INDEX IF NOT EXISTS events_observed ON events(observed)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS events_notification ON events(notification,retry_at)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS sensors(node TEXT PRIMARY KEY,received REAL NOT NULL,data TEXT NOT NULL)"
            )
        try:
            self.ip_context = ip_enrichment.IPContext()
        except Exception as exc:
            print("IP context disabled: " + type(exc).__name__, flush=True)
            self.ip_context = None

    def enrich(self, values):
        if self.ip_context:
            return self.ip_context.events(values)
        return [
            dict(
                value,
                source_info={
                    ip: {"status": "unavailable"}
                    for ip in value.get("probe_source_ips") or []
                },
            )
            for value in values
        ]

    def ingest(self, node, events, status):
        if (
            not util.valid_node_name(node)
            or not isinstance(events, list)
            or len(events) > MAX_BATCH
        ):
            raise ValueError("invalid event batch")
        clean = [normalize(value) for value in events]
        if len(json.dumps(clean).encode()) > MAX_BATCH_BYTES:
            raise ValueError("event batch too large")
        if not isinstance(status, dict) or len(json.dumps(status).encode()) > 8192:
            raise ValueError("invalid sensor status")
        for key in ["available", "running"]:
            if key in status and type(status[key]) is not bool:
                raise ValueError("invalid sensor state")
        for key in ["capture_dropped", "rss_bytes", "pending"]:
            if key in status and (
                type(status[key]) is not int or not 0 <= status[key] <= 10**12
            ):
                raise ValueError("invalid sensor counter")
        stats = status.get("stats") or {}
        if (
            not isinstance(stats, dict)
            or len(stats) > 32
            or any(type(value) is not int or value < 0 for value in stats.values())
        ):
            raise ValueError("invalid sensor metrics")
        now = time.time()
        with database("security-events.sqlite3") as db:
            db.execute("BEGIN IMMEDIATE")
            for event in clean:
                notification = (
                    "pending"
                    if event["severity"] == "high" and not event["backfill"]
                    else "summary"
                )
                db.execute(
                    "INSERT OR IGNORE INTO events(node,id,observed,received,kind,severity,data,notification) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        node,
                        event["event_id"],
                        timestamp(event["last_seen"]),
                        now,
                        event["event"],
                        event["severity"],
                        json.dumps(event, separators=(",", ":")),
                        notification,
                    ),
                )
            db.execute(
                "INSERT OR REPLACE INTO sensors VALUES(?,?,?)",
                (node, now, json.dumps(status, separators=(",", ":"))),
            )
            cutoff = now - 30 * 86400
            db.execute("DELETE FROM events WHERE observed<? OR received<?", (cutoff, cutoff))
            db.execute("COMMIT")
        self.enrich(
            [value for value in clean if value["severity"] in {"high", "medium"}]
        )
        return [event["event_id"] for event in clean]

    def sensor(self, node):
        with database("security-events.sqlite3") as db:
            row = db.execute(
                "SELECT received,data FROM sensors WHERE node=?", (node,)
            ).fetchone()
            if not row:
                return {"provider": PROVIDER, "available": False, "configured": False}
            result = json.loads(row["data"])
            result["configured"] = True
            result["report_age"] = max(0, time.time() - row["received"])
            result["available"] = (
                result.get("available") is True and result["report_age"] < 90
            )
            return result

    def recent(self, node="", limit=10):
        limit = max(1, min(20, int(limit)))
        with database("security-events.sqlite3") as db:
            rows = db.execute(
                "SELECT node,id,data FROM events "
                + ("WHERE node=? " if node else "")
                + "ORDER BY received DESC LIMIT ?",
                ((node, limit) if node else (limit,)),
            ).fetchall()
            values = [dict(json.loads(row["data"]), node=row["node"]) for row in rows]
        return self.enrich(values)

    def detail(self, node, identity):
        with database("security-events.sqlite3") as db:
            if not re.fullmatch("[a-f0-9]{16,64}", identity):
                return None
            matches = db.execute(
                "SELECT data FROM events WHERE node=? AND id LIKE ? LIMIT 2",
                (node, identity + "%"),
            ).fetchall()
            value = (
                dict(json.loads(matches[0]["data"]), node=node)
                if len(matches) == 1
                else None
            )
        return self.enrich([value])[0] if value else None

    def summary(self, start, end):
        with database("security-events.sqlite3") as db:
            rows = db.execute(
                "SELECT node,severity,kind,COUNT(*) AS count FROM events WHERE received>=? AND received<? GROUP BY node,severity,kind ORDER BY node,severity,kind",
                (start, end),
            ).fetchall()
            return [dict(row) for row in rows]

    def risk_report(self, start, end, node=""):
        """Count the whole observation window; sample high risks before noise.

        A backfilled event keeps its actual observation time, and low first-flight
        observations never displace a high/medium event in the detail sample.
        """
        if not 0 < end - start <= 31 * 86400:
            raise ValueError("invalid security window")
        if node and not util.valid_node_name(node):
            raise ValueError("invalid security node")
        clause = "observed>=? AND observed<?" + (" AND node=?" if node else "")
        params = (start, end, node) if node else (start, end)
        with database("security-events.sqlite3") as db:
            db.execute("BEGIN")
            counts = db.execute(
                "SELECT node,severity,kind,COUNT(*) AS count,MAX(observed) AS last_seen "
                "FROM events WHERE " + clause + " GROUP BY node,severity,kind",
                params,
            ).fetchall()
            hour_start = max(start, end - 3600)
            hour_params = (hour_start, end, node) if node else (hour_start, end)
            recent_counts = db.execute(
                "SELECT severity,COUNT(*) AS count FROM events WHERE "
                + clause
                + " GROUP BY severity",
                hour_params,
            ).fetchall()
            samples = db.execute(
                "SELECT node,data FROM events WHERE "
                + clause
                + " AND severity IN ('high','medium') "
                "ORDER BY CASE severity WHEN 'high' THEN 0 ELSE 1 END,observed DESC LIMIT 4",
                params,
            ).fetchall()
            db.execute("COMMIT")
        return {
            "start": start,
            "end": end,
            "counts": [dict(row) for row in counts],
            "last_hour": {row["severity"]: row["count"] for row in recent_counts},
            "events": self.enrich(
                [dict(json.loads(row["data"]), node=row["node"]) for row in samples]
            ),
        }

    def pending(self):
        with database("security-events.sqlite3") as db:
            row = db.execute(
                "SELECT node,id,data,attempts FROM events WHERE notification='pending' AND retry_at<=? ORDER BY received LIMIT 1",
                (time.time(),),
            ).fetchone()
            return dict(row) if row else None

    def notified(self, node, identity, ok):
        with database("security-events.sqlite3") as db:
            if ok:
                db.execute(
                    "UPDATE events SET notification='sent' WHERE node=? AND id=?",
                    (node, identity),
                )
            else:
                db.execute(
                    "UPDATE events SET attempts=attempts+1,retry_at=?+MIN(300,5*(1 << MIN(attempts,6))) WHERE node=? AND id=?",
                    (time.time(), node, identity),
                )


def agent_worker(name, base, token):
    """Separate worker: a failed traffic sample never blocks security reporting."""
    reader = EventReader()
    while True:
        try:
            reader.scan()
            batch = reader.batch()
            result = util.http_json(
                "POST",
                base + "/v1/events",
                token,
                {"name": name, "events": batch, "status": reader.status()},
                timeout=10,
            )
            reader.acknowledge(result.get("ack") or [])
            delay = 1 if batch else 10
        except Exception as exc:
            print("security reporting retry: " + type(exc).__name__, flush=True)
            delay = 5
        time.sleep(delay)
