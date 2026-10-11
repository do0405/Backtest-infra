"""Windows process identity, original locks, read-only queue and WMI launch."""
from datetime import datetime
import json,hashlib,os
from pathlib import Path
import shutil
import subprocess
import sys

from .continuation_manager import atomic_json,read_json
from .continuation_profiles import SOURCES,DOMAINS,profile


class WindowsHost:
    def __init__(self,workspace,helper,powershell):
        self.workspace=Path(workspace);self.helper=Path(helper);self.powershell=str(powershell)
        self.processes={};self.ready=False;self.engine=None
        self.explicit_database_url=os.environ.get('BACKTEST_DATABASE_URL')
        self.env_mtime=None

    def refresh(self):
        self.ready=False
        p=subprocess.run([self.powershell,'-NoProfile','-File',str(self.helper),'-Action','Snapshot',
            '-Workspace',str(self.workspace)],capture_output=True,text=True,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if p.returncode!=0:raise RuntimeError('process_inventory_unavailable')
        values=json.loads(p.stdout.lstrip('\ufeff'))
        if isinstance(values,dict):values=[values]
        self.processes={p['ProcessId']:p for p in values};self.ready=True

    def observe(self,entry,runtime):
        if not self.ready:return 'unknown'
        wrapper=self.processes.get(entry.get('pid'))
        if wrapper:
            saved=read_json(entry['process_identity_path'])
            if isinstance(saved,list):saved=next(p for p in saved if p['ProcessId']==entry['pid'])
            if not all(wrapper.get(k)==saved.get(k) for k in ('ProcessId','ParentProcessId','CreationDate','ExecutablePath','CommandLine')):
                return 'identity_mismatch'
            return 'active'
        child=self.processes.get(runtime.get('child_pid'))
        expected=runtime.get('current_command')
        if child:
            if expected and child['CommandLine'].strip()!=subprocess.list2cmdline(expected).strip():
                return 'identity_mismatch'
            born=datetime.fromisoformat(child['CreationDate'])
            reference=datetime.fromisoformat(runtime['child_spawned_at'])
            if abs((born-reference).total_seconds())>15:return 'identity_mismatch'
            return 'orphan'
        # Windows native venv children may survive a vanished wrapper/launcher.
        if any(p['ParentProcessId'] in {entry.get('pid'),runtime.get('child_pid')} for p in self.processes.values()):
            return 'orphan'
        role=entry.get('source_group') or runtime.get('domain') or Path(entry.get('runtime','')).name.split('.runtime')[0]
        if any(matches_role(p.get('CommandLine',''),role) for p in self.processes.values()):return 'orphan'
        return 'gone'

    def lock_released(self,role):
        import msvcrt
        p=profile(self.workspace,role)
        domains=[role]
        if role=='clean_completion':domains+=['schema_tail','coverage','sec_metadata','financial_completion','price_release','price_query_verification']
        if role=='metric_evaluation':domains+=['financial_metrics']
        try:
            for domain in domains:
                path=p.root/(domain+'.runtime.lock')
                if not path.exists():continue
                with path.open('r+b') as handle:
                    handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                    handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            return True
        except OSError:return False

    def free_space_ok(self):return shutil.disk_usage(self.workspace).free>=50*1024**3

    def configuration_marker(self,role):
        # Metadata only: no credential value or credential-content hash enters
        # runtime state. A file change permits one fresh native attempt.
        home=Path.home()
        files=[self.workspace/'.env',home/'KIS/config/kis_devlp.yaml',home/'OpenDART/config/opendart.yaml',
            home/'Alpaca/config/alpaca.yaml',home/'KRX/config/krx.yaml',home/'AlphaVantage/config/alphavantage.yaml']
        variables=('KIS_CONFIG_PATH','INVEST_PROTO_KIS_CONFIG_PATH','OPENDART_CONFIG_PATH','OPEN_DART_CONFIG_PATH',
            'ALPACA_CONFIG_PATH','APCA_CONFIG_PATH','KRX_CONFIG_PATH','ALPHAVANTAGE_CONFIG_PATH','ALPHA_VANTAGE_CONFIG_PATH')
        files += [Path(os.environ[key]).expanduser() for key in variables if os.environ.get(key)]
        stamps=[]
        for path in files:
            try:s=path.stat();stamps.append((str(path),s.st_mtime_ns,s.st_size))
            except OSError:stamps.append((str(path),None,None))
        return hashlib.sha256(json.dumps(stamps).encode()).hexdigest()

    def runnable(self,role):
        if role not in SOURCES:return None
        from sqlalchemy import create_engine,text,bindparam
        from backtest_data.config import Settings
        try:
            env_file=self.workspace/'.env'
            mtime=env_file.stat().st_mtime_ns if env_file.exists() else None
            if self.env_mtime is not None and self.env_mtime!=mtime and self.explicit_database_url is None:
                if self.engine is not None:self.engine.dispose();self.engine=None
                os.environ.pop('BACKTEST_DATABASE_URL',None)
            self.env_mtime=mtime
            if self.engine is None:self.engine=create_engine(Settings.from_env().database_url,connect_args={'connect_timeout':5})
            with self.engine.begin() as c:
                c.execute(text('SET TRANSACTION READ ONLY'));c.execute(text("SET LOCAL statement_timeout='10s'"))
                query=text("""SELECT id FROM collection_job WHERE source IN :sources AND domain IN :domains
                    AND status IN ('pending','retry','running') LIMIT 1""").bindparams(
                    bindparam('sources',expanding=True),bindparam('domains',expanding=True))
                return c.execute(query,{'sources':SOURCES[role],'domains':DOMAINS[role]}).first() is not None
        except Exception:return None

    def launch(self,role,run):
        meta=read_json(self.workspace/'artifacts/kr-full-workers.json')
        entry=meta['results' if role in ('prices','fundamentals') else 'auxiliary'][role]
        p=profile(self.workspace,role,entry)
        # Original .venv child commands stay in the existing wrapper. This base
        # Python is detached by WMI rather than a tool-owned subprocess job.
        base=sys._base_executable
        command=subprocess.list2cmdline([base,str(p.wrapper),*p.flags,'--run-directory',str(run)])
        plan=run/'launch-plan.json';receipt=run/'WMI-launch.json';identity=run/'initial-processes.json'
        atomic_json(plan,{'CommandLine':command,'ReceiptPath':str(receipt),'IdentityPath':str(identity)})
        try:
            subprocess.run([self.powershell,'-NoProfile','-File',str(self.helper),'-Action','Launch',
                '-Workspace',str(self.workspace),'-PlanPath',str(plan)],capture_output=True,text=True,timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            pass
        # A receipt on disk resolves a response timeout without a second Create.
        if receipt.exists():
            value=read_json(receipt)
            if value.get('ReturnValue')==0 and (not identity.exists() or not read_json(identity)):
                raise RuntimeError('WMI_identity_receipt_unavailable')
            value['identity_path']=str(identity);return value
        raise RuntimeError('WMI_launch_receipt_unavailable')


def matches_role(command,role):
    command=command.lower()
    if f'--role {role}' in command and 'collector-worker.py' in command:return True
    if role in ('prices','alpaca_prices') and 'backtest_data collect' in command and '--domain prices' in command:
        return '--source' not in command or ('--source alpaca' in command if role=='alpaca_prices' else '--source kis' in command)
    if role=='fundamentals':return 'backtest_data collect' in command and '--domain fundamentals' in command
    patterns={'krx_quotes':('backtest_data krx collect','krx-plan-and-collect.py'),
        'public_pit':('backtest_data publication verify',),
        'daily_publication':('backtest_data publication-daily','--kind daily_publication'),
        'supplier_prices':('backtest_data supplier-prices','--kind supplier_prices'),
        'vwap_inputs':('backtest_data vwap-inputs',),
        'supplemental_sources':('backtest_data.supplemental.collector',),
        'clean_completion':('continue-after-index.py','--domain clean_completion'),
        'metric_evaluation':('evaluate-metrics-after-completion.py','--domain metric_evaluation')}
    return any(term in command for term in patterns.get(role,()))
