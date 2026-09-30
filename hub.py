#!/usr/bin/env python3
"""Hub: receive agent snapshots over HTTPS, sample itself, push alerts and a daily summary."""
from __future__ import annotations

import hmac
import json
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import formatters
import report
import snapshot
import util

SAMPLE_SEC = 20
STALE_AFTER = 90
HYSTERESIS = 5
QUOTA_MARKS = (80, 90)
MAX_BODY = 65536
MAX_NODES = 64
MAX_WORKERS = 16


class Hub:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.local_name = util.normalize_node_name(util.env("NODE_NAME"))
        if not util.valid_node_name(self.local_name):
            raise SystemExit("invalid NODE_NAME")
        self.config = snapshot.config_from_env()
        self.token = util.env("AGENT_TOKEN")
        self.tg_token = util.env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = util.env("TELEGRAM_CHAT_ID")
        self.offline_after = util.env_int("OFFLINE_ALERT_SEC", 300)
        self.disk_limit = util.env_int("DISK_ALERT_PCT", 90)
        self.mem_limit = util.env_int("MEM_ALERT_PCT", 90)
        hour, minute, _ = util.parse_reset_time(util.env_opt("DAILY_REPORT_TIME", "09:00")).split(":")
        self.daily_at = (int(hour), int(minute))
        self.started = time.time()
        self.last_good = 0.0
        self.collector: Optional[threading.Thread] = None
        self.stop = threading.Event()
        self.runtime: dict[str, dict[str, Any]] = {}
        self.nodes_path = util.state_dir() / "nodes.json"
        self.alerts_path = util.state_dir() / "alerts.json"
        # Known nodes survive restarts so a node that never comes back still raises an offline alert.
        self.nodes: dict[str, dict[str, Any]] = util.load_json(self.nodes_path, strict=True).get("nodes", {}) if self.nodes_path.exists() else {}
        self.alerts: dict[str, Any] = util.load_json(self.alerts_path, strict=True) if self.alerts_path.exists() else {}
        self.alerts.setdefault("nodes", {})
        self.register(self.local_name)

    def register(self, name: str) -> None:
        with self.lock:
            if name in self.nodes:
                return
            if len(self.nodes) >= MAX_NODES:
                raise ValueError("too many nodes")
            self.nodes[name] = {"first_seen": datetime.now(util.local_tz()).isoformat()}
            util.save_json(self.nodes_path, {"nodes": self.nodes})

    def put_snapshot(self, name: str, snap: dict[str, Any], now: Optional[float] = None) -> None:
        self.register(name)
        with self.lock:
            self.runtime[name] = {"snapshot": snap, "last_seen": now or time.time()}

    def collect_local(self) -> None:
        self.put_snapshot(self.local_name, snapshot.build(self.config))

    def view(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = now or time.time()
        with self.lock:
            rows = []
            for name in sorted(self.nodes, key=lambda n: (n != self.local_name, n)):
                rt = self.runtime.get(name) or {}
                last_seen = rt.get("last_seen")
                rows.append({
                    "name": name,
                    "snapshot": rt.get("snapshot"),
                    "online": bool(last_seen and now - last_seen <= STALE_AFTER),
                    # Before a node's first report since start, count from the hub start.
                    "age": now - (last_seen or self.started),
                })
            return rows

    def _events(self, row: dict[str, Any], rec: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
        """(message key, format args, state updates) for every state change on one node."""
        events = []
        offline = row["age"] > self.offline_after
        if offline != rec.get("offline", False):
            key = "alert.offline" if offline else "alert.online"
            events.append((key, {"ago": formatters.duration(row["age"])}, {"offline": offline}))
        snap = row["snapshot"]
        if not row["online"] or not snap:
            return events
        for kind, value, limit, extra in (
            ("disk", formatters.disk_pct(snap), self.disk_limit, {"avail": report.fmt_bytes(snap["disk_avail"])}),
            ("mem", formatters.mem_pct(snap), self.mem_limit, {}),
        ):
            args = dict(extra, pct=f"{value:.0f}", limit=limit)
            if not rec.get(kind) and value >= limit:
                events.append((f"alert.{kind}", args, {kind: True}))
            elif rec.get(kind) and value < limit - HYSTERESIS:
                events.append((f"alert.{kind}_ok", args, {kind: False}))
        if not snap["traffic_ok"]:
            if not rec.get("traffic_error"):
                events.append(("alert.traffic_error", {"error": report.h(snap["traffic_error"])}, {"traffic_error": True}))
            return events
        if rec.get("traffic_error"):
            events.append(("alert.traffic_ok", {}, {"traffic_error": False}))
        cap = snap.get("cap_bytes")
        if cap:
            used = formatters.period_used(snap)
            fired = rec.get("quota", []) if rec.get("period") == snap["period_start"] else []
            newly = [mark for mark in QUOTA_MARKS if report.pct(used, cap) >= mark and mark not in fired]
            if newly:
                args = {"mark": max(newly), "used": report.fmt_bytes(used), "cap": report.fmt_bytes(cap)}
                events.append(("alert.quota", args, {"period": snap["period_start"], "quota": sorted(set(fired) | set(newly))}))
        return events

    def check_alerts(self, now: Optional[float] = None) -> None:
        for row in self.view(now):
            rec = self.alerts["nodes"].setdefault(row["name"], {})
            events = self._events(row, rec)
            if not events:
                continue
            text = "\n".join(formatters.alert(key, row["name"], **args) for key, args, _ in events)
            try:
                report.send_telegram(self.tg_token, self.tg_chat, text)
            except Exception:
                traceback.print_exc()
                continue  # state unchanged, so the next pass retries
            for _, _, update in events:
                rec.update(update)
            util.save_json(self.alerts_path, self.alerts)

    def maybe_daily(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(util.local_tz())
        today = now.date().isoformat()
        due = now.replace(hour=self.daily_at[0], minute=self.daily_at[1], second=0, microsecond=0)
        if now < due or self.alerts.get("daily") == today:
            return
        if "daily" not in self.alerts:
            # Fresh install after today's slot: start with tomorrow's summary.
            self.alerts["daily"] = today
            util.save_json(self.alerts_path, self.alerts)
            return
        # Let agents report after a hub restart, so they are not all listed as missing.
        if time.time() - self.started < 3 * SAMPLE_SEC:
            return
        report.send_telegram(self.tg_token, self.tg_chat, formatters.daily(self.view(), today))
        self.alerts["daily"] = today
        util.save_json(self.alerts_path, self.alerts)

    def _loop(self, interval: float, step) -> None:
        while not self.stop.is_set():
            try:
                step()
            except Exception:
                traceback.print_exc()
            self.stop.wait(interval)

    def _collect_step(self) -> None:
        self.collect_local()
        self.last_good = time.time()

    def _notify_step(self) -> None:
        self.check_alerts()
        self.maybe_daily()

    def start_workers(self) -> None:
        # Collection and Telegram delivery are separate, so a slow send never stalls sampling.
        self.collector = threading.Thread(target=self._loop, args=(SAMPLE_SEC, self._collect_step), name="collect", daemon=True)
        self.collector.start()
        threading.Thread(target=self._loop, args=(SAMPLE_SEC, self._notify_step), name="notify", daemon=True).start()

    def healthy(self) -> bool:
        return bool(self.collector and self.collector.is_alive() and time.time() - self.last_good <= STALE_AFTER)


HUB: Optional[Hub] = None


class HubHandler(BaseHTTPRequestHandler):
    server_version = "traffic-hub/2"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # one request per agent every 20s; errors are printed where they happen

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz" and HUB is not None:
            ready = HUB.healthy()
            self._send(200 if ready else 503, {"ok": ready})
            return
        self._send(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        if HUB is None or self.path != "/v1/report":
            self._send(404, {"ok": False})
            return
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer ") or not hmac.compare_digest(header[7:].strip().encode(), HUB.token.encode()):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                raise ValueError("invalid payload length")
            body = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(body, dict):
                raise ValueError("request must be a JSON object")
            name = util.normalize_node_name(str(body.get("name") or ""))
            if not util.valid_node_name(name):
                raise ValueError("bad node name")
            if name == HUB.local_name:
                raise ValueError("node name is used by the hub")
            HUB.put_snapshot(name, snapshot.validate(body.get("snapshot")))
        except (ValueError, TypeError, UnicodeError) as exc:
            self._send(400, {"ok": False, "error": str(exc)[:160]})
            return
        self._send(200, {"ok": True})


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, ssl_ctx) -> None:
        super().__init__(address, handler)
        self.ssl_ctx = ssl_ctx
        self.workers = threading.BoundedSemaphore(MAX_WORKERS)

    def process_request(self, request, client_address) -> None:
        if not self.workers.acquire(blocking=False):
            request.close()
            return
        threading.Thread(target=self._worker, args=(request, client_address), daemon=True).start()

    def handle_error(self, request, client_address) -> None:
        # Dropped connections and bad handshakes are routine on a public port; log one line.
        exc = sys.exc_info()[1]
        if not isinstance(exc, OSError):
            print(f"hub request from {client_address[0]} failed: {exc!r}", flush=True)

    def _worker(self, request, client_address) -> None:
        try:
            # The TLS handshake runs here, not in the accept loop, so a stalled client blocks only itself.
            request.settimeout(10)
            request = self.ssl_ctx.wrap_socket(request, server_side=True)
            self.process_request_thread(request, client_address)
        except Exception:
            request.close()
        finally:
            self.workers.release()


def serve() -> None:
    global HUB
    import tlsutil

    HUB = Hub()
    HUB.start_workers()
    bind, port = util.env_opt("HUB_BIND", "0.0.0.0"), util.env_int("HUB_PORT", 8788)
    server = HubServer((bind, port), HubHandler, tlsutil.server_context())
    print(f"traffic-hub listening on https://{bind}:{port} node={HUB.local_name}", flush=True)
    server.serve_forever()


def forget(name: str) -> None:
    """Drop a decommissioned node. Run with the hub stopped (install.sh forget does this)."""
    for path, key in ((util.state_dir() / "nodes.json", "nodes"), (util.state_dir() / "alerts.json", "nodes")):
        if path.exists():
            data = util.load_json(path, strict=True)
            data.get(key, {}).pop(name, None)
            util.save_json(path, data)
    print(f"forgot {name}")


def send_test() -> None:
    import i18n

    report.send_telegram(util.env("TELEGRAM_BOT_TOKEN"), util.env("TELEGRAM_CHAT_ID"),
                         i18n.t("hub.test", name=report.h(util.env("NODE_NAME"))))
    print("telegram test message sent")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--test"]:
        send_test()
    elif sys.argv[1:2] == ["--forget"] and len(sys.argv) == 3:
        forget(util.normalize_node_name(sys.argv[2]))
    else:
        serve()
