#!/usr/bin/env python3
"""Stream official monthly IP ranges into an atomically replaced SQLite file."""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
from datetime import datetime, timezone, timedelta
import gzip
import hashlib
import html
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import urllib.error
import urllib.request
import ip_data

MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_ROWS = 4_000_000


def request(url):
    headers = {
        "User-Agent": "traffic-monitor-ipdata/1.0",
        "Referer": "https://db-ip.com/db/lite.php",
    }
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=30
    )


def download(url, target):
    digest = hashlib.sha256()
    count = 0
    with request(url) as response, target.open("wb") as output:
        while True:
            block = response.read(65536)
            if not block:
                break
            count += len(block)
            if count > MAX_DOWNLOAD:
                raise ValueError("database download too large")
            digest.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if count < 1024:
        raise ValueError("database download incomplete")
    return digest.hexdigest()


def create_schema(db):
    db.execute("PRAGMA cache_size=-2048")
    db.execute("PRAGMA temp_store=FILE")
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute(
        "CREATE TABLE country(family INTEGER,start BLOB,end BLOB,country TEXT,PRIMARY KEY(family,start)) WITHOUT ROWID"
    )
    db.execute(
        "CREATE TABLE asn(family INTEGER,start BLOB,end BLOB,asn INTEGER,org TEXT,PRIMARY KEY(family,start)) WITHOUT ROWID"
    )
    db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")


def import_ranges(db, path, kind):
    count = 0
    batch = []
    previous = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) != (3 if kind == "country" else 4):
                raise ValueError("unexpected IP range columns")
            first, last = ipaddress.ip_address(row[0]), ipaddress.ip_address(row[1])
            if first.version != last.version or int(first) > int(last):
                raise ValueError("invalid IP range")
            if first.version in previous and int(first) <= previous[first.version]:
                raise ValueError("overlapping IP ranges")
            previous[first.version] = int(last)
            values = [first.version, first.packed, last.packed]
            if kind == "country":
                if not re.fullmatch("[A-Z]{2}", row[2]):
                    raise ValueError("invalid country code")
                values.append(row[2])
            else:
                number = int(row[2])
                name = " ".join(row[3].split())
                if not 0 <= number < 2**32 or len(name) > 512:
                    raise ValueError("invalid ASN record")
                values += [number, name]
            batch.append(values)
            count += 1
            if count > MAX_ROWS:
                raise ValueError("too many IP range records")
            if len(batch) >= 1000:
                db.executemany(
                    "INSERT INTO "
                    + kind
                    + " VALUES("
                    + ",".join("?" for _ in values)
                    + ")",
                    batch,
                )
                batch = []
            if count % 100000 == 0:
                print(kind, "imported", count, flush=True)
        if batch:
            db.executemany(
                "INSERT INTO "
                + kind
                + " VALUES("
                + ",".join("?" for _ in batch[0])
                + ")",
                batch,
            )
    return count


def current_month(path):
    try:
        with closing(ip_data.connect(path)) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='month'").fetchone()
            return row[0] if row else ""
    except Exception:
        return ""


def refresh_rules(directory):
    source = directory / "rules.json"
    value = json.loads(
        (source if source.is_file() else ip_data.bundled_rules()).read_text()
    )
    url = value["sources"]["censys_ranges"]
    try:
        with request(url) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("scanner source too large")
        content = html.unescape(raw.decode("utf-8"))
        networks = []
        for candidate in re.findall(r"[0-9a-fA-F:.]+/\d{1,3}", content):
            try:
                network = ipaddress.ip_network(candidate, strict=True)
            except ValueError:
                continue
            if network.is_global and str(network) not in networks:
                networks.append(str(network))
        if not 8 <= len(networks) <= 128:
            raise ValueError("unexpected Censys feed format")
        value["scanner_networks"] = [
            entry
            for entry in value.get("scanner_networks", [])
            if entry.get("service") != "Censys"
        ] + [
            {"cidr": network, "service": "Censys", "source": url}
            for network in networks
        ]
        value["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception as exc:
        print(
            "Scanner range refresh retained prior rules:",
            type(exc).__name__,
            flush=True,
        )
    fd, name = tempfile.mkstemp(prefix=".rules-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, 0o640)
        os.replace(name, directory / "rules.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def update(directory):
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("unsafe IP data directory")
    destination = directory / "geo.sqlite3"
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    existing = current_month(destination)
    if existing == month:
        refresh_rules(directory)
        print("IP database already current:", month)
        return
    candidates = [month, (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")]
    with tempfile.TemporaryDirectory(prefix=".ipdata-", dir=directory) as scratch:
        scratch = Path(scratch)
        hashes = {}
        chosen = None
        for candidate in candidates:
            if existing and candidate <= existing:
                break
            try:
                for kind in ["country", "asn"]:
                    url = f"https://download.db-ip.com/free/dbip-{kind}-lite-{candidate}.csv.gz"
                    hashes[kind] = download(url, scratch / (kind + ".csv.gz"))
                chosen = candidate
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
        if chosen is None:
            if existing:
                refresh_rules(directory)
                print(
                    "Newest edition not yet available; prior database retained:",
                    existing,
                )
                return
            raise RuntimeError("no IP database edition available")
        temporary = scratch / "geo.sqlite3"
        with sqlite3.connect(str(temporary)) as db:
            create_schema(db)
            for kind in ["country", "asn"]:
                count = import_ranges(db, scratch / (kind + ".csv.gz"), kind)
                if count < 100000:
                    raise ValueError("IP database has unexpectedly few rows")
                db.executemany(
                    "INSERT INTO metadata VALUES(?,?)",
                    [(kind + "_rows", str(count)), (kind + "_sha256", hashes[kind])],
                )
            db.executemany(
                "INSERT INTO metadata VALUES(?,?)",
                [
                    ("month", chosen),
                    ("provider", "DB-IP Lite CC BY 4.0"),
                    ("updated_at", now.isoformat(timespec="seconds")),
                ],
            )
            db.commit()
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("IP database integrity failed")
            for sample in ["1.1.1.1", "8.8.8.8"]:
                key = ipaddress.ip_address(sample).packed
                row = db.execute(
                    "SELECT end FROM country WHERE family=4 AND start<=? ORDER BY start DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if not row or row[0] < key:
                    raise ValueError("IP database sample lookup failed")
        os.chmod(temporary, 0o640)
        with temporary.open("rb") as data:
            os.fsync(data.fileno())
        os.replace(temporary, destination)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    refresh_rules(directory)
    print(
        "Published local IP database:",
        chosen,
        "bytes",
        destination.stat().st_size,
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=ip_data.data_dir())
    args = parser.parse_args()
    update(args.directory)
