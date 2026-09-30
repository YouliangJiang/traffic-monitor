#!/usr/bin/env python3
"""Agent: push snapshots to hub and run jobs (bandwidth tests)."""
from __future__ import annotations

import time
import traceback
import threading
import json
from typing import Any, Optional

import cut
import hostinfo
import snapshot
import util


def _cap() -> Optional[int]:
    raw = util.env_opt("MONTHLY_CAP_BYTES", "0")
    if raw in {"", "0"}:
        return None
    return int(raw)


def _reset_time() -> str:
    try:
        return util.parse_reset_time(util.env_opt("BILLING_RESET_TIME", "00:00:00"))
    except ValueError:
        return "00:00:00"


def _billing_path():
    return util.state_dir() / "billing.json"


def load_billing() -> tuple[Optional[int], int, str, bool]:
    """Hub inventory wins once a sync has been saved. Env is only the first boot."""
    cap = _cap()
    reset_day = util.env_int("BILLING_RESET_DAY", 1)
    reset_time = _reset_time()
    reset_set = bool(util.env_opt("BILLING_RESET_DAY"))
    saved = _read_billing()
    if isinstance(saved, dict) and saved:
        if "cap_bytes" in saved:
            raw = saved.get("cap_bytes")
            cap = None if raw in (None, "", 0, "0") else int(raw)
        if saved.get("reset_day"):
            reset_day = int(saved["reset_day"])
        if saved.get("reset_time"):
            try:
                reset_time = util.parse_reset_time(str(saved["reset_time"]))
            except ValueError:
                pass
        if "reset_set" in saved:
            reset_set = bool(saved.get("reset_set"))
    return cap, reset_day, reset_time, reset_set


def _read_billing() -> dict:
    data = util.load_json(_billing_path(), strict=True) if _billing_path().exists() else {}
    return data if isinstance(data, dict) else {}


def install_push() -> dict:
    raw = _read_billing().get("from_install")
    return raw if isinstance(raw, dict) else {}


def store_billing(cap: Optional[int], reset_day: int, reset_time: str, reset_set: bool, enabled: bool = True, kicked: bool = False) -> None:
    prev = _read_billing()
    payload = {
        "cap_bytes": cap,
        "reset_day": int(reset_day),
        "reset_time": reset_time,
        "reset_set": bool(reset_set),
        "enabled": enabled,
        "kicked": kicked,
    }
    if prev.get("from_install"):
        payload["from_install"] = prev["from_install"]
    util.save_json(_billing_path(), payload)


def clear_install_push() -> None:
    prev = _read_billing()
    if "from_install" not in prev:
        return
    prev.pop("from_install", None)
    util.save_json(_billing_path(), prev)


def execute_job(job: dict[str, Any], iface: str, *, cutting: bool) -> dict[str, Any]:
    job_id = str(job.get("id") or "")
    try:
        data = hostinfo.run_sample(
            str(job.get("type") or ""),
            job.get("params") or {},
            iface,
            cutting=cutting,
        )
        return {"id": job_id, "ok": True, "data": data}
    except Exception as exc:
        return {"id": job_id, "ok": False, "error": str(exc)}


def cached_job(job: dict[str, Any], iface: str, *, cutting: bool) -> dict[str, Any]:
    """Persist execution intent before generating traffic; never repeat a started job."""
    path = util.state_dir() / "jobs-seen.json"
    cache = util.load_json(path, strict=True) if path.exists() else {}
    job_id = str(job.get("id") or "")
    previous = cache.get(job_id)
    if previous and previous.get("state") == "done":
        return previous["result"]
    if previous and previous.get("state") == "started":
        result = {"id": job_id, "ok": False, "error": "measurement interrupted; not repeated"}
    else:
        cache[job_id] = {"state": "started", "job": job, "ts": time.time()}
        util.save_json(path, cache)
        result = execute_job(job, iface, cutting=cutting)
    cache[job_id] = {"state": "done", "result": result, "ts": time.time()}
    cache = dict(sorted(cache.items(), key=lambda item: item[1].get("ts", 0))[-64:])
    util.save_json(path, cache)
    return result


def main() -> None:
    hub_url, token = util.env("FLEET_HUB_URL").rstrip("/"), util.env("AGENT_TOKEN")
    name = util.normalize_node_name(util.env("NODE_NAME"))
    if not util.valid_node_name(name):
        raise SystemExit("invalid NODE_NAME")
    iface = util.env_opt("TRAFFIC_IFACE", "eth0")
    cap, day, clock, reset_set = load_billing()
    saved = _read_billing()
    configuration = {"cap": cap, "day": day, "clock": clock, "reset_set": reset_set, "enabled": bool(saved.get("enabled", True)), "kicked": bool(saved.get("kicked", False)), "svc": []}
    lock, wake, ready = threading.Lock(), threading.Event(), threading.Event()
    current = {"snapshot": None}

    def collect():
        while True:
            with lock:
                config = dict(configuration)
            try:
                snap = snapshot.build_snapshot(iface, config["day"], config["svc"], reset_time=config["clock"], cap=config["cap"], reset_set=config["reset_set"], allow_cut=config["enabled"] and not config["kicked"])
                with lock:
                    current["snapshot"] = snap
                ready.set()
            except Exception:
                traceback.print_exc()
            wake.wait(20)
            wake.clear()

    threading.Thread(target=collect, name="agent-collect", daemon=True).start()
    cache_path = util.state_dir() / "jobs-seen.json"
    cache = util.load_json(cache_path, strict=True) if cache_path.exists() else {}
    pending_job = next((entry.get("job") for entry in cache.values() if entry.get("state") in {"received", "started"}), None)
    pending_result = next((entry["result"] for entry in cache.values() if entry.get("state") == "done" and not entry.get("confirmed")), None)
    print(f"traffic-agent node={name} hub={hub_url}", flush=True)
    while True:
        if not ready.wait(20):
            continue
        with lock:
            snap, config = current["snapshot"], dict(configuration)
        import report
        stamp = report.parse_iso_datetime(snap.get("ts")) if snap else None
        if not stamp or time.time() - stamp.timestamp() > 90:
            time.sleep(2)
            continue
        payload = {"name": name, "snapshot": snap, "wait": 0 if pending_job or pending_result else 25}
        try:
            if pending_job:
                util.http_json("POST", hub_url + "/v1/sync", token, dict(payload, wait=0, job_ack=pending_job["id"]), timeout=15)
                pending_result = cached_job(pending_job, iface, cutting=(snap.get("cut") or {}).get("want") == "cut")
                pending_job = None
            if pending_result:
                payload["job_result"] = pending_result
                payload["wait"] = 0
            body = util.http_json("POST", hub_url + "/v1/sync", token, payload, timeout=40)
            if pending_result:
                cache = util.load_json(cache_path, strict=True)
                cache[pending_result["id"]]["confirmed"] = True
                util.save_json(cache_path, cache)
                pending_result = None
            with lock:
                configuration.update(cap=body.get("cap_bytes"), day=int(body.get("reset_day") or 1), clock=body.get("reset_time") or "00:00:00", reset_set=bool(body.get("reset_set", True)), enabled=bool(body.get("enabled", True)), kicked=False, svc=util.normalize_svc_list(body.get("svc")))
                updated = dict(configuration)
            store_billing(updated["cap"], updated["day"], updated["clock"], updated["reset_set"], updated["enabled"], False)
            if any(config[key] != updated[key] for key in configuration):
                wake.set()
            jobs = body.get("jobs") or []
            if jobs:
                pending_job = jobs[0]
                cache = util.load_json(cache_path, strict=True) if cache_path.exists() else {}
                if pending_job["id"] not in cache:
                    cache[pending_job["id"]] = {"state": "received", "job": pending_job, "ts": time.time()}
                    util.save_json(cache_path, cache)
        except util.HubError as exc:
            if exc.code == 403:
                with lock:
                    configuration.update(kicked=True, enabled=False)
                    updated = dict(configuration)
                store_billing(updated["cap"], updated["day"], updated["clock"], updated["reset_set"], False, True)
                wake.set()
            else:
                traceback.print_exc()
            time.sleep(5)
        except Exception:
            traceback.print_exc()
            time.sleep(5)
        if not pending_job and not pending_result:
            time.sleep(1)


if __name__ == "__main__":
    main()
