#!/usr/bin/env python3
"""Agent: push snapshots to hub and run jobs (bandwidth tests)."""
from __future__ import annotations

import time
import traceback
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
    saved = util.load_json(_billing_path())
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
    data = util.load_json(_billing_path())
    return data if isinstance(data, dict) else {}


def install_push() -> dict:
    raw = _read_billing().get("from_install")
    return raw if isinstance(raw, dict) else {}


def store_billing(cap: Optional[int], reset_day: int, reset_time: str, reset_set: bool) -> None:
    prev = _read_billing()
    payload = {
        "cap_bytes": cap,
        "reset_day": int(reset_day),
        "reset_time": reset_time,
        "reset_set": bool(reset_set),
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


def main() -> None:
    hub_url = util.env("FLEET_HUB_URL").rstrip("/")
    token = util.env("FLEET_TOKEN")
    name = util.normalize_node_name(util.env("NODE_NAME"))
    if not util.valid_node_name(name):
        raise SystemExit("NODE_NAME must match [a-z][a-z0-9-]{0,31}")
    iface = util.env_opt("TRAFFIC_IFACE", "eth0")
    cap, reset_day, reset_time, reset_set = load_billing()
    pending_result: dict[str, Any] | None = None
    backlog: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    enabled = True
    kicked = False
    print(f"traffic-agent node={name} hub={hub_url}", flush=True)
    while True:
        snap: dict[str, Any] = {}
        try:
            allow_cut = enabled and not kicked
            snap = snapshot.build_snapshot(
                iface,
                reset_day,
                watch,
                reset_time=reset_time,
                cap=cap,
                reset_set=reset_set,
                allow_cut=allow_cut,
            )
            payload = {
                "name": name,
                "iface": iface,
                "reset_day": reset_day,
                "reset_time": reset_time,
                "reset_set": reset_set,
                "cap_bytes": cap,
                "snapshot": snap,
                "wait": 0 if backlog else 25,
            }
            push = install_push()
            if push:
                payload["apply_config"] = push
            if pending_result:
                payload["job_result"] = pending_result
            body = util.http_json("POST", f"{hub_url}/v1/sync", token, payload, timeout=40)
            pending_result = None
            if push:
                clear_install_push()
            kicked = False
            watch = util.normalize_svc_list(body.get("svc"))
            if "cap_bytes" in body:
                raw_cap = body.get("cap_bytes")
                cap = None if raw_cap in (None, "", 0, "0") else int(raw_cap)
            if body.get("reset_day"):
                reset_day = int(body.get("reset_day") or reset_day)
            if body.get("reset_time"):
                try:
                    reset_time = util.parse_reset_time(str(body.get("reset_time")))
                except ValueError:
                    pass
            if "reset_set" in body:
                reset_set = bool(body.get("reset_set"))
            if "enabled" in body:
                enabled = bool(body.get("enabled"))
                if not enabled:
                    cut.force_pass(str(snap.get("period_key") or ""))
            store_billing(cap, reset_day, reset_time, reset_set)
            jobs = list(body.get("jobs") or [])
            queued = list(backlog)
            backlog = []
            if queued:
                seen = {str(item.get("id") or "") for item in jobs}
                jobs = [item for item in queued if str(item.get("id") or "") not in seen] + jobs
            cutting = allow_cut and cut.should_cut(
                int(snap.get("period_total") or 0), cap, reset_day, reset_set
            )
            if jobs:
                pending_result = execute_job(jobs[0], iface, cutting=cutting)
                backlog = jobs[1:]
        except util.HubError as exc:
            if exc.code == 403:
                kicked = True
                cut.force_pass(str(snap.get("period_key") or ""))
            else:
                traceback.print_exc()
            time.sleep(5)
            continue
        except Exception:
            traceback.print_exc()
            time.sleep(5)
            continue
        if not pending_result:
            time.sleep(1)


if __name__ == "__main__":
    main()
