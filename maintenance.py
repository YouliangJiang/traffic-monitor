#!/usr/bin/env python3
"""Thirty-day security retention, independent of sensor activity."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import time

import util

RETENTION_DAYS = 30
RETENTION_SECONDS = RETENTION_DAYS * 86400
DATABASES = {
    "security-events.sqlite3": {
        "events": "observed < ? OR received < ?",
        "sensors": "received < ?",
    },
    "security-outbox.sqlite3": {
        "outbox": "created < ?",
        "cursors": "seen < ?",
    },
    "ip-context.sqlite3": {
        "cache": "used < ? AND queued IS NULL",
        "budget": "day < ?",
    },
}


def prune_database(path, filters, cutoff):
    if not path.exists():
        return {}
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("unsafe maintenance database")
    result = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)) as db:
        db.execute("PRAGMA cache_size=-1024")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with db:
            for table, condition in filters.items():
                if table not in tables:
                    continue
                value = int(cutoff // 86400) if table == "budget" else cutoff
                result[table] = db.execute(
                    "DELETE FROM " + table + " WHERE " + condition,
                    (value,) * condition.count("?"),
                ).rowcount
                if table == "outbox" and result[table] and "reader_state" in tables:
                    db.execute(
                        "INSERT INTO reader_state VALUES('retention_expired',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
                        (result[table],),
                    )
        # PASSIVE never waits for active readers. VACUUM only reclaims meaningful free space.
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        if free * page_size >= 8 * 1024 * 1024 and free >= pages * 0.2:
            if shutil.disk_usage(path.parent).free >= pages * page_size * 2:
                db.execute("VACUUM")
                result["vacuumed"] = True
        result["bytes"] = path.stat().st_size
    return result


def run(directory=None, now=None):
    directory = (Path(directory) if directory is not None else util.state_dir()).absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("unsafe maintenance directory")
    now = time.time() if now is None else now
    cutoff = now - RETENTION_SECONDS
    result = {"schema_version": 1, "retention_days": RETENTION_DAYS,
              "finished_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
              "databases": {}, "backup_files_removed": 0, "errors": {}}
    for name, filters in DATABASES.items():
        try:
            result["databases"][name] = prune_database(directory / name, filters, cutoff)
        except Exception as exc:
            result["errors"][name] = type(exc).__name__
    backups = directory / "integration-replay-backups"
    if backups.is_dir() and not backups.is_symlink():
        for path in backups.glob("before-seconds-*.sqlite3"):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                path.unlink()
                result["backup_files_removed"] += 1
    util.save_json(directory / "maintenance-status.json", result)
    print(json.dumps(result, separators=(",", ":")), flush=True)
    if result["errors"]:
        raise RuntimeError("security maintenance failed; see maintenance-status.json")
    return result


if __name__ == "__main__":
    run()
