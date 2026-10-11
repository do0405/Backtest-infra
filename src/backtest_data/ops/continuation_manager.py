"""Adopt live workers, then renew bounded batches from existing checkpoints."""
from dataclasses import asdict
from datetime import datetime,timezone
import json,shutil
from pathlib import Path
import time
from uuid import uuid4

from .continuation_policy import CORE_ROLES,decide
from .continuation_profiles import ROLES,profile,section
from .db_recovery import AttemptResult
from .source_cadence import LOCAL_REFRESH_SOURCES


def read_json(path):
    path=Path(path)
    if path.stat().st_size>16*1024*1024:raise ValueError('status_file_size_limit')
    for attempt in range(3):
        try:return json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError,ValueError):
            if attempt==2:raise
            time.sleep(.05*(attempt+1))


def atomic_json(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.'+uuid4().hex+'.pending')
    temporary.write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')
    for attempt in range(3):
        try:temporary.replace(path);return
        except PermissionError:
            if attempt==2:raise
            time.sleep(.05*(attempt+1))


class ContinuationManager:
    def __init__(self,workspace,root,host,*,roles=ROLES,clock=time.time,refresh_manifest_id=None):
        self.workspace=Path(workspace);self.root=Path(root);self.host=host;self.roles=tuple(roles);self.clock=clock
        if refresh_manifest_id is not None and (not isinstance(refresh_manifest_id,str) or not refresh_manifest_id.strip()):
            raise ValueError('explicit refresh manifest identity required')
        self.refresh_manifest_id=refresh_manifest_id
        self.metadata=self.workspace/'artifacts/kr-full-workers.json';self.target=self.root/'manager.runtime.json'
        self.state=read_json(self.target) if self.target.exists() else {
            'runtime_schema_version':3,'status':'adopting','started_at':datetime.now(timezone.utc).isoformat(),
            'policy':'continuous_checkpoint_batches_v1','overall_budget_seconds':None,'retry_count_limit':None,
            'roles':{},'coverage_complete':False}

    def save(self):
        self.state['checked_at']=datetime.now(timezone.utc).isoformat();atomic_json(self.target,self.state)

    def tick(self):
        meta=read_json(self.metadata)
        for role in self.roles:
            try:self._tick_role(role,meta)
            except (FileNotFoundError,PermissionError,json.JSONDecodeError):
                self.state['roles'].setdefault(role,{})['status']='waiting_for_status_file'
            except Exception as exc:
                self.state['roles'].setdefault(role,{}).update(status='action_required',requires_user=True,
                    reason='manager_operation_error',error_type=type(exc).__name__)
        self.state['status']='collection_pass_complete' if all(self.state['roles'].get(r,{}).get('status')=='complete'
            for r in self.roles) else 'managing'
        self.save()

    def _tick_role(self,role,meta):
        entry=meta[section(role)][role];item=self.state['roles'].setdefault(role,{'generations':0,'failure_streak':0})
        if item.get('launch_uncertain'):return
        generation=entry['runtime']
        if item.get('entry_runtime')!=generation:
            item.update(entry_runtime=generation,next_launch_at=None,requires_user=False,status='observing')
            item.pop('refresh_dispatch_pending',None)
        elif item.get('status')=='operator_stopped':return
        elif item.get('status')=='complete' or item.get('refresh_dispatch_pending'):
            if self.refresh_manifest_id is not None and role in LOCAL_REFRESH_SOURCES:
                self._tick_completed_role(role,entry,item)
            return
        runtime_path=Path(entry['runtime'])
        # Allow the newly launched wrapper time to create its runtime. The
        # committed launch journal prevents another launch in this window.
        if not runtime_path.exists():
            observation=self.host.observe(entry,{})
            if observation=='gone' and self.clock()-item.setdefault('runtime_missing_since',self.clock())>90:
                item.update(status='action_required',requires_user=True,reason='wrapper_runtime_missing')
            else:item['status']='waiting_for_wrapper_runtime'
            return
        runtime=read_json(runtime_path);observation=self.host.observe(entry,runtime)
        if observation=='identity_mismatch':
            item.update(status='action_required',reason='process_identity_mismatch',requires_user=True);return
        if observation in ('active','orphan','unknown'):
            item.update(status='adopted_running' if observation=='active' else 'waiting_for_existing_child',
                requires_user=False,pid=entry.get('pid'));return
        if entry.get('operator_stop_path'):
            stop=read_json(entry['operator_stop_path'])
            if stop.get('operator_stop') is True:item.update(status='operator_stopped',requires_user=False);return
        if not self.host.free_space_ok():
            item.update(status='action_required',reason='storage_space_required',requires_user=True);return
        if not runtime.get('finished_at'):
            # A vanished process cannot be a concurrent writer. Keep every
            # saved source cursor and let the normal fencing acquire expired jobs.
            decision={'action':'resume','reason':'unexpected_process_exit','delay_seconds':60,'requires_user':False}
        else:
            try:receipt=read_json(entry['stdout'])
            except (OSError,ValueError):receipt=None
            result=AttemptResult(runtime.get('exit_code',3),receipt)
            runnable=self.host.runnable(role) if role in CORE_ROLES and isinstance(receipt,dict) and receipt.get('status')=='partial' else None
            nested=None
            if role=='clean_completion' and isinstance(receipt,dict) and receipt.get('failed_stage'):
                stage=receipt.get('stages',{}).get(receipt['failed_stage'],{})
                if stage.get('stdout') and stage.get('runtime'):
                    nested=AttemptResult(read_json(stage['runtime']).get('exit_code',3),read_json(stage['stdout']))
            dependency_active=role=='metric_evaluation' and self.state['roles'].get('clean_completion',{}).get('status') not in {'complete','operator_stopped'}
            decision=asdict(decide(role,result,runnable=runnable,failure_streak=item.get('failure_streak',0),
                nested_result=nested,dependency_active=dependency_active,storage_ready=True))
        if decision['reason']=='authentication_or_permission_required':
            marker=self.host.configuration_marker(role) if hasattr(self.host,'configuration_marker') else None
            if 'configuration_marker' not in item:
                item.update(configuration_marker=marker,next_access_probe_at=self.clock()+21600)
            if marker!=item['configuration_marker'] or self.clock()>=item['next_access_probe_at'] or item.get('auth_probe_ready'):
                item.update(configuration_marker=marker,next_access_probe_at=self.clock()+21600,auth_probe_ready=True)
                decision={'action':'resume','reason':'configuration_or_access_recheck','delay_seconds':5,'requires_user':False}
        item.update(reason=decision['reason'],requires_user=decision['requires_user'])
        action=decision['action']
        if action in ('complete','action_required','wait'):
            item['status']=action;item['next_launch_at']=None;return
        if not self.host.lock_released(role):item['status']='waiting_for_domain_lock';return
        if item.get('next_launch_at') is None:
            item.update(status='waiting_for_continuation',next_launch_at=self.clock()+decision['delay_seconds']);return
        if self.clock()<item['next_launch_at']:return
        self._launch(role,entry,item,decision)

    def _tick_completed_role(self,role,entry,item):
        if entry.get('operator_stop_path') and read_json(entry['operator_stop_path']).get('operator_stop') is True:
            item.update(status='operator_stopped',requires_user=False,refresh_dispatch_pending=False);return
        result=self.host.plan_completed_refresh(role,entry,manifest_id=self.refresh_manifest_id,
            previous=item.get('refresh_planning'),as_of=datetime.fromtimestamp(self.clock(),timezone.utc))
        status=result['status']
        if result.get('checkpoint') is not None:item['refresh_planning']=result['checkpoint']
        item.update(reason='completed_pass_'+status,requires_user=False,coverage_complete=False,
            original_traversal_proofs=result.get('proofs',item.get('original_traversal_proofs',{})),
            refresh_dispatch_pending=True)
        if status!='ready':
            item['status']={'up_to_date':'waiting_for_publication','planning':'planning_daily_refresh',
                'original_pass_unconfirmed':'waiting_for_original_pass_proof','existing_owner':'waiting_for_existing_child',
                'domain_lock_held':'waiting_for_domain_lock','operator_stopped':'operator_stopped',
                'inventory_unconfirmed':'waiting_for_host_inventory','storage_deferred':'waiting_for_storage'}.get(status,'action_required')
            item['next_launch_at']=None
            if status=='operator_stopped':item['refresh_dispatch_pending']=False
            if item['status']=='action_required':item['requires_user']=True
            return
        if not self.host.lock_released(role):item['status']='waiting_for_domain_lock';return
        if item.get('next_launch_at') is None:
            item.update(status='waiting_for_continuation',next_launch_at=self.clock()+5);return
        if self.clock()<item['next_launch_at']:return
        self._launch(role,entry,item,{'reason':'completed_daily_refresh_ready'})

    def _launch(self,role,entry,item,decision):
        p=profile(self.workspace,role,entry)
        name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-continuous-'+role+'-'+uuid4().hex[:8]
        run=p.root/'runs'/name;run.mkdir(parents=True)
        current=read_json(self.metadata)
        if current[section(role)][role]['runtime']!=entry['runtime']:return
        atomic_json(run/'previous-workers.json',current)
        state_path=self.workspace/'artifacts/collection-watch/state.json'
        if state_path.exists():shutil.copyfile(state_path,run/'previous-monitor-state.json')
        if entry.get('progress_path') and Path(entry['progress_path']).is_file():
            shutil.copyfile(entry['progress_path'],run/'previous-progress.json')
        intent={'role':role,'previous_runtime':entry['runtime'],'run_directory':str(run),'status':'launch_intent'}
        atomic_json(run/'continuation-intent.json',intent)
        # Persist uncertainty before dispatch; a host crash after Create must
        # never produce an unrecorded second launch on manager restart.
        item.update(status='launching',launch_uncertain=True,launch_intent_path=str(run/'continuation-intent.json'))
        self.save()
        try:
            launch=self.host.launch(role,run)
            if launch.get('ReturnValue')!=0:raise RuntimeError('WMI_launch_not_successful')
            atomic_json(run/'launch-receipt.json',launch)
            new_entry=dict(entry)
            new_entry.update(run_id=name,pid=launch['ProcessId'],run_directory=str(run),
                process_identity_path=launch['identity_path'],runtime=str(run/(role+'.runtime.json')),
                stdout=str(run/(role+'.stdout.json')),stderr=str(run/(role+'.stderr')),
                continuation_policy='continuous_checkpoint_batches_v1',previous_runtime=entry['runtime'])
            current=read_json(self.metadata)
            current[section(role)][role]=new_entry
            if role in ('prices','fundamentals'):current[role+'_pid']=launch['ProcessId']
            current['continuation_policy']='continuous_checkpoint_batches_v1'
            atomic_json(self.metadata,current)
            item.update(status='waiting_for_wrapper_runtime',launch_uncertain=False,requires_user=False,
                entry_runtime=new_entry['runtime'],next_launch_at=None,generations=item.get('generations',0)+1,
                failure_streak=0 if decision['reason'] in {'batch_budget_finished','planned_queue_remaining'} else item.get('failure_streak',0)+1)
            item.pop('auth_probe_ready',None)
            item.pop('refresh_dispatch_pending',None)
            intent['status']='metadata_registered';atomic_json(run/'continuation-intent.json',intent)
        except Exception as exc:
            item.update(status='action_required',requires_user=True,reason='launch_outcome_requires_review',error_type=type(exc).__name__)
        self.save()
