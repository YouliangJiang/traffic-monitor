#!/usr/bin/env python3
"""SSH deployment with private stdin credentials, atomic releases, and rollback."""
from __future__ import annotations
import argparse
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parent


def ssh(target, command, payload=None, timeout=120):
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12', target, command], input=payload, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-1000:] or 'remote command failed')
    return result.stdout


def python(target, script, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    return json.loads(ssh(target, 'sudo -n python3 -c ' + shlex.quote(script), data))


READ_INFO = '''from pathlib import Path
import json
config={}
for line in (Path('/etc/traffic-monitor.env').read_text().splitlines() if Path('/etc/traffic-monitor.env').exists() else []):
 if line and not line.startswith('#') and '=' in line:
  key,value=line.split('=',1);config[key]=value.strip().strip('"').strip("'")
print(json.dumps({key:config.get(key,'') for key in ['ROLE','NODE_NAME','FLEET_HUB_URL','TRAFFIC_IFACE']}))
'''

BACKUP = '''from pathlib import Path
import json,tempfile,shutil,subprocess
root=Path('/var/backups/traffic-monitor');root.mkdir(parents=True,exist_ok=True,mode=0o700)
backup=Path(tempfile.mkdtemp(prefix='release-',dir=root));backup.chmod(0o700)
if Path('/opt/traffic-monitor').exists():shutil.copytree('/opt/traffic-monitor',backup/'code',ignore=shutil.ignore_patterns('__pycache__'))
for name in ['traffic-monitor.env','traffic-monitor-agents.json']:
 p=Path('/etc')/name
 if p.is_file():shutil.copy2(p,backup/name)
units=backup/'units';units.mkdir()
for p in Path('/etc/systemd/system').glob('traffic-*'):
 if p.is_file():shutil.copy2(p,units/p.name)
extra=backup/'journal';extra.mkdir()
for name,path in [('config','/etc/systemd/journald@traffic-security.conf'),('limits','/etc/systemd/system/systemd-journald@traffic-security.service.d/limits.conf')]:
 if Path(path).is_file():shutil.copy2(path,extra/name)
print(json.dumps({'backup':str(backup)}))
'''

UPDATE_ENV = '''from pathlib import Path
import json,os,tempfile,sys
request=json.load(sys.stdin);p=Path('/etc/traffic-monitor.env')
if p.is_symlink():raise RuntimeError('environment file must not be a symlink')
config={}
if p.exists():
 for line in p.read_text().splitlines():
  if line and not line.startswith('#') and '=' in line:
   k,v=line.split('=',1);config[k]=v
config.update(request)
fd,name=tempfile.mkstemp(prefix='.traffic-env-',dir='/etc')
with os.fdopen(fd,'w') as output:
 output.write(''.join(k+'='+str(v)+'\\n' for k,v in sorted(config.items()) if v))
 output.flush();os.fsync(output.fileno())
os.replace(name,p);os.chmod(p,0o600)
print(json.dumps({'updated':True}))
'''

ROLLBACK = '''from pathlib import Path
import json,sys,subprocess,shutil,os,tempfile,grp
request=json.load(sys.stdin);backup=Path(request['backup'])
if backup.parent!=Path('/var/backups/traffic-monitor'):raise RuntimeError('invalid backup path')
subprocess.run(['systemctl','stop','traffic-hub.service','traffic-bot.service','traffic-agent.service','traffic-cut.path','traffic-cut.timer','traffic-ipdata.service','traffic-ipdata.timer','traffic-maintenance.timer','traffic-maintenance.service','traffic-journal-clean.service'],check=False,capture_output=True)
for name,path in [('config','/etc/systemd/journald@traffic-security.conf'),('limits','/etc/systemd/system/systemd-journald@traffic-security.service.d/limits.conf')]:
 original=backup/'journal'/name;destination=Path(path)
 if original.is_file():destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(original,destination)
 elif destination.is_file():destination.unlink()
if not (backup/'units'/'traffic-maintenance.timer').exists():
 subprocess.run(['systemctl','disable','traffic-maintenance.timer'],check=False,capture_output=True)
for p in Path('/etc/systemd/system').glob('traffic-*'):
 if not (backup/'units'/p.name).exists() and p.is_file():p.unlink()
if not (backup/'code').exists():
 for p in Path('/etc/systemd/system').glob('traffic-*'):
  if not (backup/'units'/p.name).exists() and p.is_file():p.unlink()
 for name in ['traffic-monitor.env','traffic-monitor-agents.json']:
  original=backup/name;destination=Path('/etc')/name
  if original.exists():shutil.copy2(original,destination)
  elif destination.exists():destination.unlink()
 current=Path('/opt/traffic-monitor')
 if current.is_symlink():current.unlink()
 subprocess.run(['systemctl','daemon-reload'],check=True)
 subprocess.run(['systemctl','try-restart','systemd-journald@traffic-security.service'],check=False)
 print(json.dumps({'rolled_back':True}));raise SystemExit(0)
release=Path('/opt/traffic-monitor-releases')/('rollback-'+backup.name)
if not release.exists():shutil.copytree(backup/'code',release)
release.chmod(0o755)
link=Path('/opt/.traffic-monitor-rollback-'+backup.name)
link.symlink_to(release);os.replace(link,'/opt/traffic-monitor')
for name in ['traffic-monitor.env','traffic-monitor-agents.json']:
 p=backup/name
 if p.exists():shutil.copy2(p,Path('/etc')/name)
for p in (backup/'units').iterdir():shutil.copy2(p,Path('/etc/systemd/system')/p.name)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','try-restart','systemd-journald@traffic-security.service'],check=False)
role=request['role'];units=['traffic-hub.service','traffic-bot.service','traffic-monitor.timer'] if role=='hub' else ['traffic-agent.service']
if role=='hub' and (backup/'units'/'traffic-ipdata.timer').is_file():units.append('traffic-ipdata.timer')
if (backup/'units'/'traffic-maintenance.timer').is_file():units.append('traffic-maintenance.timer')
subprocess.run(['systemctl','restart']+units+['traffic-cut.path','traffic-cut.timer'],check=True)
print(json.dumps({'rolled_back':True}))
'''

HEALTH = '''from pathlib import Path
from contextlib import closing
import json,os,pwd,subprocess,sys,sqlite3,time
request=json.load(sys.stdin);role=request['role'];code=Path('/opt/traffic-monitor')
config={}
for line in Path('/etc/traffic-monitor.env').read_text().splitlines():
 if line and not line.startswith('#') and '=' in line:
  k,v=line.split('=',1);config[k]=v.strip().strip('"').strip("'")
os.environ.update(config);sys.path.insert(0,str(code))
owner=pwd.getpwnam('trafficmon');os.initgroups(owner.pw_name,owner.pw_gid);os.setgid(owner.pw_gid);os.setuid(owner.pw_uid)
import report,util
units=(['traffic-hub.service','traffic-bot.service','traffic-ipdata.timer'] if role=='hub' else ['traffic-agent.service'])+['traffic-maintenance.timer','systemd-journald@traffic-security.service']
active=all(subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0 for unit in units)
ledger=Path('/var/lib/traffic-monitor/traffic.sqlite3');fresh=False
if ledger.exists():
 with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as db:
  row=db.execute('SELECT value FROM metadata WHERE id=1').fetchone()
  fresh=bool(row and time.time()-report.parse_iso_datetime(json.loads(row[0])['last_ts']).timestamp()<60)
api=True
if role=='hub':
 try:api=util.http_json('GET',config['HUB_URL']+'/healthz',config['ADMIN_TOKEN'],timeout=5).get('ok') is True
 except Exception:api=False
retention=all(subprocess.check_output(['systemctl','show',unit,'--property=Result','--value'],text=True).strip()=='success' for unit in ['traffic-maintenance.service','traffic-journal-clean.service'])
state=util.load_json(Path('/var/lib/traffic-monitor/maintenance-status.json'))
retention=retention and state.get('retention_days')==30 and state.get('errors')=={} and time.time()-report.parse_iso_datetime(state['finished_at']).timestamp()<600
print(json.dumps({'ok':active and fresh and api and retention,'role':role,'node':config['NODE_NAME'],'active':active,'ledger_fresh':fresh,'api_ready':api,'retention_ready':retention,'release':(code/'release-id').read_text().strip()}))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('target', nargs='?')
    parser.add_argument('--role',choices=['hub','agent'])
    parser.add_argument('--hub')
    parser.add_argument('--hub-host')
    parser.add_argument('--name')
    parser.add_argument('--cap')
    parser.add_argument('--reset')
    parser.add_argument('--iface')
    parser.add_argument('--security-provider', choices=['xray_honeypot','off'])
    args=parser.parse_args()
    for name in ['READ_INFO','BACKUP','UPDATE_ENV','ROLLBACK','HEALTH']:
        compile(globals()[name], name, 'exec')
    settings={}
    local=ROOT/'deploy.local'
    if local.exists():
        for line in local.read_text().splitlines():
            if line and not line.startswith('#') and '=' in line:
                key,value=line.split('=',1);settings[key]=value
    target=args.target or settings.get('SSH_TARGET')
    if not target:parser.error('an SSH target is required')
    info=python(target,READ_INFO)
    role=args.role or info['ROLE'] or 'hub'
    if role not in {'hub','agent'}:parser.error('role must be hub or agent')
    node=args.name or info['NODE_NAME'] or settings.get('NODE_NAME') or 'my-node'
    hub=args.hub or settings.get('HUB_URL') or info['FLEET_HUB_URL'] or 'https://127.0.0.1:8788'
    iface=args.iface or info['TRAFFIC_IFACE'] or 'eth0'
    backup=python(target,BACKUP)['backup']
    stage=None
    try:
        stage=ssh(target,'sudo -n mktemp -d /tmp/traffic-monitor.XXXXXXXX').decode().strip()
        if not stage.startswith('/tmp/traffic-monitor.') or '/' in stage[len('/tmp/traffic-monitor.'):]:raise RuntimeError('invalid staging directory')
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w:gz') as bundle:
            for path in sorted(ROOT.glob('*.py'))+[ROOT/'install-host.sh',ROOT/'locales',ROOT/'systemd',ROOT/'data']:
                bundle.add(path,arcname=path.name,filter=lambda entry:None if '__pycache__' in entry.name else entry)
        ssh(target,'sudo -n tar -xzf - -C '+shlex.quote(stage),archive.getvalue())
        updates={key:settings[key] for key in ['TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','ADMIN_TOKEN','AGENT_TOKEN'] if settings.get(key)}
        if args.security_provider:
            updates['SECURITY_PROVIDER'] = '' if args.security_provider == 'off' else args.security_provider
            if args.security_provider != 'off':
                updates.update(SECURITY_EVENT_LOG='/var/log/xray-honeypot/events.jsonl', SECURITY_STATUS_FILE='/var/log/xray-honeypot/status.json')
        if updates:python(target,UPDATE_ENV,updates)
        if role=='agent':
            hub_host=args.hub_host or settings.get('HUB_HOST')
            if hub_host:
                command=['sudo','-n','python3','/opt/traffic-monitor/enroll-agent.py','--name',node,'--iface',iface,'--credentials']
                if args.cap:command+=['--cap',args.cap]
                if args.reset:command+=['--reset',args.reset]
                credentials=json.loads(ssh(hub_host,shlex.join(command)))
                python(target,UPDATE_ENV,credentials)
                cert=ssh(hub_host,'sudo -n cat /var/lib/traffic-monitor/hub.crt')
                ssh(target,'sudo -n tee '+shlex.quote(stage+'/hub.crt')+' >/dev/null',cert)
        units=['traffic-hub.service','traffic-bot.service'] if role=='hub' else ['traffic-agent.service']
        ssh(target,'sudo -n systemctl stop '+shlex.join(units+['traffic-cut.path','traffic-cut.timer']))
        command=['sudo','-n','bash',stage+'/install-host.sh','--role',role,'--name',node,'--iface',iface]
        if role=='agent':command+=['--hub',hub]
        if args.cap:command+=['--cap',args.cap]
        if args.reset:command+=['--reset',args.reset]
        ssh(target,shlex.join(command))
        for attempt in range(20):
            result=python(target,HEALTH,{'role':role})
            if result['ok']:
                print(json.dumps(dict(result,backup=backup),ensure_ascii=False))
                return
            time.sleep(2)
        raise RuntimeError('deployment readiness check did not pass')
    except Exception:
        python(target,ROLLBACK,{'backup':backup,'role':info['ROLE'] or role})
        raise
    finally:
        if stage:
            ssh(target,'sudo -n rm -rf -- '+shlex.quote(stage))


if __name__=='__main__':
    main()
