"""Windows process identity, original locks, read-only queue and WMI launch."""
from datetime import datetime
import json,hashlib,os
from pathlib import Path
import shutil
import subprocess
import sys

from .continuation_manager import atomic_json,read_json
from .continuation_profiles import SOURCES,DOMAINS,profile
from .source_cadence import LOCAL_REFRESH_SOURCES,TRAVERSED,_root_scope,prepare_refresh_plan,local_refresh_sources


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

    def plan_completed_refresh(self,role,entry,*,manifest_id,previous,as_of):
        """Plan only after process admission and while holding the original lock."""
        def deferred(status):return {'status':status,'checkpoint':previous,'coverage_complete':False}
        if role not in LOCAL_REFRESH_SOURCES:raise ValueError('role has no local completed refresh authority')
        if not self.ready:return deferred('inventory_unconfirmed')
        if entry.get('operator_stop_path') and read_json(entry['operator_stop_path']).get('operator_stop') is True:
            return deferred('operator_stopped')
        if any(matches_role(p.get('CommandLine',''),role) for p in self.processes.values()):return deferred('existing_owner')
        if not self.free_space_ok():return deferred('storage_deferred')
        import msvcrt
        root=profile(self.workspace,role,entry).root
        lock=root/(role+'.runtime.lock')
        with lock.open('a+b') as handle:
            if handle.seek(0,2)==0:handle.write(b'0');handle.flush()
            handle.seek(0)
            try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:return deferred('domain_lock_held')
            try:
                # Refresh the process inventory while admission is fenced by
                # the original source mutex, including unregistered collectors.
                self.refresh()
                current=read_json(self.workspace/'artifacts/kr-full-workers.json')[
                    'results' if role in {'prices','fundamentals'} else 'auxiliary'][role]
                if current['runtime']!=entry['runtime']:return deferred('existing_owner')
                if current.get('operator_stop_path') and read_json(current['operator_stop_path']).get('operator_stop') is True:
                    return deferred('operator_stopped')
                if any(matches_role(p.get('CommandLine',''),role) for p in self.processes.values()):return deferred('existing_owner')
                return self._plan_completed_refresh_locked(role,manifest_id,previous,as_of)
            finally:
                handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)

    def _plan_completed_refresh_locked(self,role,manifest_id,previous,as_of):
        policy_path=self.workspace/'artifacts/source-ownership.json'
        ownership=read_json(policy_path) if policy_path.exists() else None
        sources=local_refresh_sources(role,ownership=ownership,manifest_id=manifest_id,previous=previous)
        if not sources:
            # Completed wrapper receipts can precede queued continuations.
            # Drain already planned work even after new refresh admission moves.
            pending=self.runnable(role)
            status='ready' if pending is True else 'remote_owned' if pending is False else 'queue_state_unconfirmed'
            return {'status':status,'checkpoint':previous,'coverage_complete':False}
        from sqlalchemy import create_engine,select,text,func
        from backtest_data.config import Settings
        from backtest_data.db import transaction
        from backtest_data.ingestion.models import CollectionJob,TargetUniverseManifest
        from backtest_data.ingestion.jobs import plan_job
        from backtest_data.remote.source_handlers import full_manifest_scope
        from backtest_data.remote_updates import load_update_context
        if self.engine is None:self.engine=create_engine(Settings.from_env().database_url,connect_args={'connect_timeout':5})
        with transaction(self.engine) as session:
            session.execute(text("SET LOCAL statement_timeout='10s'"));session.execute(text("SET LOCAL lock_timeout='2s'"))
            target=session.get(TargetUniverseManifest,manifest_id);full_manifest_scope(target)
            members,scope,issuers,mappings=load_update_context(session,target,enabled_sources=sources)
            lane=select(CollectionJob.id).where(CollectionJob.source.in_(sources),CollectionJob.domain.in_(DOMAINS[role]))
            outstanding=session.scalar(lane.where(CollectionJob.status.in_(('pending','retry','running'))).limit(1)) is not None
            failed=session.scalar(lane.where(CollectionJob.status=='failed').limit(1)) is not None
            original=list(session.scalars(select(CollectionJob).where(CollectionJob.source.in_(sources),
                CollectionJob.domain.in_(DOMAINS[role]),CollectionJob.request['manifest_id'].astext==manifest_id,
                CollectionJob.request['output_start'].astext==scope['output_start'],
                CollectionJob.request['output_end'].astext==scope['output_end'],
                ~CollectionJob.request.op('?')('refresh_policy'))))
            evidence=list(original)
            for source in sources:
                domain,expected=_root_scope(members,issuers,source)
                cutoff=CollectionJob.request['output_end'].astext
                refresh=select(CollectionJob).where(CollectionJob.source==source,CollectionJob.domain==domain,
                    CollectionJob.entity_id.in_(expected),CollectionJob.request['manifest_id'].astext==manifest_id,
                    CollectionJob.request.op('?')('refresh_policy'),CollectionJob.status!='superseded',
                    CollectionJob.request['original_backfill_start'].astext==scope['output_start'],
                    CollectionJob.request['original_backfill_end'].astext==scope['output_end'])
                latest=session.scalar(refresh.with_only_columns(cutoff).group_by(cutoff).having(
                    func.count(func.distinct(CollectionJob.entity_id))==len(expected),
                    func.bool_and(CollectionJob.status.in_(TRAVERSED))).order_by(cutoff.desc()).limit(1))
                if latest:evidence.extend(session.scalars(refresh.where(cutoff==latest)))
            if failed:return {'status':'original_pass_unconfirmed','checkpoint':previous,'coverage_complete':False}
            in_planning=previous is not None and previous.get('status')=='planning'
            result=prepare_refresh_plan(members,issuers,evidence,manifest_id=manifest_id,scope=scope,
                enabled_sources=sources,as_of=as_of,previous=previous if in_planning else None,mappings=mappings)
            if result['status']=='original_pass_unconfirmed':return {key:value for key,value in result.items() if key!='jobs'}
            if outstanding and not in_planning:
                # Existing refresh retries are admitted at any clock time.
                return {'status':'ready','checkpoint':previous,'proofs':result['proofs'],'coverage_complete':False}
            for specification in result.pop('jobs'):plan_job(session,**specification)
        # Return the cursor only after the whole bounded planning commit.
        return result

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
