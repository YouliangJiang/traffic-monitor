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
import i18n

STALE_AFTER = 90
MAX_JOB_WAIT = 50
MAX_BODY = 262144
JOB_KEEP_SEC = 300
MAX_JOBS = 64
MAX_WORKERS = 16


class Hub:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.local_name = util.normalize_node_name(util.env_opt("NODE_NAME", "local"))
        self.iface = util.env_opt("TRAFFIC_IFACE", "eth0")
        self.reset_day = util.env_int("BILLING_RESET_DAY", 1)
        try:
            self.reset_time = util.parse_reset_time(util.env_opt("BILLING_RESET_TIME", "00:00:00"))
        except ValueError:
            self.reset_time = "00:00:00"
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
        for rec in self.nodes.values():
            rec.setdefault("reset_time", "00:00:00")
            rec.setdefault("reset_set", True)
        self._ensure_local()

    def _ensure_local(self) -> None:
        if not valid_or_fix(self.local_name):
            self.local_name = "local"
        if self.local_name not in self.nodes:
            self.nodes[self.local_name] = {
                "cap_bytes": self.local_cap,
                "reset_day": self.reset_day,
                "reset_time": self.reset_time,
                "reset_set": True,
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
        existing = self.nodes.get(name)
        rec = dict(existing) if existing else {
            "cap_bytes": meta.get("cap_bytes"),
            "reset_day": meta.get("reset_day") or 1,
            "reset_time": meta.get("reset_time") or "00:00:00",
            "reset_set": bool(meta.get("reset_set", True)),
            "iface": meta.get("iface") or "eth0",
            "enabled": True,
            "kind": "agent",
            "note": "",
        }
        rec["kind"] = rec.get("kind") or "agent"
        rec.setdefault("enabled", True)
        rec.setdefault("iface", meta.get("iface") or "eth0")
        rec.setdefault("reset_day", meta.get("reset_day") or 1)
        rec.setdefault("reset_time", meta.get("reset_time") or "00:00:00")
        rec.setdefault("reset_set", True)
        if "cap_bytes" not in rec:
            rec["cap_bytes"] = meta.get("cap_bytes")
        apply = meta.get("apply_config")
        if isinstance(apply, dict):
            if "cap_bytes" in apply:
                raw_cap = apply.get("cap_bytes")
                rec["cap_bytes"] = None if raw_cap in (None, "", 0, "0") else int(raw_cap)
            if apply.get("reset_day"):
                rec["reset_day"] = int(apply.get("reset_day") or rec.get("reset_day") or 1)
            if apply.get("reset_time"):
                try:
                    rec["reset_time"] = util.parse_reset_time(str(apply.get("reset_time")))
                except ValueError:
                    pass
            if "reset_set" in apply:
                rec["reset_set"] = bool(apply.get("reset_set"))
        self.nodes[name] = rec
        if rec != existing:
            self._save()

    def add_node(
        self,
        name: str,
        cap: Optional[int],
        reset_day: int,
        iface: str,
        note: str = "",
        reset_time: str = "00:00:00",
        reset_set: bool = True,
    ) -> dict[str, Any]:
        name = util.normalize_node_name(name)
        if not util.valid_node_name(name):
            raise ValueError(i18n.t("hub.bad_name"))
        self.kicked.discard(name)
        rec = self.nodes.get(name) or {}
        try:
            reset_time = util.parse_reset_time(reset_time)
        except ValueError:
            reset_time = "00:00:00"
        rec.update(
            {
                "cap_bytes": cap,
                "reset_day": int(reset_day or 1),
                "reset_time": reset_time,
                "reset_set": bool(reset_set),
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
        ev = self.events.pop(name, None)
        if ev:
            ev.set()
        with self.lock:
            drop = [jid for jid, job in self.jobs.items() if job.get("node") == name]
            for jid in drop:
                self.jobs.pop(jid, None)
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

    def set_reset(self, name: str, reset_day: int, reset_time: str = "00:00:00") -> None:
        rec = self._require(name)
        rec["reset_day"] = int(reset_day)
        rec["reset_time"] = util.parse_reset_time(reset_time)
        rec["reset_set"] = True
        self._save()

    def svc_specs(self, name: str) -> list[dict[str, Any]]:
        rec = self.nodes.get(util.normalize_node_name(name)) or {}
        return util.normalize_svc_list(rec.get("svc"))

    def set_svc(self, name: str, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rec = self._require(name)
        rec["svc"] = util.normalize_svc_list(specs)
        self._save()
        return rec["svc"]

    def upsert_svc(self, name: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
        specs = [dict(item) for item in self.svc_specs(name)]
        spec = util.normalize_svc_list([spec])
        if not spec:
            raise ValueError(i18n.t("error.bad_svc", text=""))
        spec = spec[0]
        specs = [item for item in specs if item.get("name") != spec["name"]]
        specs.append(spec)
        if len(specs) > util.MAX_SVC:
            raise ValueError(i18n.t("error.too_many_svc"))
        return self.set_svc(name, specs)

    def remove_svc(self, name: str, svc_name: str = "") -> list[dict[str, Any]]:
        if not svc_name:
            return self.set_svc(name, [])
        svc_name = util.parse_svc_name(svc_name)
        specs = [item for item in self.svc_specs(name) if item.get("name") != svc_name]
        return self.set_svc(name, specs)

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
                pending = rt.setdefault("pending", [])
                pending.append(job_id)
                if len(pending) > 8:
                    rt["pending"] = pending[-8:]
            self._prune_jobs_locked()
        if name == self.local_name:
            threading.Thread(target=self._run_local_job, args=(job_id,), daemon=True).start()
        else:
            self.event_for(name).set()
        return job_id

    def pop_jobs(self, name: str) -> list[dict[str, Any]]:
        """Hand the agent one queued job. The rest stay pending for the next sync."""
        with self.lock:
            rt = self.runtime.setdefault(name, {})
            ids = list(rt.get("pending") or [])
            jobs = []
            rest: list[str] = []
            taken = False
            for job_id in ids:
                job = self.jobs.get(job_id)
                if not job or job["status"] != "queued":
                    continue
                if not taken:
                    job["status"] = "running"
                    jobs.append(
                        {
                            "id": job_id,
                            "type": job["type"],
                            "params": job["params"],
                        }
                    )
                    taken = True
                    continue
                rest.append(job_id)
            rt["pending"] = rest
        if rest:
            self.event_for(name).set()
        else:
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

    def _prune_jobs_locked(self) -> None:
        now = time.time()
        drop = [
            jid
            for jid, job in self.jobs.items()
            if now - float(job.get("created") or 0) > JOB_KEEP_SEC
        ]
        if not drop and len(self.jobs) <= MAX_JOBS:
            return
        if len(self.jobs) - len(drop) > MAX_JOBS:
            extra = sorted(
                (jid for jid in self.jobs if jid not in drop),
                key=lambda jid: float((self.jobs[jid] or {}).get("created") or 0),
            )
            drop.extend(extra[: max(0, len(self.jobs) - len(drop) - MAX_JOBS)])
        for jid in drop:
            self.jobs.pop(jid, None)

    def prune_jobs(self) -> None:
        with self.lock:
            self._prune_jobs_locked()

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
        snap = (self.runtime.get(self.local_name) or {}).get("snapshot") or {}
        cutting = (snap.get("cut") or {}).get("want") == "cut"
        try:
            data = hostinfo.run_sample(
                str(job.get("type") or ""),
                job.get("params") or {},
                iface,
                cutting=cutting,
            )
            self.finish_job(job_id, True, data)
        except Exception as exc:
            self.finish_job(job_id, False, error=str(exc))

    def collect_local(self) -> None:
        rec = self.nodes.get(self.local_name) or {}
        iface = rec.get("iface") or self.iface
        reset_day = int(rec.get("reset_day") or self.reset_day)
        reset_time = rec.get("reset_time") or self.reset_time
        reset_set = bool(rec.get("reset_set", True))
        allow_cut = bool(rec.get("enabled", True))
        specs = util.normalize_svc_list(rec.get("svc"))
        cap = rec.get("cap_bytes")
        if cap in (None, "", 0, "0"):
            cap = None
        else:
            cap = int(cap)
        snap = snapshot.build_snapshot(
            iface,
            reset_day,
            specs,
            reset_time=reset_time,
            cap=cap,
            reset_set=reset_set,
            allow_cut=allow_cut,
        )
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
            newly = [mark for mark in report.THRESHOLDS if pct >= mark and mark not in fired]
            if newly:
                rec["fired"] = sorted(fired | set(newly))
                changed = True
                marks = i18n.t("sep.list").join(f"{m}%" for m in newly)
                text = i18n.t(
                    "hub.alert",
                    name=report.h(name),
                    marks=marks,
                    detail=formatters.node_detail(row),
                )
                try:
                    report.send_telegram(token, chat_id, text)
                except Exception:
                    traceback.print_exc()
            cut_info = (row.get("snapshot") or {}).get("cut") or {}
            want = str(cut_info.get("want") or "pass")
            applied = str(cut_info.get("applied") or "pass")
            prev_cut = str(rec.get("cut") or "pass")
            applied_ok = cut_info.get("ok")
            if want == "cut" and applied == "cut" and applied_ok is not False and prev_cut != "cut":
                rec["cut"] = "cut"
                rec["cut_period"] = period
                changed = True
                text = i18n.t(
                    "hub.cut_on",
                    name=report.h(name),
                    pct=f"{pct:.1f}",
                    detail=formatters.node_detail(row),
                    err=report.h(cut_info.get("error") or ""),
                )
                try:
                    report.send_telegram(token, chat_id, text)
                except Exception:
                    traceback.print_exc()
            elif want == "cut" and applied_ok is False and prev_cut != "fail":
                rec["cut"] = "fail"
                changed = True
                text = i18n.t(
                    "hub.cut_fail",
                    name=report.h(name),
                    pct=f"{pct:.1f}",
                    detail=formatters.node_detail(row),
                    err=report.h(cut_info.get("error") or ""),
                )
                try:
                    report.send_telegram(token, chat_id, text)
                except Exception:
                    traceback.print_exc()
            elif want == "pass" and applied == "pass" and prev_cut in {"cut", "fail"}:
                rec["cut"] = "pass"
                changed = True
                cut_period = str(rec.get("cut_period") or "")
                key = "hub.cut_off" if cut_period and cut_period != str(period or "") else "hub.cut_off_early"
                text = i18n.t(
                    key,
                    name=report.h(name),
                    detail=formatters.node_detail(row),
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
                self.prune_jobs()
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
            specs = util.normalize_svc_list(rec.get("svc"))
            rows.append(
                {
                    "name": name,
                    "enabled": bool(rec.get("enabled", True)),
                    "kind": rec.get("kind") or "agent",
                    "cap_bytes": cap,
                    "reset_day": rec.get("reset_day") or 1,
                    "reset_time": rec.get("reset_time") or "00:00:00",
                    "reset_set": bool(rec.get("reset_set", True)),
                    "iface": rec.get("iface") or "eth0",
                    "note": rec.get("note") or "",
                    "svc": specs,
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._send(413, {"ok": False, "error": "payload too large"})
            return
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
                        "reset_time": body.get("reset_time"),
                        "iface": body.get("iface"),
                        "apply_config": body.get("apply_config"),
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
            if name in HUB.kicked:
                self._send(403, {"ok": False, "error": "kicked"})
                return
            jobs = HUB.pop_jobs(name)
            rec = HUB.nodes.get(name) or {}
            self._send(
                200,
                {
                    "ok": True,
                    "jobs": jobs,
                    "svc": HUB.svc_specs(name),
                    "cap_bytes": rec.get("cap_bytes"),
                    "reset_day": rec.get("reset_day") or 1,
                    "reset_time": rec.get("reset_time") or "00:00:00",
                    "reset_set": bool(rec.get("reset_set", True)),
                    "enabled": bool(rec.get("enabled", True)),
                },
            )
            return
        if parsed.path == "/v1/jobs":
            name = str(body.get("node") or "")
            job_type = str(body.get("type") or "")
            params = body.get("params") or {}
            if not isinstance(params, dict) or job_type not in hostinfo.JOB_TYPES:
                self._send(400, {"ok": False, "error": "unknown job"})
                return
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
                    raw_flag = body.get("reset_set")
                    reset_set = True if raw_flag is None else bool(raw_flag)
                    rec = HUB.add_node(
                        name,
                        cap if cap != "unlimited" else None,
                        int(body.get("reset_day") or 1),
                        str(body.get("iface") or "eth0"),
                        str(body.get("note") or ""),
                        str(body.get("reset_time") or "00:00:00"),
                        reset_set,
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
                if action == "reset":
                    HUB.set_reset(
                        name,
                        int(body.get("reset_day") or 1),
                        str(body.get("reset_time") or "00:00:00"),
                    )
                    self._send(200, {"ok": True})
                    return
                if action == "svc":
                    if body.get("clear"):
                        specs = HUB.remove_svc(name, str(body.get("svc_name") or ""))
                    else:
                        specs = HUB.upsert_svc(
                            name,
                            {
                                "name": body.get("svc_name") or body.get("svc"),
                                "ports": body.get("ports"),
                                "proc": body.get("proc"),
                            },
                        )
                    self._send(200, {"ok": True, "svc": specs})
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
    request_queue_size = 64

    def __init__(self, server_address, RequestHandlerClass, ssl_ctx):
        super().__init__(server_address, RequestHandlerClass)
        self.ssl_ctx = ssl_ctx
        self._workers = threading.BoundedSemaphore(MAX_WORKERS)

    def process_request(self, request, client_address):
        if not self._workers.acquire(blocking=False):
            try:
                request.close()
            except Exception:
                pass
            return
        thread = threading.Thread(
            target=self._run_worker,
            args=(request, client_address),
            daemon=True,
        )
        thread.start()

    def _run_worker(self, request, client_address):
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._workers.release()

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(8)
            request = self.ssl_ctx.wrap_socket(request, server_side=True)
            request.settimeout(45)
        except Exception:
            try:
                request.close()
            except Exception:
                pass
            return
        super().process_request_thread(request, client_address)


def serve() -> None:
    global HUB
    HUB = Hub()
    threading.Thread(target=HUB.local_loop, name="local-collect", daemon=True).start()
    bind = util.env_opt("HUB_BIND", "0.0.0.0")
    port = util.env_int("HUB_PORT", 8788)
    import tlsutil

    server = HubServer((bind, port), HubHandler, tlsutil.server_context())
    print(f"traffic-hub listening https://{bind}:{port} local={HUB.local_name}", flush=True)
    server.serve_forever()


def daily_report() -> None:
    token = util.env("TELEGRAM_BOT_TOKEN")
    chat_id = util.env("TELEGRAM_CHAT_ID")
    fleet = util.env_opt("HUB_URL", "https://127.0.0.1:8788").rstrip("/")
    rows = util.http_json("GET", f"{fleet}/v1/nodes", util.env("FLEET_TOKEN"), timeout=15)
    import formatters

    text = formatters.fleet_overview(rows.get("nodes") or [], title=i18n.t("fleet.daily_title"))
    report.send_telegram(token, chat_id, text)
    print("daily report sent", flush=True)


if __name__ == "__main__":
    import sys

    if "--daily" in sys.argv:
        daily_report()
    else:
        serve()
