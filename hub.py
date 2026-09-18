#!/usr/bin/env python3
"""Fleet hub: inventory, snapshots, jobs, local collector. Stdlib only."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import hostinfo
import report
import snapshot
import util

STALE_AFTER = 90
MAX_JOB_WAIT = 35


class Hub:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.local_name = util.normalize_node_name(util.env_opt("NODE_NAME", "local"))
        self.iface = util.env_opt("TRAFFIC_IFACE", "eth0")
        self.reset_day = util.env_int("BILLING_RESET_DAY", 1)
        raw_cap = util.env_opt("MONTHLY_CAP_BYTES", "")
        if raw_cap in {"", "0"}:
            self.local_cap: Optional[int] = None
        else:
            self.local_cap = int(raw_cap)
        self.token = util.env("FLEET_TOKEN")
        self.path = util.state_dir() / "inventory.json"
        self.runtime: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, threading.Event] = {}
        data = util.load_json(self.path)
        self.nodes: dict[str, dict[str, Any]] = data.get("nodes") or {}
        self.kicked: set[str] = set(data.get("kicked") or [])
        self._ensure_local()

    def _ensure_local(self) -> None:
        if not valid_or_fix(self.local_name):
            self.local_name = "local"
        if self.local_name not in self.nodes:
            self.nodes[self.local_name] = {
                "cap_bytes": self.local_cap,
                "reset_day": self.reset_day,
                "iface": self.iface,
                "enabled": True,
                "kind": "local",
                "note": "",
            }
            self._save()

    def _save(self) -> None:
        util.save_json(
            self.path,
            {"nodes": self.nodes, "kicked": sorted(self.kicked)},
        )

    def event_for(self, name: str) -> threading.Event:
        ev = self.events.get(name)
        if ev is None:
            ev = threading.Event()
            self.events[name] = ev
        return ev

    def upsert_from_agent(self, name: str, meta: dict[str, Any]) -> None:
        if name in self.kicked:
            raise PermissionError("kicked")
        rec = self.nodes.get(name) or {
            "cap_bytes": meta.get("cap_bytes"),
            "reset_day": meta.get("reset_day") or 1,
            "iface": meta.get("iface") or "eth0",
            "enabled": True,
            "kind": "agent",
            "note": "",
        }
        rec["kind"] = rec.get("kind") or "agent"
        rec.setdefault("enabled", True)
        rec.setdefault("iface", meta.get("iface") or "eth0")
        rec.setdefault("reset_day", meta.get("reset_day") or 1)
        if "cap_bytes" not in rec:
            rec["cap_bytes"] = meta.get("cap_bytes")
        self.nodes[name] = rec
        self._save()

    def add_node(self, name: str, cap: Optional[int], reset_day: int, iface: str, note: str = "") -> dict[str, Any]:
        name = util.normalize_node_name(name)
        if not util.valid_node_name(name):
            raise ValueError("名字只能用小写字母、数字和短横线，例如 hk-1")
        self.kicked.discard(name)
        rec = self.nodes.get(name) or {}
        rec.update(
            {
                "cap_bytes": cap,
                "reset_day": reset_day,
                "iface": iface or "eth0",
                "enabled": True,
                "kind": rec.get("kind") or "agent",
                "note": note,
            }
        )
        self.nodes[name] = rec
        self._save()
        return rec

    def kick(self, name: str) -> None:
        name = util.normalize_node_name(name)
        self.nodes.pop(name, None)
        self.runtime.pop(name, None)
        self.kicked.add(name)
        self._save()

    def set_enabled(self, name: str, enabled: bool) -> None:
        rec = self._require(name)
        rec["enabled"] = enabled
        self._save()

    def set_cap(self, name: str, cap: Optional[int]) -> None:
        rec = self._require(name)
        rec["cap_bytes"] = cap
        self._save()

    def _require(self, name: str) -> dict[str, Any]:
        name = util.normalize_node_name(name)
        rec = self.nodes.get(name)
        if rec is None:
            raise KeyError(name)
        return rec

    def put_snapshot(self, name: str, snap: dict[str, Any]) -> None:
        with self.lock:
            rt = self.runtime.setdefault(name, {})
            rt["snapshot"] = snap
            rt["last_seen"] = time.time()
            rt.setdefault("pending", [])

    def submit_job(self, name: str, job_type: str, params: dict[str, Any]) -> str:
        name = util.normalize_node_name(name)
        self._require(name)
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "node": name,
            "type": job_type,
            "params": params,
            "status": "queued",
            "result": None,
            "error": None,
            "created": time.time(),
        }
        with self.lock:
            self.jobs[job_id] = job
            if name != self.local_name:
                rt = self.runtime.setdefault(name, {})
                rt.setdefault("pending", []).append(job_id)
        if name == self.local_name:
            threading.Thread(target=self._run_local_job, args=(job_id,), daemon=True).start()
        else:
            self.event_for(name).set()
        return job_id

    def pop_jobs(self, name: str) -> list[dict[str, Any]]:
        with self.lock:
            rt = self.runtime.setdefault(name, {})
            ids = list(rt.get("pending") or [])
            rt["pending"] = []
            jobs = []
            for job_id in ids:
                job = self.jobs.get(job_id)
                if job and job["status"] == "queued":
                    job["status"] = "running"
                    jobs.append(
                        {
                            "id": job_id,
                            "type": job["type"],
                            "params": job["params"],
                        }
                    )
        self.event_for(name).clear()
        return jobs

    def finish_job(self, job_id: str, ok: bool, data: Any = None, error: str = "") -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["status"] = "ok" if ok else "error"
            job["result"] = data
            job["error"] = error

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def wait_job(self, job_id: str, timeout: float = MAX_JOB_WAIT) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get_job(job_id)
            if job and job["status"] in {"ok", "error"}:
                return job
            time.sleep(0.2)
        job = self.get_job(job_id) or {"id": job_id, "status": "timeout"}
        return job

    def _run_local_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            return
        rec = self.nodes.get(self.local_name) or {}
        iface = rec.get("iface") or self.iface
        try:
            if job["type"] == "bw":
                seconds = float((job.get("params") or {}).get("seconds") or 3)
                data = hostinfo.sample_bandwidth(iface, seconds)
                self.finish_job(job_id, True, data)
            else:
                self.finish_job(job_id, False, error=f"unknown job {job['type']}")
        except Exception as exc:
            self.finish_job(job_id, False, error=str(exc))

    def collect_local(self) -> None:
        rec = self.nodes.get(self.local_name) or {}
        iface = rec.get("iface") or self.iface
        reset_day = int(rec.get("reset_day") or self.reset_day)
        snap = snapshot.build_snapshot(iface, reset_day)
        snap["name"] = self.local_name
        self.put_snapshot(self.local_name, snap)

    def maybe_alerts(self) -> None:
        import formatters

        token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or ""
        if not token or not chat_id:
            return
        path = util.state_dir() / "alerts.json"
        state = util.load_json(path)
        changed = False
        for row in self.view(include_disabled=False):
            pct = row.get("pct")
            if pct is None or not row.get("online"):
                continue
            name = row["name"]
            period = (row.get("snapshot") or {}).get("period_start")
            rec = state.setdefault(name, {"fired": [], "period": period})
            if rec.get("period") != period:
                rec["fired"] = []
                rec["period"] = period
                changed = True
            fired = {int(x) for x in rec.get("fired") or []}
            newly = [mark for mark in (50, 70, 85, 95, 100) if pct >= mark and mark not in fired]
            if not newly:
                continue
            rec["fired"] = sorted(fired | set(newly))
            changed = True
            marks = "、".join(f"{m}%" for m in newly)
            text = (
                f"🚨 <b>{report.h(name)}</b> 流量跨过 {marks}\n"
                f"{formatters.node_detail(row)}"
            )
            try:
                report.send_telegram(token, chat_id, text)
            except Exception:
                traceback.print_exc()
        if changed:
            util.save_json(path, state)

    def local_loop(self) -> None:
        while True:
            try:
                self.collect_local()
                self.maybe_alerts()
            except Exception:
                traceback.print_exc()
            time.sleep(20)

    def view(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        now = time.time()
        rows = []
        for name, rec in sorted(self.nodes.items()):
            if not include_disabled and not rec.get("enabled", True):
                continue
            rt = self.runtime.get(name) or {}
            last_seen = float(rt.get("last_seen") or 0)
            snap = rt.get("snapshot") or {}
            online = (name == self.local_name) or (last_seen and now - last_seen <= STALE_AFTER)
            cap = rec.get("cap_bytes")
            used = int(snap.get("period_total") or 0)
            rows.append(
                {
                    "name": name,
                    "enabled": bool(rec.get("enabled", True)),
                    "kind": rec.get("kind") or "agent",
                    "cap_bytes": cap,
                    "reset_day": rec.get("reset_day") or 1,
                    "iface": rec.get("iface") or "eth0",
                    "note": rec.get("note") or "",
                    "online": bool(online),
                    "last_seen": last_seen,
                    "age": (now - last_seen) if last_seen else None,
                    "snapshot": snap,
                    "used": used,
                    "pct": util.cap_pct(used, cap),
                }
            )
        return rows

    def get_node(self, name: str) -> dict[str, Any]:
        name = util.normalize_node_name(name)
        for row in self.view(include_disabled=True):
            if row["name"] == name:
                return row
        raise KeyError(name)


def valid_or_fix(name: str) -> bool:
    return util.valid_node_name(name)


HUB: Optional[Hub] = None


class HubHandler(BaseHTTPRequestHandler):
    server_version = "traffic-hub/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("hub " + (fmt % args), flush=True)

    def _auth(self) -> bool:
        header = self.headers.get("Authorization") or ""
        got = header[7:].strip() if header.startswith("Bearer ") else ""
        return secrets.compare_digest(got, HUB.token) if HUB else False

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if HUB is None:
            self._send(500, {"ok": False})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send(200, {"ok": True})
            return
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        if parsed.path == "/v1/nodes":
            self._send(200, {"ok": True, "nodes": HUB.view()})
            return
        if parsed.path.startswith("/v1/nodes/"):
            name = parsed.path.rsplit("/", 1)[-1]
            try:
                self._send(200, {"ok": True, "node": HUB.get_node(name)})
            except KeyError:
                self._send(404, {"ok": False, "error": "unknown node"})
            return
        if parsed.path.startswith("/v1/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            qs = parse_qs(parsed.query or "")
            wait = bool(qs.get("wait"))
            job = HUB.wait_job(job_id) if wait else HUB.get_job(job_id)
            if not job:
                self._send(404, {"ok": False, "error": "unknown job"})
                return
            self._send(200, {"ok": True, "job": job})
            return
        self._send(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        if HUB is None:
            self._send(500, {"ok": False})
            return
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/v1/sync":
            name = util.normalize_node_name(str(body.get("name") or ""))
            if not util.valid_node_name(name):
                self._send(400, {"ok": False, "error": "bad node name"})
                return
            if name in HUB.kicked:
                self._send(403, {"ok": False, "error": "kicked"})
                return
            try:
                HUB.upsert_from_agent(
                    name,
                    {
                        "cap_bytes": body.get("cap_bytes"),
                        "reset_day": body.get("reset_day"),
                        "iface": body.get("iface"),
                    },
                )
            except PermissionError:
                self._send(403, {"ok": False, "error": "kicked"})
                return
            if body.get("snapshot"):
                snap = body["snapshot"]
                snap["name"] = name
                HUB.put_snapshot(name, snap)
            result = body.get("job_result") or {}
            if result.get("id"):
                HUB.finish_job(
                    str(result["id"]),
                    bool(result.get("ok")),
                    result.get("data"),
                    str(result.get("error") or ""),
                )
            wait = float(body.get("wait") or 0)
            wait = max(0.0, min(25.0, wait))
            if wait and not (HUB.runtime.get(name) or {}).get("pending"):
                HUB.event_for(name).wait(timeout=wait)
            jobs = HUB.pop_jobs(name)
            self._send(200, {"ok": True, "jobs": jobs})
            return
        if parsed.path == "/v1/jobs":
            name = str(body.get("node") or "")
            job_type = str(body.get("type") or "")
            params = body.get("params") or {}
            try:
                job_id = HUB.submit_job(name, job_type, params)
            except KeyError:
                self._send(404, {"ok": False, "error": "unknown node"})
                return
            self._send(200, {"ok": True, "id": job_id})
            return
        if parsed.path == "/v1/nodes":
            action = str(body.get("action") or "")
            name = str(body.get("name") or "")
            try:
                if action == "add":
                    cap = body.get("cap_bytes")
                    rec = HUB.add_node(
                        name,
                        cap if cap != "unlimited" else None,
                        int(body.get("reset_day") or 1),
                        str(body.get("iface") or "eth0"),
                        str(body.get("note") or ""),
                    )
                    self._send(200, {"ok": True, "node": rec, "name": util.normalize_node_name(name)})
                    return
                if action == "kick":
                    HUB.kick(name)
                    self._send(200, {"ok": True})
                    return
                if action == "enable":
                    HUB.set_enabled(name, True)
                    self._send(200, {"ok": True})
                    return
                if action == "disable":
                    HUB.set_enabled(name, False)
                    self._send(200, {"ok": True})
                    return
                if action == "cap":
                    HUB.set_cap(name, body.get("cap_bytes"))
                    self._send(200, {"ok": True})
                    return
            except ValueError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            except KeyError:
                self._send(404, {"ok": False, "error": "unknown node"})
                return
            self._send(400, {"ok": False, "error": "bad action"})
            return
        self._send(404, {"ok": False})


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve() -> None:
    global HUB
    HUB = Hub()
    threading.Thread(target=HUB.local_loop, name="local-collect", daemon=True).start()
    bind = util.env_opt("HUB_BIND", "0.0.0.0")
    port = util.env_int("HUB_PORT", 8788)
    server = HubServer((bind, port), HubHandler)
    print(f"traffic-hub listening {bind}:{port} local={HUB.local_name}", flush=True)
    server.serve_forever()


def daily_report() -> None:
    token = util.env("TELEGRAM_BOT_TOKEN")
    chat_id = util.env("TELEGRAM_CHAT_ID")
    fleet = util.env_opt("HUB_URL", "http://127.0.0.1:8788").rstrip("/")
    rows = util.http_json("GET", f"{fleet}/v1/nodes", util.env("FLEET_TOKEN"), timeout=15)
    import formatters

    text = formatters.fleet_overview(rows.get("nodes") or [], title="📊 机群日报")
    report.send_telegram(token, chat_id, text)
    print("daily report sent", flush=True)


if __name__ == "__main__":
    import sys

    if "--daily" in sys.argv:
        daily_report()
    else:
        serve()
