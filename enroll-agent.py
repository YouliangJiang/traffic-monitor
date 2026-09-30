#!/usr/bin/env python3
"""Root-only enrollment; agent credentials are separate from the control-plane key."""
from __future__ import annotations
import argparse
import grp
import json
import os
from pathlib import Path
import secrets
import util


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    parser.add_argument('--cap')
    parser.add_argument('--reset')
    parser.add_argument('--iface', default='eth0')
    parser.add_argument('--credentials', action='store_true', help='Emit credentials for a private deployment pipe')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('enrollment requires root')
    name = util.normalize_node_name(args.name)
    if not util.valid_node_name(name):
        raise SystemExit('invalid node name')
    config = {}
    for line in Path('/etc/traffic-monitor.env').read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            config[key] = value.strip().strip('"').strip("'")
    os.environ.update(config)
    path = Path(config.get('AGENT_AUTH_FILE') or '/etc/traffic-monitor-agents.json')
    records = util.load_json(path, strict=True) if path.exists() else {'agents': {}}
    token = records.setdefault('agents', {}).setdefault(name, secrets.token_urlsafe(32))
    util.save_json(path, records, mode=0o640)
    os.chown(path, 0, grp.getgrnam('trafficmon').gr_gid)
    base = config.get('HUB_URL') or 'https://127.0.0.1:8788'
    admin = config['ADMIN_TOKEN']
    try:
        existing = util.http_json('GET', base + '/v1/nodes/' + name, admin)['node']
    except util.HubError as exc:
        if exc.code != 404:
            raise
        existing = {'cap_bytes': None, 'reset_day': 1, 'reset_time': '00:00:00', 'reset_set': False}
    day, clock = util.parse_reset(args.reset) if args.reset else (existing['reset_day'], existing['reset_time'])
    policy = {'action': 'add', 'name': name, 'iface': args.iface, 'cap_bytes': util.parse_cap(args.cap) if args.cap else existing['cap_bytes'], 'reset_day': day, 'reset_time': clock, 'reset_set': True if args.reset else existing['reset_set']}
    util.http_json('POST', base + '/v1/nodes', admin, policy)
    if existing.get('enabled') is False:
        util.http_json('POST', base + '/v1/nodes', admin, {'action':'disable', 'name':name})
    if args.credentials:
        print(json.dumps({'AGENT_TOKEN': token, 'NODE_NAME': name}))
    else:
        print('enrolled node=' + name)


if __name__ == '__main__':
    main()
