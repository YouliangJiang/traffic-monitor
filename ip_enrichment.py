"""Bounded Hub-only IP context cache; no network calls on report/alert paths."""

from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import ip_data
import util

MAX_CACHE = 4096
MAX_PENDING = 128
MAX_DAILY = 500
CACHE_TTL = 7 * 86400
RETRY_AFTER = 3600
MAX_RESPONSE = 32768
LOOKUP_TIMEOUT = 4
GEO_URL = "https://ipwho.is/"
DNS_URL = "https://cloudflare-dns.com/dns-query"


def database():
    # Lazy import keeps the accounting/event reader independent of enrichment.
    from security_events import database as open_database

    return open_database("ip-context.sqlite3")


def address(value):
    try:
        parsed = ipaddress.ip_address(value)
        if getattr(parsed, "scope_id", None):
            return None
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        return parsed
    except (ValueError, TypeError):
        return None


def text(value, maximum=120):
    return " ".join(value.split())[:maximum] if isinstance(value, str) else ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # A fixed HTTPS provider may not redirect lookups to an arbitrary host.
        return None


def fetch_json(url, headers=None):
    request = urllib.request.Request(
        url, headers={"User-Agent": "traffic-monitor-ip-context/1", **(headers or {})}
    )
    with urllib.request.build_opener(NoRedirect()).open(
        request, timeout=LOOKUP_TIMEOUT
    ) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("IP context response too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid IP context response")
    return value


def dns_records(name, kind):
    url = DNS_URL + "?" + urllib.parse.urlencode({"name": name, "type": kind})
    response = fetch_json(url, {"Accept": "application/dns-json"})
    return response.get("Answer") or [] if response.get("Status") == 0 else []


def reverse_name(ip):
    expected = 1 if ip.version == 4 else 28
    records = dns_records(ip.reverse_pointer, "PTR")
    names = [
        text(row.get("data"), 253).lower().rstrip(".")
        for row in records
        if row.get("type") == 12
    ]
    for name in names[:2]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name) or ".." in name:
            continue
        try:
            forwards = dns_records(name, "A" if ip.version == 4 else "AAAA")
            verified = any(
                address(row.get("data")) == ip
                for row in forwards
                if row.get("type") == expected
            )
        except Exception:
            verified = False
        return name, verified
    return "", False


def scanner_hint(hostname, verified):
    if verified:
        for domain, service in ip_data.rules().get("scanner_domains", {}).items():
            if hostname == domain or hostname.endswith("." + domain):
                return service
    return ""


def lookup(ip_text, allow_geo=True):
    ip = address(ip_text)
    if ip is None or not ip.is_global or ip.is_multicast:
        return {"status": "non_public" if ip else "invalid"}
    result = {
        "status": "unavailable",
        "checked_at": int(time.time()),
        "provider": "IPWhois / Cloudflare DNS",
        "data_version": ip_data.version(),
    }
    try:
        result.update(ip_data.lookup(str(ip)))
    except Exception:
        pass
    if allow_geo and result["status"] != "ready":
        try:
            query = urllib.parse.urlencode(
                {"fields": "ip,success,country,country_code,region,city,connection"}
            )
            raw = fetch_json(GEO_URL + str(ip) + "?" + query)
            if raw.get("success") is True and address(raw.get("ip")) == ip:
                connection = raw.get("connection") or {}
                if not isinstance(connection, dict):
                    raise ValueError("invalid connection metadata")
                for key in ["country", "country_code", "region", "city"]:
                    result[key] = text(raw.get(key), 80)
                for key in ["org", "isp", "domain"]:
                    result[key] = text(connection.get(key))
                asn = connection.get("asn")
                if type(asn) is int and 0 < asn < 2**32:
                    result["asn"] = asn
                result["status"] = "ready"
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    result["retry_seconds"] = min(
                        86400,
                        max(RETRY_AFTER, int(exc.headers.get("Retry-After", 86400))),
                    )
                except (ValueError, TypeError):
                    result["retry_seconds"] = 86400
        except Exception:
            pass
    try:
        hostname, verified = reverse_name(ip)
        result.update(hostname=hostname, forward_verified=verified)
        if hostname and result["status"] != "ready":
            result["status"] = "partial"
    except Exception:
        pass
    try:
        result.update(
            ip_data.classify(
                result,
                str(ip),
                result.get("hostname", ""),
                result.get("forward_verified", False),
            )
        )
    except Exception:
        result.update(scanner="", network_type="unknown")
    return result


class IPContext:
    def __init__(self):
        with database() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cache(ip TEXT PRIMARY KEY,data TEXT NOT NULL,expires REAL NOT NULL,used REAL NOT NULL,queued REAL,priority INTEGER NOT NULL DEFAULT 1,retry_at REAL NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS cache_queue ON cache(queued,priority,retry_at)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS budget(day INTEGER PRIMARY KEY,count INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS controls(key TEXT PRIMARY KEY,value REAL NOT NULL)"
            )

    def get(self, value, priority=1):
        ip = address(value)
        if ip is None:
            return {"status": "invalid"}
        if not ip.is_global or ip.is_multicast:
            return {"status": "non_public"}
        key = str(ip)
        now = time.time()
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cache WHERE ip=?", (key,)).fetchone()
            data = (
                {"status": "pending", **json.loads(row["data"])}
                if row
                else {"status": "pending"}
            )
            if row:
                db.execute("UPDATE cache SET used=? WHERE ip=?", (now, key))
            data_version = ip_data.version()
            changed = bool(data_version) and data.get("data_version") != data_version
            force_refresh = changed and row and row["retry_at"] == row["expires"]
            needs_lookup = not row or row["expires"] <= now or changed
            if needs_lookup and (not row or row["retry_at"] <= now or force_refresh):
                if row and row["queued"] is not None:
                    db.execute(
                        "UPDATE cache SET priority=MIN(priority,?) WHERE ip=?",
                        (priority, key),
                    )
                elif (
                    db.execute(
                        "SELECT COUNT(*) FROM cache WHERE queued IS NOT NULL"
                    ).fetchone()[0]
                    < MAX_PENDING
                ):
                    if not row:
                        count = db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                        if count >= MAX_CACHE:
                            db.execute(
                                "DELETE FROM cache WHERE ip IN (SELECT ip FROM cache WHERE queued IS NULL ORDER BY used LIMIT 1)"
                            )
                        db.execute(
                            "INSERT INTO cache VALUES(?,?,0,?,?,?,0)",
                            (key, "{}", now, now, priority),
                        )
                    else:
                        db.execute(
                            "UPDATE cache SET queued=?,priority=?,retry_at=0 WHERE ip=?",
                            (now, priority, key),
                        )
                    if data.get("status") in {None, "unavailable"}:
                        data["status"] = "pending"
            db.execute("COMMIT")
        if row and row["expires"] <= now and data.get("status") in {"ready", "partial"}:
            data["stale"] = True
        return data

    def events(self, values):
        enriched = []
        for value in values:
            details = {}
            for ip in list(dict.fromkeys(value.get("probe_source_ips") or []))[:16]:
                try:
                    details[ip] = self.get(
                        ip, 0 if value.get("severity") == "high" else 1
                    )
                except Exception:
                    details[ip] = {"status": "unavailable"}
            enriched.append(dict(value, source_info=details))
        return enriched

    def work_one(self):
        now = time.time()
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT ip FROM cache WHERE queued IS NOT NULL AND retry_at<=? ORDER BY priority,queued LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                db.execute("COMMIT")
                return False
            ip = row["ip"]
            db.execute(
                "UPDATE cache SET queued=NULL,retry_at=? WHERE ip=?", (now + 300, ip)
            )
            day = int(now // 86400)
            usage = db.execute(
                "SELECT count FROM budget WHERE day=?", (day,)
            ).fetchone()
            pause = db.execute(
                "SELECT value FROM controls WHERE key='pause_until'"
            ).fetchone()
            allow_geo = (
                util.env_opt("IP_CONTEXT_ONLINE", "0") == "1"
                and (not usage or usage["count"] < MAX_DAILY)
                and (not pause or pause["value"] <= now)
            )
            if allow_geo:
                db.execute(
                    "INSERT INTO budget VALUES(?,1) ON CONFLICT(day) DO UPDATE SET count=count+1",
                    (day,),
                )
            db.execute("DELETE FROM budget WHERE day<?", (day - 2,))
            db.execute("COMMIT")
        # Lookup is outside every database transaction and HTTP handler.
        value = lookup(ip, allow_geo=allow_geo)
        retry = value.pop("retry_seconds", RETRY_AFTER)
        expires = now + (CACHE_TTL if value["status"] == "ready" else retry)
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT data FROM cache WHERE ip=?", (ip,)).fetchone()
            old = json.loads(previous["data"]) if previous else {}
            if value["status"] != "ready" and old.get("status") == "ready":
                dns = {
                    key: value[key]
                    for key in ["hostname", "forward_verified", "scanner"]
                    if value.get(key)
                }
                value = dict(old, **dns, stale=True)
            db.execute(
                "UPDATE cache SET data=?,expires=?,retry_at=? WHERE ip=?",
                (json.dumps(value, separators=(",", ":")), expires, expires, ip),
            )
            if retry > RETRY_AFTER:
                db.execute(
                    "INSERT OR REPLACE INTO controls VALUES('pause_until',?)",
                    (now + retry,),
                )
            db.execute("COMMIT")
        return True

    def run(self, stop):
        while not stop.is_set():
            try:
                busy = self.work_one()
            except Exception as exc:
                print("IP context retry: " + type(exc).__name__, flush=True)
                busy = False
            stop.wait(2 if busy else 5)
