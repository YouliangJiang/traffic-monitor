from __future__ import annotations
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import counters
import formatters
import hub
import snapshot
import tlsutil
import util

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
GB = 10**9


def snap(**overrides):
    base = {
        'ts': NOW.isoformat(), 'iface': 'eth0', 'hostname': 'h', 'uptime_sec': 3600.0, 'cpu_pct': 5.0, 'load1': 0.1,
        'nproc': 1, 'mem_total': 1000, 'mem_available': 500, 'disk_total': 1000, 'disk_used': 100, 'disk_avail': 900,
        'net_rx_rate': 0.0, 'net_tx_rate': 0.0, 'cap_bytes': 100 * GB, 'reset_day': 1, 'reset_time': '00:00:00',
        'period_start': '2026-09-01T00:00:00+00:00', 'period_end': '2026-10-01T00:00:00+00:00',
        'period_rx': 0, 'period_tx': 0, 'today_rx': 0, 'today_tx': 0, 'yesterday_rx': 0, 'yesterday_tx': 0,
        'traffic_ok': True, 'traffic_error': '',
    }
    base.update(overrides)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'TRAFFIC_MONITOR_STATE_DIR': str(self.state), 'NODE_NAME': 'home', 'AGENT_TOKEN': 'agent-key',
                                           'TELEGRAM_BOT_TOKEN': 'tg', 'TELEGRAM_CHAT_ID': '1', 'UI_LANG': 'en'})
        self.env.start()
        self.tz = patch.object(util, 'local_tz', return_value=UTC)
        self.tz.start()
        self.sent = []
        self.tg = patch('report.send_telegram', side_effect=lambda token, chat, text: self.sent.append(text))
        self.tg.start()

    def tearDown(self):
        self.tg.stop(); self.tz.stop(); self.env.stop(); self.temp.cleanup()


class LedgerTests(Base):
    def sample(self, start, end, rx=100, tx=100):
        with patch.object(counters, '_iface_bytes', return_value=(0, 0)), patch.object(counters, '_boot_id', return_value='boot'):
            counters.record_sample('eth0', start)
        with patch.object(counters, '_iface_bytes', return_value=(rx, tx)), patch.object(counters, '_boot_id', return_value='boot'):
            counters.record_sample('eth0', end)

    def test_second_precision_and_conservation(self):
        reset = NOW.replace(hour=0, minute=0, second=30)
        self.sample(reset, reset + timedelta(seconds=20), 1000, 1000)
        self.assertEqual(counters.sum_between(reset, reset + timedelta(days=1)), (1000, 1000))
        split = reset + timedelta(seconds=7)
        first = counters.sum_between(reset, split); second = counters.sum_between(split, reset + timedelta(seconds=20))
        self.assertEqual(tuple(a + b for a, b in zip(first, second)), (1000, 1000))

    def test_cross_period_interval_is_distributed(self):
        reset = NOW.replace(hour=0, minute=0, second=30)
        self.sample(reset - timedelta(seconds=20), reset + timedelta(seconds=20))
        self.assertEqual(counters.sum_between(reset - timedelta(days=1), reset), (50, 50))
        self.assertEqual(counters.sum_between(reset, reset + timedelta(days=1)), (50, 50))

    def test_reboot_counts_from_boot_time(self):
        self.sample(NOW - timedelta(minutes=10), NOW - timedelta(minutes=9), 500, 500)
        with patch.object(counters, '_iface_bytes', return_value=(70, 30)), patch.object(counters, '_boot_id', return_value='boot-2'), \
                patch.object(counters, '_boot_time', return_value=NOW - timedelta(minutes=1)):
            counters.record_sample('eth0', NOW)
        self.assertEqual(counters.sum_between(NOW - timedelta(hours=1), NOW), (570, 530))

    def test_billing_period_clamps_short_month_and_honours_time(self):
        start, end = counters.billing_period(datetime(2026, 2, 28, 12, tzinfo=UTC), 31, '08:00')
        self.assertEqual((start, end), (datetime(2026, 2, 28, 8, tzinfo=UTC), datetime(2026, 3, 31, 8, tzinfo=UTC)))
        start, _ = counters.billing_period(datetime(2026, 1, 1, 7, tzinfo=UTC), 1, '08:00')
        self.assertEqual(start, datetime(2025, 12, 1, 8, tzinfo=UTC))

    def test_usage_splits_today_and_yesterday(self):
        self.sample(NOW.replace(hour=0) - timedelta(seconds=10), NOW.replace(hour=0) + timedelta(seconds=10), 200, 200)
        with patch.object(counters, '_iface_bytes', return_value=(200, 200)), patch.object(counters, '_boot_id', return_value='boot'):
            result = counters.usage('eth0', 1, '00:00:00', NOW)
        self.assertEqual((result['today_rx'], result['yesterday_rx'], result['period_rx']), (100, 100, 200))

    def test_accounting_failure_still_reports_resources(self):
        host = {k: v for k, v in snap().items() if k in snapshot.INT_KEYS + snapshot.NUM_KEYS + ('hostname',) and k not in snapshot.TRAFFIC_KEYS}
        config = {'iface': 'eth0', 'cap_bytes': None, 'reset_day': 1, 'reset_time': '00:00:00'}
        with patch('hostinfo.collect', return_value=host), patch.object(counters, 'usage', side_effect=util.StateError('broken')):
            result = snapshot.validate(snapshot.build(config, NOW))
        self.assertFalse(result['traffic_ok'])
        self.assertIn('broken', result['traffic_error'])


class HubTests(Base):
    def post(self, instance, body, token='agent-key'):
        handler = object.__new__(hub.HubHandler)
        raw = json.dumps(body).encode()
        handler.path = '/v1/report'
        handler.headers = {'Authorization': 'Bearer ' + token, 'Content-Length': str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        result = []
        handler._send = lambda status, payload: result.append(status)
        with patch.object(hub, 'HUB', instance):
            handler.do_POST()
        return result[-1]

    def test_report_requires_token_and_valid_snapshot(self):
        instance = hub.Hub()
        self.assertEqual(self.post(instance, {'name': 'sg', 'snapshot': snap()}, token='wrong'), 401)
        self.assertEqual(self.post(instance, {'name': 'sg', 'snapshot': snap(cpu_pct=float('nan'))}), 400)
        self.assertEqual(self.post(instance, {'name': 'home', 'snapshot': snap()}), 400)
        self.assertEqual(self.post(instance, {'name': 'sg', 'snapshot': snap()}), 200)
        self.assertEqual([row['name'] for row in instance.view()], ['home', 'sg'])
        self.assertIn('sg', hub.Hub().nodes)  # registry survives a restart

    def test_offline_alert_and_recovery(self):
        instance = hub.Hub()
        now = time.time()
        instance.put_snapshot('sg', snap(), now=now)
        instance.put_snapshot('home', snap(), now=now + 400)
        instance.check_alerts(now + 400)
        self.assertEqual(len(self.sent), 1); self.assertIn('sg</b> offline', self.sent[0])
        instance.check_alerts(now + 420)
        self.assertEqual(len(self.sent), 1)
        instance.put_snapshot('sg', snap(), now=now + 440)
        instance.put_snapshot('home', snap(), now=now + 440)
        instance.check_alerts(now + 440)
        self.assertIn('back online', self.sent[-1])

    def test_disk_alert_has_hysteresis(self):
        instance = hub.Hub()
        for used, expected in ((950, 1), (880, 1), (800, 2), (950, 3)):
            instance.put_snapshot('home', snap(disk_used=used, disk_avail=1000 - used))
            instance.check_alerts()
            self.assertEqual(len(self.sent), expected, used)
        self.assertIn('disk recovered', self.sent[1])

    def test_quota_marks_fire_once_per_period(self):
        instance = hub.Hub()
        for used, period, expected in ((85, '2026-09-01', 1), (88, '2026-09-01', 1), (95, '2026-09-01', 2), (95, '2026-10-01', 3)):
            instance.put_snapshot('home', snap(period_rx=used * GB, period_start=period + 'T00:00:00+00:00'))
            instance.check_alerts()
            self.assertEqual(len(self.sent), expected, used)
        self.assertIn('reached 90%', self.sent[1])

    def test_failed_delivery_is_retried(self):
        instance = hub.Hub()
        instance.put_snapshot('home', snap(mem_available=10))
        with patch('report.send_telegram', side_effect=RuntimeError('down')):
            instance.check_alerts()
        self.assertFalse(instance.alerts['nodes']['home'].get('mem'))
        instance.check_alerts()
        self.assertIn('memory at 99%', self.sent[-1])

    def test_daily_summary_once_per_day(self):
        instance = hub.Hub()
        instance.put_snapshot('home', snap(yesterday_rx=GB))
        instance.maybe_daily(NOW)  # first run after the slot only arms tomorrow
        self.assertEqual(self.sent, [])
        instance.started -= 3600
        tomorrow = NOW + timedelta(days=1)
        instance.maybe_daily(tomorrow); instance.maybe_daily(tomorrow)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Yesterday 1.00 GB', self.sent[0])
        self.assertIn('Online 1/1', self.sent[0])

    def test_forget_drops_node(self):
        instance = hub.Hub()
        instance.put_snapshot('sg', snap())
        hub.forget('sg')
        self.assertEqual(list(hub.Hub().nodes), ['home'])

    def get_status(self, instance, peer, token='agent-key'):
        handler = object.__new__(hub.HubHandler)
        handler.path = '/v1/status'
        handler.headers = {'Authorization': 'Bearer ' + token}
        handler.client_address = (peer, 40000)
        handler.connection = Mock(getsockname=lambda: ('10.0.0.1', 8788))
        result = []
        handler._send = lambda status, payload: result.append((status, payload))
        with patch.object(hub, 'HUB', instance):
            handler.do_GET()
        return result[-1]

    def test_status_is_local_only_and_needs_token(self):
        instance = hub.Hub()
        instance.put_snapshot('sg', snap())
        self.assertEqual(self.get_status(instance, '10.0.0.9')[0], 403)  # an agent holding the token
        self.assertEqual(self.get_status(instance, '10.0.0.1', token='wrong')[0], 401)
        status, payload = self.get_status(instance, '10.0.0.1')
        self.assertEqual((status, [row['name'] for row in payload['rows']]), (200, ['home', 'sg']))

    def update(self, text, chat=1, age=0, update_id=7):
        return {'update_id': update_id, 'message': {'chat': {'id': chat}, 'date': int(time.time()) - age, 'text': text}}

    def test_status_command_answers_only_the_configured_chat(self):
        instance = hub.Hub()
        instance.put_snapshot('home', snap(today_rx=GB, net_rx_rate=125000.0))
        for update in (self.update('/status', chat=2), self.update('/status', age=600), self.update('hello'),
                       {'update_id': 1}, {'message': 'x'}, self.update('/forget home')):
            instance.handle_update(update)
        self.assertEqual(self.sent, [])
        instance.handle_update(self.update('/status@my_bot now'))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('today 1.00 GB', self.sent[0])
        self.assertIn('Now ↓ 1.0 Mbps', self.sent[0])
        instance.handle_update(self.update('/help'))
        self.assertIn('/status', self.sent[1])

    def test_poll_advances_offset_even_when_reply_fails(self):
        instance = hub.Hub()
        with patch('report.get_updates', return_value=[self.update('/status', update_id=41), self.update('/status', update_id=42)]) as poll, \
                patch('report.send_telegram', side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):
                instance.poll_commands()
        self.assertEqual(poll.call_args[0][1], 0)
        self.assertEqual(instance.update_offset, 42)  # 41 is dropped, 42 is fetched again

    def test_menu_replaces_leftovers_and_is_scoped_to_the_chat(self):
        calls = []
        with patch('report._call', side_effect=lambda token, method, payload, timeout: calls.append((method, payload)) or (200, '{"ok":true}')):
            hub.Hub().sync_menu()
        self.assertEqual([p['scope']['type'] for m, p in calls if m == 'deleteMyCommands'],
                         ['default', 'all_private_chats', 'all_group_chats', 'all_chat_administrators'])
        method, payload = calls[-1]
        self.assertEqual((method, payload['scope']), ('setMyCommands', {'type': 'chat', 'chat_id': '1'}))
        self.assertEqual([c['command'] for c in payload['commands']], ['status', 'help'])
        self.assertTrue(all(c['description'] and not c['description'].startswith('cmd.') for c in payload['commands']))

    def test_menu_failure_does_not_block_commands_and_is_retried(self):
        instance = hub.Hub()
        polls = []

        def poll(*_args):
            polls.append(1)
            if len(polls) == 2:
                instance.stop.set()
            return []
        with patch('report.set_commands', side_effect=[RuntimeError('down'), None]) as menu, \
                patch('report.get_updates', side_effect=poll), patch('builtins.print'):
            instance._command_loop()
        self.assertEqual((menu.call_count, len(polls), instance.menu_synced), (2, 2, True))


@unittest.skipUnless(shutil.which('openssl'), 'openssl required')
class TlsTests(Base):
    def test_pinned_post_end_to_end(self):
        crt, _ = tlsutil.ensure_hub_cert()
        instance = hub.Hub()
        server = hub.HubServer(('127.0.0.1', 0), hub.HubHandler, tlsutil.server_context())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'https://127.0.0.1:{server.server_address[1]}/v1/report'
        try:
            with patch.object(hub, 'HUB', instance):
                pin = tlsutil.fingerprint(crt)
                tlsutil.post_json(url, pin, 'agent-key', {'name': 'sg', 'snapshot': snap()})
                self.assertIn('sg', instance.runtime)
                with self.assertRaises(tlsutil.PinError):
                    tlsutil.post_json(url, '00' * 32, 'agent-key', {'name': 'hk', 'snapshot': snap()})
                with self.assertRaises(tlsutil.HubError):
                    tlsutil.post_json(url, pin, 'wrong', {'name': 'hk', 'snapshot': snap()})
                self.assertNotIn('hk', instance.runtime)
                rows = tlsutil.get_json(url.replace('/v1/report', '/v1/status'), pin, 'agent-key')['rows']
                self.assertEqual([row['name'] for row in rows], ['home', 'sg'])
                self.assertIn('sg', formatters.plain(formatters.status(rows, NOW)))
        finally:
            server.shutdown(); server.server_close()


class FormatTests(Base):
    def test_daily_text_escapes_and_handles_missing(self):
        rows = [{'name': 'a', 'snapshot': snap(traffic_ok=False, traffic_error='<x>'), 'online': True, 'age': 0},
                {'name': 'b', 'snapshot': None, 'online': False, 'age': 600}]
        text = formatters.daily(rows, '2026-09-30')
        self.assertIn('&lt;x&gt;', text)
        self.assertIn('has not reported', text)

    def test_status_text_adds_rate_and_plain_strips_markup(self):
        rows = [{'name': 'a', 'snapshot': snap(net_tx_rate=1250.0, traffic_error='<x>', traffic_ok=False), 'online': True, 'age': 0},
                {'name': 'b', 'snapshot': snap(), 'online': False, 'age': 600}]
        text = formatters.status(rows, NOW)
        self.assertEqual(text.count('Now ↓'), 1)  # not for the offline node
        self.assertIn('↑ 10 Kbps', text)
        self.assertNotIn('Now ↓', formatters.daily(rows, '2026-09-30'))
        plain = formatters.plain(text)
        self.assertNotIn('<b>', plain)
        self.assertIn('<x>', plain)

    def test_parse_cap_is_decimal(self):
        self.assertEqual(util.parse_cap('2T'), 2 * 10**12)
        self.assertEqual(util.parse_cap('500gib'), 500 * 1024**3)
        self.assertIsNone(util.parse_cap('unlimited'))
        with self.assertRaises(ValueError):
            util.parse_cap('2X')


if __name__ == '__main__':
    unittest.main()
