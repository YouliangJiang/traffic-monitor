"""Validated boundaries for the hub protocol and persisted billing policy."""
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Any, Optional
import util


def integer(value: Any, name: str, minimum: int, maximum: Optional[int] = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'invalid {name}')
    return value


def cap(value: Any) -> Optional[int]:
    return None if value is None else integer(value, 'cap_bytes', 1, 10**18)


@dataclass(frozen=True)
class Billing:
    cap_bytes: Optional[int]
    reset_day: int
    reset_time: str = '00:00:00'
    reset_set: bool = True

    @classmethod
    def parse(cls, value: dict[str, Any]) -> 'Billing':
        flag = value.get('reset_set', True)
        if type(flag) is not bool:
            raise ValueError('invalid reset_set')
        return cls(cap(value.get('cap_bytes')), integer(value.get('reset_day', 1), 'reset_day', 1, 31), util.parse_reset_time(value.get('reset_time') or '00:00:00'), flag)


def snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get('ledger_ok') is not True:
        raise ValueError('snapshot requires valid accounting')
    for key in ['period_rx', 'period_tx', 'period_total', 'today_rx', 'today_tx', 'today_total']:
        integer(value.get(key), key, 0, 10**18)
    if value['period_total'] != value['period_rx'] + value['period_tx']:
        raise ValueError('inconsistent period_total')
    for key in ['ts', 'period_start', 'period_end']:
        from report import parse_iso_datetime
        if not isinstance(value.get(key), str) or parse_iso_datetime(value[key]) is None:
            raise ValueError('invalid snapshot timestamp')
    for key in ['mem_total', 'mem_available', 'swap_total', 'swap_free', 'disk_total', 'disk_used', 'disk_avail']:
        integer(value.get(key, 0), key, 0, 2**63-1)
    for key in ['cpu_pct','steal_pct','load1','net_rx_bps','net_tx_bps','net_window_sec','uptime_sec']:
        number = value.get(key, 0)
        if type(number) not in {int, float} or not math.isfinite(number) or number < 0:
            raise ValueError('invalid snapshot metric')
    services = value.get('svc') or []
    if not isinstance(services, list) or len(services) > util.MAX_SVC:
        raise ValueError('invalid service snapshot')
    for service in services:
        if not isinstance(service, dict) or not util.valid_node_name(service.get('name') or '') or type(service.get('ok')) is not bool:
            raise ValueError('invalid service status')
        integer(service.get('rss',0), 'rss',0,2**63-1)
        ports=service.get('ports') or []
        if not isinstance(ports,list) or len(ports)>util.MAX_SVC_PORTS:
            raise ValueError('invalid service ports')
        for port in ports:
            if not isinstance(port,dict) or type(port.get('ok')) is not bool:
                raise ValueError('invalid port status')
            integer(port.get('port'),'port',1,65535)
    status=value.get('cut') or {}
    if not isinstance(status,dict) or status.get('want','pass') not in {'cut','pass'} or status.get('applied','pass') not in {'cut','pass'}:
        raise ValueError('invalid protection status')
    return value


def job_params(kind: str, value: Any) -> dict[str, Any]:
    if kind not in {'bw', 'nic', 'rtt'} or not isinstance(value, dict):
        raise ValueError('invalid measurement job')
    result = {}
    if 'seconds' in value:
        seconds = value['seconds']
        if type(seconds) not in {int, float} or not math.isfinite(seconds) or not 1 <= seconds <= 15:
            raise ValueError('invalid measurement duration')
        result['seconds'] = seconds
    if 'samples' in value:
        result['samples'] = integer(value['samples'], 'samples', 3, 5)
    return result
