"""Bounded-memory benchmark using synthetic SQLite intervals, never live NICs."""
from datetime import datetime,timedelta,timezone
import gc,json,os,resource,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path
from unittest.mock import patch
source=Path(sys.argv[1]).resolve();sys.path.insert(0,str(source))
import counters
if len(sys.argv)>2 and sys.argv[2]=='worker':
 os.environ['TRAFFIC_MONITOR_STATE_DIR']=sys.argv[3]
 now=datetime(2026,9,30,12,tzinfo=timezone.utc)
 started=time.monotonic()
 with patch.object(counters,'_iface_bytes',return_value=(1000,1000)),patch.object(counters,'_boot_id',return_value='fake'):
  counters.record_sample('eth0',now)
 counters.sum_between(now-timedelta(days=30),now)
 counters.sum_between(now.replace(hour=0),now)
 counters.day_rows(last_n=4)
 unit=1048576 if sys.platform=='darwin' else 1024
 print(json.dumps({'python':sys.version.split()[0],'peak_mib':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/unit,1),'seconds':round(time.monotonic()-started,3)}))
 raise SystemExit(0)
for days in [90,400]:
 with tempfile.TemporaryDirectory(prefix='traffic-monitor-bench-') as directory:
  os.environ['TRAFFIC_MONITOR_STATE_DIR']=directory
  now=datetime(2026,9,30,12,tzinfo=timezone.utc);end=int(now.timestamp()*1000000);begin=end-days*86400*1000000
  with counters._database() as db:
   db.execute('BEGIN IMMEDIATE')
   db.executemany('INSERT INTO samples VALUES(?,?,?,?)',((begin+(i+1)*20000000,begin+i*20000000,5000000,5000000) for i in range(days*4320)))
   db.execute('INSERT INTO metadata VALUES(1,?)',(json.dumps({'iface':'eth0','boot_id':'fake','last_rx':1000,'last_tx':1000,'last_ts':now.isoformat()}),))
   db.execute('COMMIT')
  result=subprocess.run([sys.executable,__file__,str(source),'worker',directory],capture_output=True,text=True,check=True)
  metrics=json.loads(result.stdout);metrics.update(days=days,intervals=days*4320,service_limit_mib=96)
  print(json.dumps(metrics),flush=True)
