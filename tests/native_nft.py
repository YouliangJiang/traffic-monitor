#!/usr/bin/env python3
"""Run only inside an anonymous network namespace, never the host namespace."""
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from unittest.mock import patch

if os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
    raise SystemExit('Refusing to run in the host network namespace.')

sys.path.insert(0, sys.argv[1])
import cutctl
import report
import util


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=8, **kwargs)


def require(args):
    result = run(args)
    if result.returncode:
        raise RuntimeError(str(args[0]) + ': ' + result.stderr[:300])


child = subprocess.Popen(['unshare', '--net', 'sleep', '45'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
listener = None
try:
    for _ in range(100):
        if child.poll() is not None:
            raise RuntimeError('Cannot create the isolated agent namespace.')
        if Path('/proc/%s/ns/net' % child.pid).exists() and os.readlink('/proc/%s/ns/net' % child.pid) != os.readlink('/proc/self/ns/net'):
            break
        time.sleep(.01)
    else:
        raise RuntimeError('Agent namespace was not created.')

    require(['ip', 'link', 'add', 'tm-hub', 'type', 'veth', 'peer', 'name', 'tm-agent'])
    require(['ip', 'link', 'set', 'tm-agent', 'netns', str(child.pid)])
    require(['ip', 'address', 'add', '192.0.2.1/24', 'dev', 'tm-hub'])
    require(['ip', 'link', 'set', 'tm-hub', 'up'])
    enter = ['nsenter', '--target', str(child.pid), '--net']
    require(enter + ['ip', 'address', 'add', '192.0.2.2/24', 'dev', 'tm-agent'])
    require(enter + ['ip', 'link', 'set', 'tm-agent', 'up'])

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('192.0.2.1', 8788))
    listener.listen(8)
    listener.settimeout(.25)
    stopping = threading.Event()

    def serve():
        while not stopping.is_set():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(b'OK')
            except socket.timeout:
                continue
            except OSError:
                break

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = enter + ['python3', '-c', 'import socket; s=socket.create_connection(("192.0.2.1",8788),timeout=2); s.settimeout(2); print(s.recv(2).decode()); s.close()']
    before = run(client)
    assert before.returncode == 0 and before.stdout.strip() == 'OK'

    endpoints = {'hub_port': 8788, 'hub4': ['192.0.2.1'], 'hub6': [], 'tg4': ['149.154.167.220'], 'tg6': []}
    with patch.object(cutctl, 'collect_endpoints', return_value=endpoints), patch.object(cutctl, 'is_hub', return_value=True), patch.object(cutctl, 'ssh_ports', return_value=[22]), patch.object(cutctl, 'iface_exists', return_value=False), patch.object(cutctl, 'append_tailscale_underlay', return_value=None):
        rules = cutctl.build_nft(True)
    applied = run(['nft', '-f', '-'], input=rules)
    assert applied.returncode == 0, applied.stderr
    during = run(client)
    assert during.returncode == 0 and during.stdout.strip() == 'OK', during.stderr
    fingerprint = cutctl._kernel_state()[1]
    try:
        with patch.object(cutctl, 'build_nft', return_value='invalid nft syntax'):
            cutctl.apply_rules('cut', endpoints)
    except RuntimeError:
        pass
    else:
        raise AssertionError('invalid rule transaction was accepted')
    assert cutctl._kernel_state()[1] == fingerprint
    print(json.dumps({'test': 'hub_agent_connectivity', 'before_cutoff': 'OK', 'project_cutoff': 'OK', 'failed_transaction_preserves_rules': True, 'host_network_touched': False}))

    # A missing runtime table models the same persistent state after a reboot.
    require(['nft', 'delete', 'table', 'inet', 'trafficmon-cut'])
    with tempfile.TemporaryDirectory(prefix='traffic-monitor-nft-review-') as directory:
        current = datetime.now(timezone.utc)
        state = Path(directory)
        env = {'STATE_DIRECTORY': directory, 'TRAFFIC_MONITOR_STATE_DIR': directory, 'TRAFFIC_CUT_STATE_DIR': str(state/'trusted'), 'ROLE': 'hub', 'BILLING_RESET_DAY': '1'}
        with patch.dict(os.environ, env), patch.object(util, 'local_tz', return_value=timezone.utc), patch.object(cutctl, 'collect_endpoints', return_value=endpoints):
            start, _ = report.billing_period(current, 1)
            util.save_json(state/'cut-desired.json', {'want':'cut','armed':True,'period_key':start.strftime('%Y-%m-%dT%H:%M:%S'),'reset_day':1,'reset_time':'00:00:00','reset_set':True,'cap':1000})
            import cut
            util.save_json(cut.applied_path(), {'want':'cut','ok':True,'endpoints':endpoints,'kernel_digest':fingerprint}, mode=0o644)
            with contextlib.redirect_stdout(open(os.devnull, 'w')):
                result = cutctl.reconcile()
            exists = run(['nft', 'list', 'table', 'inet', 'trafficmon-cut']).returncode == 0
    assert result == 0 and exists is True
    print(json.dumps({'test':'reboot_reconciliation','reported_success':True,'kernel_cutoff_table_restored':True,'host_network_touched':False}))
finally:
    if listener is not None:
        stopping.set()
        listener.close()
    child.terminate()
    child.wait(timeout=3)
