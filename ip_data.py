"""Read-only local geography/ASN ranges and independently replaceable rules."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import util
from contextlib import closing


def data_dir():
    return Path(util.env_opt("IP_DATA_DIR", "/var/lib/traffic-monitor-ipdata"))


def bundled_rules():
    return Path(__file__).resolve().parent / "data" / "ip-rules.json"


def rules():
    path = data_dir() / "rules.json"
    if not path.is_file():
        path = bundled_rules()
    if path.is_symlink() or path.stat().st_size > 262144:
        raise ValueError("invalid IP rule file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("invalid IP rule schema")
    if (
        len(value.get("scanner_domains", {})) > 64
        or len(value.get("scanner_networks", [])) > 256
        or len(value.get("hosting_names", [])) > 128
    ):
        raise ValueError("IP rule collection too large")
    return value


def version():
    path = data_dir() / "geo.sqlite3"
    if not path.is_file() or path.is_symlink():
        return ""
    metadata = path.stat()
    rule_path = data_dir() / "rules.json"
    rule_path = rule_path if rule_path.is_file() else bundled_rules()
    rule_metadata = rule_path.stat()
    return f"{metadata.st_ino}:{metadata.st_mtime_ns}:{rule_metadata.st_mtime_ns}"


def connect(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing or unsafe IP database")
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA cache_size=-1024")
    return db


def lookup(value):
    ip = ipaddress.ip_address(value)
    path = data_dir() / "geo.sqlite3"
    if not path.is_file():
        return {}
    key = ip.packed
    with closing(connect(path)) as db:
        country = db.execute(
            "SELECT end,country FROM country WHERE family=? AND start<=? ORDER BY start DESC LIMIT 1",
            (ip.version, key),
        ).fetchone()
        network = db.execute(
            "SELECT end,asn,org FROM asn WHERE family=? AND start<=? ORDER BY start DESC LIMIT 1",
            (ip.version, key),
        ).fetchone()
        metadata = {
            row["key"]: row["value"]
            for row in db.execute("SELECT key,value FROM metadata")
        }
    result = {
        "provider": "DB-IP Lite",
        "data_month": metadata.get("month"),
        "data_version": version(),
        "checked_at": int(path.stat().st_mtime),
    }
    if country and country["end"] >= key and country["country"] != "ZZ":
        result["country_code"] = country["country"]
        result["country"] = country["country"]
    if network and network["end"] >= key:
        result.update(asn=network["asn"], org=network["org"])
    if "country_code" in result or "asn" in result:
        result["status"] = "ready"
        return result
    return {}


def classify(result, value, hostname="", verified=False):
    config = rules()
    ip = ipaddress.ip_address(value)
    scanner = ""
    basis = ""
    if verified:
        for domain, service in config.get("scanner_domains", {}).items():
            if hostname == domain or hostname.endswith("." + domain):
                scanner, basis = service, "verified_dns"
                break
    if not scanner:
        for entry in config.get("scanner_networks", []):
            network = ipaddress.ip_network(entry["cidr"])
            if ip.version == network.version and ip in network:
                scanner, basis = entry["service"], "published_network"
                break
    names = (result.get("org", "") + " " + result.get("isp", "")).lower()
    hosting = any(name.lower() in names for name in config.get("hosting_names", []))
    return {
        "scanner": scanner,
        "scanner_basis": basis,
        "network_type": "hosting" if hosting else "unknown",
    }


def country_name(code, lang):
    values = rules().get("country_names", {})
    return values.get(lang, {}).get(code, code)
