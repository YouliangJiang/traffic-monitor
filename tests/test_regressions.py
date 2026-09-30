from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import agent
import counters
import cut
import cutctl
import formatters
import hub
import hostinfo
import report
import util

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'app'
        self.state.mkdir()
        self.cut_state = self.root / 'root-cut'
        self.cut_state.mkdir(mode=0o755)
        self.auth = self.root / 'agents.json'
        util.save_json(self.auth, {'agents': {'agent-a': 'agent-a-key', 'agent-b': 'agent-b-key'}})
        self.env = patch.dict(os.environ, {'STATE_DIRECTORY':str(self.state), 'TRAFFIC_MONITOR_STATE_DIR':str(self.state), 'TRAFFIC_CUT_STATE_DIR':str(self.cut_state), 'ADMIN_TOKEN':'admin-key', 'AGENT_AUTH_FILE':str(self.auth), 'NODE_NAME':'local', 'ROLE':'hub', 'MONTHLY_CAP_BYTES':'1000', 'BILLING_RESET_DAY':'1', 'TELEGRAM_BOT_TOKEN':'not-transmitted', 'TELEGRAM_CHAT_ID':'1'})
        self.env.start()
        self.tz = patch.object(util, 'local_tz', return_value=UTC)
        self.tz.start()

    def tearDown(self):
        self.tz.stop(); self.env.stop(); self.temp.cleanup()

    def instance(self):
        instance = hub.Hub()
        instance.add_node('agent-a', 1000, 1, 'eth0')
        instance.add_node('agent-b', 1000, 1, 'eth0')
        return instance

    def request(self, instance, path, body=None, token='admin-key', length=None):
        handler = object.__new__(hub.HubHandler)
        raw = json.dumps(body or {}).encode()
        handler.path = path
        handler.headers = {'Authorization':'Bearer '+token,'Content-Length':str(len(raw) if length is None else length)}
        handler.rfile = io.BytesIO(raw)
        result=[]
        handler._send=lambda status,body:result.append((status,body))
        with patch.object(hub,'HUB',instance):
            handler.do_POST() if body is not None else handler.do_GET()
        return result[-1]

    def sample(self, start, end, rx=100, tx=100):
        with patch.object(counters,'_iface_bytes',return_value=(0,0)),patch.object(counters,'_boot_id',return_value='boot'):
            counters.record_sample('eth0',start)
        with patch.object(counters,'_iface_bytes',return_value=(rx,tx)),patch.object(counters,'_boot_id',return_value='boot'):
            counters.record_sample('eth0',end)

    def test_second_precision_and_conservation(self):
        reset=NOW.replace(hour=0,minute=0,second=30)
        self.sample(reset,reset+timedelta(seconds=20),1000,1000)
        self.assertEqual(counters.sum_between(reset,reset+timedelta(days=1)),(1000,1000))
        split=reset+timedelta(seconds=7)
        first=counters.sum_between(reset,split);second=counters.sum_between(split,reset+timedelta(seconds=20))
        self.assertEqual(tuple(a+b for a,b in zip(first,second)),(1000,1000))

    def test_cross_period_interval_is_distributed(self):
        reset=NOW.replace(hour=0,minute=0,second=30)
        self.sample(reset-timedelta(seconds=20),reset+timedelta(seconds=20))
        self.assertEqual(counters.sum_between(reset-timedelta(days=1),reset),(50,50))
        self.assertEqual(counters.sum_between(reset,reset+timedelta(days=1)),(50,50))

    def test_legacy_migration_exact_and_once(self):
        data={'iface':'eth0','boot_id':'boot','last_rx':100,'last_tx':200,'last_ts':NOW.isoformat(),'minutes':{'2026-09-30T11:58':{'rx':100,'tx':200},'2026-09-30T11:59':{'rx':300,'tx':400}}}
        util.save_json(self.state/'traffic.json',data)
        self.assertEqual(counters.sum_between(NOW-timedelta(hours=1),NOW),(400,600))
        util.save_json(self.state/'traffic.json',dict(data,minutes={}))
        self.assertEqual(counters.sum_between(NOW-timedelta(hours=1),NOW),(400,600))

    def test_corrupt_ledger_is_not_reset(self):
        ledger=self.state/'traffic.json';ledger.write_text('{broken')
        with patch.object(counters,'_iface_bytes',return_value=(0,0)),patch.object(counters,'_boot_id',return_value='boot'):
            with self.assertRaises(util.StateError):counters.record_sample('eth0',NOW)
        self.assertEqual(ledger.read_text(),'{broken')

    def test_corrupt_inventory_is_not_reinitialized(self):
        path=self.state/'inventory.json';path.write_text('{broken')
        with self.assertRaises(util.StateError):hub.Hub()
        self.assertEqual(path.read_text(),'{broken')

    def test_corrupt_billing_does_not_fall_back_to_env_policy(self):
        path=self.state/'billing.json';path.write_text('{broken')
        with self.assertRaises(util.StateError):agent.load_billing()
        self.assertEqual(path.read_text(),'{broken')

    def test_accounting_failure_preserves_cutoff(self):
        desired={'want':'cut','armed':True,'period_key':'2026-09-01T00:00:00','reset_day':1,'reset_time':'00:00:00','reset_set':True,'cap':1000}
        util.save_json(cut.desired_path(),desired)
        import snapshot
        with patch.object(counters,'record_sample',side_effect=PermissionError('disk unavailable')):
            with self.assertRaises(report.AccountingError):snapshot.build_snapshot('eth0',1,cap=1000)
        self.assertEqual(util.load_json(cut.desired_path()),desired)

    def test_atomic_writes_do_not_follow_symlinks(self):
        victim=self.root/'victim';victim.write_text('unchanged')
        target=self.state/'cut-applied.json'
        target.symlink_to(victim)
        target.with_suffix('.json.tmp').symlink_to(victim)
        util.save_json(target,{'safe':True})
        self.assertEqual(victim.read_text(),'unchanged')
        self.assertFalse(target.is_symlink())

    def test_privileged_state_is_separate_and_trusted(self):
        victim=self.root/'victim';victim.write_text('unchanged')
        (self.state/'cut-applied.json.tmp').symlink_to(victim)
        cutctl.write_applied({'want':'pass','ok':True})
        self.assertEqual(victim.read_text(),'unchanged')
        self.assertTrue(cut.applied_path().is_file())
        self.cut_state.chmod(0o777)
        with self.assertRaises(RuntimeError):cutctl.write_applied({'ok':True})

    def test_concurrent_json_writers(self):
        target=self.state/'inventory.json';barrier=threading.Barrier(16);errors=[]
        def write(i):
            try:barrier.wait();util.save_json(target,{'writer':i})
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=write,args=(i,)) for i in range(16)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(errors,[])
        self.assertIn(util.load_json(target)['writer'],range(16))

    def test_agent_cannot_manage_or_spoof_another_node(self):
        instance=self.instance()
        self.assertEqual(self.request(instance,'/v1/nodes',{'action':'cap','name':'agent-b','cap_bytes':1},'agent-a-key')[0],401)
        self.assertEqual(instance.nodes['agent-b']['cap_bytes'],1000)
        self.assertEqual(self.request(instance,'/v1/sync',{'name':'agent-b','wait':0},'agent-a-key')[0],403)
        self.assertEqual(self.request(instance,'/v1/nodes',token='agent-a-key')[0],401)
        self.assertEqual(self.request(instance,'/v1/nodes',{'action':'cap','name':'agent-b','cap_bytes':2000})[0],200)

    def test_invalid_body_and_billing_are_rejected(self):
        instance=self.instance()
        self.assertEqual(self.request(instance,'/v1/nodes',{},length=-1)[0],400)
        for value in ['1000',True,-1,0]:
            self.assertEqual(self.request(instance,'/v1/nodes',{'action':'cap','name':'agent-a','cap_bytes':value})[0],400)
        self.assertEqual(instance.nodes['agent-a']['cap_bytes'],1000)

    def test_job_lease_redelivery_and_owner(self):
        instance=self.instance()
        job_id=instance.submit_job('agent-a','nic',{})
        with patch.object(hub.time,'time',return_value=1000):
            first=instance.pop_jobs('agent-a')
        with patch.object(hub.time,'time',return_value=1001):
            self.assertEqual(instance.pop_jobs('agent-a'),[])
        with patch.object(hub.time,'time',return_value=1061):
            self.assertEqual(instance.pop_jobs('agent-a'),first)
        with self.assertRaises(ValueError):instance.finish_job(job_id,True,{},node='agent-b')
        instance.acknowledge_job('agent-a',job_id)
        instance.finish_job(job_id,True,{'result':1},node='agent-a')
        self.assertEqual(instance.get_job(job_id)['status'],'ok')
        restarted=self.instance()
        self.assertEqual(restarted.get_job(job_id)['result'],{'result':1})

    def test_agent_deduplicates_measurements(self):
        job={'id':'one','type':'bw','params':{}}
        with patch.object(hostinfo,'run_sample',return_value={'bytes':10}) as run:
            a=agent.cached_job(job,'eth0',cutting=False)
            b=agent.cached_job(job,'eth0',cutting=False)
        self.assertEqual(a,b);self.assertEqual(run.call_count,1)

    def test_started_measurement_is_not_repeated_after_restart(self):
        job={'id':'one','type':'bw','params':{}}
        util.save_json(self.state/'jobs-seen.json',{'one':{'state':'started','job':job,'ts':time.time()}})
        with patch.object(hostinfo,'run_sample') as run:
            result=agent.cached_job(job,'eth0',cutting=False)
        self.assertFalse(result['ok']);self.assertEqual(run.call_count,0)

    def test_failed_alert_retries_without_killing_collector(self):
        instance=self.instance()
        row={'name':'agent-a','online':True,'pct':81,'snapshot':{'period_start':'2026-09-01T00:00:00+00:00'}}
        with patch.object(instance,'view',return_value=[row]),patch.object(formatters,'node_detail',return_value='detail'):
            with patch.object(report,'telegram_call',side_effect=RuntimeError('offline')) as call,contextlib.redirect_stderr(io.StringIO()):
                instance.maybe_alerts()
                self.assertEqual(call.call_count,3)
            self.assertEqual(util.load_json(self.state/'alerts.json')['agent-a']['fired'],[])
            with patch.object(report,'telegram_call',return_value={'ok':True}) as call:
                instance.maybe_alerts();instance.maybe_alerts()
                self.assertEqual(call.call_count,1)
        self.assertEqual(util.load_json(self.state/'alerts.json')['agent-a']['fired'],[80])
        with patch.object(report,'telegram_call',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):report.send_telegram('fake','1','test')

    def test_health_detects_dead_or_stale_collector(self):
        instance=self.instance()
        self.assertEqual(self.request(instance,'/healthz')[0],503)
        instance.collector=Mock();instance.collector.is_alive.return_value=True
        instance.last_good=time.time()
        self.assertEqual(self.request(instance,'/healthz')[0],200)
        instance.last_good-=91
        self.assertEqual(self.request(instance,'/healthz')[0],503)
        instance.collector.is_alive.return_value=False
        self.assertEqual(self.request(instance,'/healthz')[0],503)

    def test_reboot_and_changed_kernel_rules_are_reconciled(self):
        desired={'want':'cut','armed':True,'period_key':'2026-09-01T00:00:00','reset_day':1,'reset_time':'00:00:00','reset_set':True,'cap':1000}
        util.save_json(cut.desired_path(),desired)
        endpoints={'hub_port':8788,'hub4':[],'hub6':[],'tg4':[],'tg6':[]}
        util.save_json(cut.applied_path(),{'want':'cut','ok':True,'endpoints':endpoints,'kernel_digest':'old'})
        for before in [(False,''),(True,'changed')]:
            with patch.object(report,'utcnow',return_value=NOW),patch.object(cutctl,'collect_endpoints',return_value=endpoints),patch.object(cutctl,'_kernel_state',side_effect=[before,(True,'new')]),patch.object(cutctl,'apply_rules') as apply,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cutctl.reconcile(),0)
            apply.assert_called_once_with('cut',endpoints)

    def test_billing_rollover_restores_even_without_collector(self):
        desired={'want':'cut','armed':True,'period_key':'2026-08-01T00:00:00','reset_day':1,'reset_time':'00:00:00','reset_set':True,'cap':1000}
        util.save_json(cut.desired_path(),desired)
        with patch.object(report,'utcnow',return_value=NOW),patch.object(cutctl,'_kernel_state',side_effect=[(True,'old'),(False,'')]),patch.object(cutctl,'apply_rules') as apply,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cutctl.reconcile(),0)
        apply.assert_called_once_with('pass',{})

    def test_invalid_cutoff_input_never_removes_rules(self):
        util.save_json(cut.desired_path(),{'want':'invalid'})
        with patch.object(cutctl,'apply_rules') as apply,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cutctl.reconcile(),1)
        apply.assert_not_called()

    def test_hub_rules_preserve_response_and_block_forwarded_public_flows(self):
        endpoints={'hub_port':8788,'hub4':[],'hub6':[],'tg4':[],'tg6':[]}
        with patch.object(cutctl,'is_hub',return_value=True),patch.object(cutctl,'ssh_ports',return_value=[22]),patch.object(cutctl,'iface_exists',return_value=False),patch.object(cutctl,'append_tailscale_underlay'):
            rules=cutctl.build_nft(True,endpoints)
        output=rules.split('chain output {')[1].split('chain forward {')[0]
        self.assertIn('tcp sport 8788 ct state established accept',output)
        self.assertNotIn('ct state established,related accept',rules.split('chain forward {')[1])


if __name__=='__main__':unittest.main()
