#!/usr/bin/env python3
"""Agent: sample this host every 20s and POST the snapshot to the hub."""
from __future__ import annotations

import sys
import time
import traceback

import snapshot
import tlsutil
import util

SAMPLE_SEC = 20


def main(check: bool = False) -> None:
    name = util.normalize_node_name(util.env("NODE_NAME"))
    if not util.valid_node_name(name):
        raise SystemExit("invalid NODE_NAME")
    url = util.env("HUB_URL").rstrip("/") + "/v1/report"
    pin, token = util.env("HUB_FINGERPRINT"), util.env("AGENT_TOKEN")
    config = snapshot.config_from_env()
    if check:
        # install.sh: prove the hub, pin and token work before enabling the service.
        tlsutil.post_json(url, pin, token, {"name": name, "snapshot": snapshot.build(config)})
        print(f"reported to {url} as {name}")
        return
    print(f"traffic-agent node={name} hub={url}", flush=True)
    while True:
        started = time.monotonic()
        try:
            # Sampling also advances the local ledger, so traffic is counted even while the hub is down.
            snap = snapshot.build(config)
            tlsutil.post_json(url, pin, token, {"name": name, "snapshot": snap})
        except Exception:
            traceback.print_exc()
        time.sleep(max(1.0, SAMPLE_SEC - (time.monotonic() - started)))


if __name__ == "__main__":
    main(check=sys.argv[1:] == ["--check"])
