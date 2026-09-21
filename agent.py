#!/usr/bin/env python3
"""Agent: push snapshots to hub and run jobs (bandwidth tests)."""
from __future__ import annotations

import os
import time
import traceback
from typing import Any

import hostinfo
import snapshot
import util


def _cap() -> int | None:
    raw = util.env_opt("MONTHLY_CAP_BYTES", "0")
    if raw in {"", "0"}:
        return None
    return int(raw)


def execute_job(job: dict[str, Any], iface: str) -> dict[str, Any]:
    job_id = str(job.get("id") or "")
    try:
        if job.get("type") == "bw":
            seconds = float((job.get("params") or {}).get("seconds") or 3)
            data = hostinfo.sample_bandwidth(iface, seconds)
            return {"id": job_id, "ok": True, "data": data}
        if job.get("type") == "nic":
            seconds = float((job.get("params") or {}).get("seconds") or 3)
            data = hostinfo.sample_nic(iface, seconds)
            return {"id": job_id, "ok": True, "data": data}
        return {"id": job_id, "ok": False, "error": f"unknown job {job.get('type')}"}
    except Exception as exc:
        return {"id": job_id, "ok": False, "error": str(exc)}


def main() -> None:
    hub_url = util.env("FLEET_HUB_URL").rstrip("/")
    token = util.env("FLEET_TOKEN")
    name = util.normalize_node_name(util.env("NODE_NAME"))
    if not util.valid_node_name(name):
        raise SystemExit("NODE_NAME must match [a-z][a-z0-9-]{0,31}")
    iface = util.env_opt("TRAFFIC_IFACE", "eth0")
    reset_day = util.env_int("BILLING_RESET_DAY", 1)
    cap = _cap()
    pending_result: dict[str, Any] | None = None
    print(f"traffic-agent node={name} hub={hub_url}", flush=True)
    while True:
        try:
            snap = snapshot.build_snapshot(iface, reset_day)
            payload = {
                "name": name,
                "iface": iface,
                "reset_day": reset_day,
                "cap_bytes": cap,
                "snapshot": snap,
                "wait": 25,
            }
            if pending_result:
                payload["job_result"] = pending_result
            body = util.http_json("POST", f"{hub_url}/v1/sync", token, payload, timeout=40)
            pending_result = None
            jobs = body.get("jobs") or []
            for job in jobs:
                pending_result = execute_job(job, iface)
                break
        except Exception:
            traceback.print_exc()
            pending_result = None
            time.sleep(5)
            continue
        if not pending_result:
            time.sleep(1)


if __name__ == "__main__":
    main()
