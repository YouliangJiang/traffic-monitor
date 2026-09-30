#!/usr/bin/env python3
"""Transactional NIC accounting; bounded-memory queries over timestamped intervals."""
from __future__ import annotations

import calendar
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

import util

UTC = timezone.utc


def _iface_bytes(iface: str) -> tuple[int, int]:
    with open('/proc/net/dev', encoding='utf-8') as stream:
        for line in stream:
            label, _, rest = line.partition(':')
            if label.strip() == iface:
                fields = rest.split()
                return int(fields[0]), int(fields[8])
    raise RuntimeError(f'interface {iface} not found')


def _boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _boot_time() -> Optional[datetime]:
    try:
        for line in Path('/proc/stat').read_text().splitlines():
            if line.startswith('btime '):
                return datetime.fromtimestamp(int(line.split()[1]), UTC)
    except (OSError, ValueError):
        pass
    return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _micros(value: datetime) -> int:
    return int(_as_utc(value).timestamp() * 1_000_000)


def _path() -> Path:
    return util.state_dir() / 'traffic.sqlite3'


@contextmanager
def _database():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise util.StateError('ledger database must not be a symlink')
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA cache_size=-2048')
        connection.execute('CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS samples(end_us INTEGER PRIMARY KEY, start_us INTEGER NOT NULL, rx INTEGER NOT NULL CHECK(rx>=0), tx INTEGER NOT NULL CHECK(tx>=0), CHECK(end_us>start_us))')
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def record_sample(iface: str, now: Optional[datetime] = None) -> None:
    now = _as_utc(now or datetime.now(UTC))
    rx, tx = _iface_bytes(iface)
    boot = _boot_id()
    if not boot:
        raise RuntimeError('missing boot identity')
    with _database() as connection:
        connection.execute('BEGIN IMMEDIATE')
        saved = connection.execute('SELECT value FROM metadata WHERE id=1').fetchone()
        if saved:
            previous = json.loads(saved[0])
            start = _as_utc(datetime.fromisoformat(previous['last_ts']))
            if now < start:
                raise RuntimeError('clock moved backwards; keeping the previous accounting baseline')
            same_iface = previous['iface'] == iface
            if same_iface and boot == previous['boot_id'] and rx >= previous['last_rx'] and tx >= previous['last_tx']:
                drx, dtx = rx - previous['last_rx'], tx - previous['last_tx']
            elif same_iface and boot != previous['boot_id']:
                start = _boot_time() or start
                drx, dtx = rx, tx
            else:
                drx = dtx = 0
            if _micros(now) > _micros(start) and (drx or dtx):
                connection.execute('INSERT INTO samples VALUES(?,?,?,?)', (_micros(now), _micros(start), drx, dtx))
        baseline = {'iface': iface, 'boot_id': boot, 'last_rx': rx, 'last_tx': tx, 'last_ts': now.isoformat()}
        connection.execute('INSERT OR REPLACE INTO metadata VALUES(1,?)', (json.dumps(baseline),))
        cutoff = _micros(now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=400))
        connection.execute('DELETE FROM samples WHERE end_us<?', (cutoff,))
        connection.execute('COMMIT')


def _sum(connection: sqlite3.Connection, start: int, end: int) -> tuple[int, int]:
    if end <= start:
        return 0, 0
    row = connection.execute('SELECT COALESCE(SUM(rx),0), COALESCE(SUM(tx),0) FROM samples WHERE end_us>? AND end_us<=? AND start_us>=?', (start, end, start)).fetchone()
    rx, tx = int(row[0]), int(row[1])
    # Only intervals crossing an edge need proportional allocation. Integer prefix
    # differences conserve every byte when adjacent periods are queried separately.
    for finish, begin, drx, dtx in connection.execute('SELECT end_us,start_us,rx,tx FROM samples WHERE end_us>? AND start_us<? AND (start_us<? OR end_us>?)', (start, end, start, end)):
        duration = finish - begin
        left, right = max(start, begin) - begin, min(end, finish) - begin
        rx += drx * right // duration - drx * left // duration
        tx += dtx * right // duration - dtx * left // duration
    return rx, tx


def sum_between(start: datetime, end: datetime) -> tuple[int, int]:
    with _database() as connection:
        return _sum(connection, _micros(start), _micros(end))


def day_rows(last_n: Optional[int] = None) -> list[tuple[date, int, int]]:
    with _database() as connection:
        if last_n:
            final_row = connection.execute('SELECT end_us FROM samples ORDER BY end_us DESC LIMIT 1').fetchone()
            bounds = (final_row[0] - last_n * 86400 * 1_000_000, final_row[0]) if final_row else (None, None)
        else:
            bounds = connection.execute('SELECT MIN(start_us),MAX(end_us) FROM samples').fetchone()
        if bounds[0] is None:
            return []
        tz = util.local_tz()
        first = datetime.fromtimestamp(bounds[0] / 1_000_000, UTC).astimezone(tz).date()
        final = datetime.fromtimestamp(bounds[1] / 1_000_000, UTC).astimezone(tz).date()
        if last_n:
            first = max(first, final - timedelta(days=last_n - 1))
        rows = []
        while first <= final:
            begin = datetime.combine(first, datetime.min.time(), tzinfo=tz)
            rx, tx = _sum(connection, _micros(begin), _micros(begin + timedelta(days=1)))
            rows.append((first, rx, tx))
            first += timedelta(days=1)
        return rows


def _reset_at(year: int, month: int, reset_day: int, reset_time: str) -> datetime:
    day = min(max(1, int(reset_day)), calendar.monthrange(year, month)[1])
    hour, minute, second = (int(x) for x in util.parse_reset_time(reset_time).split(':'))
    return datetime(year, month, day, hour, minute, second, tzinfo=util.local_tz())


def billing_period(now: datetime, reset_day: int, reset_time: str = '00:00:00') -> tuple[datetime, datetime]:
    """[start, end) of the billing period containing now, in the host timezone.

    A reset day past the end of a short month falls on that month's last day.
    """
    now = _as_utc(now).astimezone(util.local_tz())
    this = _reset_at(now.year, now.month, reset_day, reset_time)
    if now >= this:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return this, _reset_at(year, month, reset_day, reset_time)
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return _reset_at(year, month, reset_day, reset_time), this


def usage(iface: str, reset_day: int, reset_time: str = '00:00:00', now: Optional[datetime] = None) -> dict[str, Any]:
    """Record one sample, then sum the billing period, today and yesterday (host-local days)."""
    now = _as_utc(now or datetime.now(UTC))
    record_sample(iface, now)
    start, end = billing_period(now, reset_day, reset_time)
    today = now.astimezone(util.local_tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    with _database() as connection:
        period_rx, period_tx = _sum(connection, _micros(start), _micros(end))
        today_rx, today_tx = _sum(connection, _micros(today), _micros(today + timedelta(days=1)))
        yesterday_rx, yesterday_tx = _sum(connection, _micros(yesterday), _micros(today))
    return {
        'period_start': start.isoformat(),
        'period_end': end.isoformat(),
        'period_rx': period_rx,
        'period_tx': period_tx,
        'today_rx': today_rx,
        'today_tx': today_tx,
        'yesterday_rx': yesterday_rx,
        'yesterday_tx': yesterday_tx,
    }
