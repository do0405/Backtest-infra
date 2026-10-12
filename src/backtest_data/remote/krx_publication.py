"""Bounded KRX delivery controls; original source SHA stays in exact provenance."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import quote
from .github_http import GhAPI, Response
from .paced_publication import _Publisher, publish_release, publication_assets, PublicationBudgetExceeded

MAX_REQUESTS = 64

class PublicationFailure(RuntimeError):
    """Only safe primitive diagnostics can cross the runner log boundary."""
    def __init__(self, diagnostic):
        phases={'metadata','run_binding','publication','readback'}
        reasons={'authorization','rate_limit','network','server','request_rejected','proof_conflict','deadline','request_budget'}
        self._diagnostic={'phase':diagnostic.get('phase') if diagnostic.get('phase') in phases else 'publication',
            'reason':diagnostic.get('reason') if diagnostic.get('reason') in reasons else 'proof_conflict',
            'http_status':diagnostic.get('http_status') if type(diagnostic.get('http_status')) is int and 0<=diagnostic['http_status']<=599 else 0,
            'elapsed_seconds':_number(diagnostic.get('elapsed_seconds'),7200),
            'request_count':_number(diagnostic.get('request_count'),64),
            'retry_after_seconds':_number(diagnostic.get('retry_after_seconds'),86400),
            'rate_remaining':_number(diagnostic.get('rate_remaining'),10**9),
            'rate_reset_epoch':_number(diagnostic.get('rate_reset_epoch'),4102444800)}
        super().__init__('KRX publication failed: '+self._diagnostic['reason'])
    def diagnostic(self):return dict(self._diagnostic)

def _number(value, maximum):
    if isinstance(value,bool):return None
    try:result=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return result if math.isfinite(result) and 0<=result<=maximum else None

def _reason(response):
    message=response.data.get('message','') if isinstance(response.data,dict) else ''
    message=message.lower() if isinstance(message,str) else ''
    if response.status in {401,403} and any(s in message for s in ('resource not accessible','bad credentials','requires authentication','permission denied')):return 'authorization'
    if response.status==429 or response.status==403 and ('retry-after' in response.headers or response.headers.get('x-ratelimit-remaining')=='0' or any(s in message for s in ('secondary rate limit','api rate limit exceeded'))):return 'rate_limit'
    if response.status in {401,403}:return 'authorization'
    if response.status==0:return 'network'
    if 500<=response.status<600:return 'server'
    return 'request_rejected'

class BoundedGhAPI:
    """One monotonic deadline and REST cap cover retries, pacing and readback."""
    def __init__(self,gh,*,budget_seconds=2400,max_requests=MAX_REQUESTS,clock=time.monotonic,wall_clock=time.time,sleep=time.sleep,transport=None):
        if type(budget_seconds) not in {int,float} or not math.isfinite(budget_seconds) or not 0<budget_seconds<=7200 or type(max_requests) is not int or not 1<=max_requests<=MAX_REQUESTS:raise ValueError('bounded KRX publication required')
        self.gh=str(gh);self.clock=clock;self.wall_clock=wall_clock;self.sleeper=sleep;self.transport=transport
        self.started=clock();self.deadline=self.started+budget_seconds;self.max_requests=max_requests;self.count=0;self.phase='metadata';self.last=Response(0,{},None);self.reason=None;self._release_binding=None
    def bind_release(self,repo,tag,notes):
        self._release_binding=(f'repos/{repo}/releases/tags/'+quote(tag,safe=''),f'repos/{repo}/releases',tag,notes)
    def failure(self,reason=None):
        response=self.last
        return PublicationFailure({'phase':self.phase,'elapsed_seconds':round(max(0,min(7200,self.clock()-self.started)),2),'request_count':self.count,'http_status':response.status if type(response.status) is int and 0<=response.status<=599 else 0,'retry_after_seconds':_number(response.headers.get('retry-after'),86400),'rate_remaining':_number(response.headers.get('x-ratelimit-remaining'),10**9),'rate_reset_epoch':_number(response.headers.get('x-ratelimit-reset'),4102444800),'reason':reason or self.reason or 'proof_conflict'})
    def remaining(self):
        value=self.deadline-self.clock()
        if value<=0:raise self.failure('deadline')
        return value
    def sleep(self,seconds):
        if not math.isfinite(seconds) or seconds<0:raise self.failure('proof_conflict')
        if seconds>=self.remaining():raise self.failure('deadline')
        self.sleeper(seconds);self.remaining()
    def request(self,method,url,*,body=None,input_file=None,raw=False):
        remaining=self.remaining()
        if self.count>=self.max_requests:raise self.failure('request_budget')
        self.count+=1
        if self.transport is not None:
            response=self.transport.request(method,url,body=body,input_file=input_file,raw=raw,timeout=min(300,remaining))
        else:response=self._http(method,url,body=body,input_file=input_file,raw=raw,timeout=min(300,remaining))
        self.last=response;self.reason=None if 200<=response.status<300 else _reason(response)
        self.remaining()
        if self._release_binding is not None and 200<=response.status<300:
            lookup,create,tag,notes=self._release_binding
            if (method=='GET' and url==lookup or method=='POST' and url==create) and (
                    not isinstance(response.data,dict) or response.data.get('tag_name')!=tag or response.data.get('body')!=notes):
                raise self.failure('proof_conflict')
        return response
    def _http(self,method,url,*,body,input_file,raw,timeout):
        try:
            return GhAPI(self.gh).request(method,url,body=body,input_file=input_file,
                                         raw=raw,timeout=min(timeout,self.remaining()))
        except ValueError:
            raise self.failure('proof_conflict') from None
    def get(self,url,*,raw=False):
        # Reads share the same clock and request cap, with bounded rate waits.
        publisher=_Publisher(self,clock=self.clock,wall_clock=self.wall_clock,sleep=self.sleep,budget_seconds=self.remaining(),max_retries=3)
        class Raw:
            def request(inner,method,url,**kwargs):return self.request(method,url,raw=True,**kwargs)
        if raw:publisher.api=Raw()
        try:return publisher.request('GET',url)
        except PublicationFailure:raise
        except PublicationBudgetExceeded:raise self.failure('deadline') from None
        except Exception:raise self.failure() from None

def provenance_notes(notes,source_sha,run_id,run_attempt):
    if not isinstance(notes,str) or len(notes)>4096 or not re.fullmatch('[a-f0-9]{40}',str(source_sha)) or type(run_id) is not str or not re.fullmatch('[1-9][0-9]{0,29}',run_id) or type(run_attempt) is not int or not 1<=run_attempt<=1000:raise ValueError('original KRX run provenance required')
    return notes+'\n\n'+json.dumps({'source_sha':source_sha,'run_id':run_id,'run_attempt':run_attempt},sort_keys=True)

def publish_control(gh,repo,tag,paths,*,source_sha,run_id,run_attempt,title,notes,api=None,clock=time.monotonic,wall_clock=time.time,sleep=time.sleep,budget_seconds=2400,max_requests=MAX_REQUESTS,max_retries=3,readback_files=()):
    if isinstance(api,BoundedGhAPI):
        # Admission history, publication and readback consume one original cap
        # and deadline. Resuming the control never mints another allowance.
        bounded=api;clock=bounded.clock;wall_clock=bounded.wall_clock
    else:bounded=BoundedGhAPI(gh,budget_seconds=budget_seconds,max_requests=max_requests,clock=clock,wall_clock=wall_clock,sleep=sleep,transport=api)
    try:
        exact_notes=provenance_notes(notes,source_sha,run_id,run_attempt)
        if tag.startswith('quota-') and bounded.deadline-bounded.started>2400:raise ValueError('bounded quota publication required')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('repository binding')
        paths=list(paths)
        expected={name:{key:proof[key] for key in ('size','digest')}
                  for name,proof in publication_assets(paths,max_assets=3).items()}
        metadata=bounded.get(f'repos/{repo}')
        if not isinstance(metadata,dict) or metadata.get('default_branch')!='main':raise ValueError('default branch')
        bounded.phase='run_binding'
        current=bounded.get(f'repos/{repo}/actions/runs/{run_id}')
        if not isinstance(current,dict) or str(current.get('id'))!=run_id or current.get('run_attempt')!=run_attempt or current.get('head_sha')!=source_sha:raise ValueError('source run')
        bounded.phase='publication'
        bounded.bind_release(repo,tag,exact_notes)
        remaining=int(bounded.remaining())
        if remaining<1:raise bounded.failure('deadline')
        result=publish_release(gh,repo,tag,paths,target='main',control_target=True,title=title,notes=exact_notes,api=bounded,clock=clock,wall_clock=wall_clock,sleep=bounded.sleep,budget_seconds=remaining,max_retries=max_retries)
        bounded.phase='readback'
        release=bounded.get(f'repos/{repo}/releases/tags/'+quote(tag,safe=''))
        if not isinstance(release,dict) or release.get('id')!=result['release_id'] or release.get('tag_name')!=tag or release.get('draft') is not False or release.get('prerelease') is not False or release.get('target_commitish')!='main' or release.get('body')!=exact_notes:raise ValueError('published control conflict')
        published=datetime.fromisoformat(release['published_at'].replace('Z','+00:00'))
        if published.tzinfo is None or published.utcoffset() is None:raise ValueError('aware publication')
        actual={}
        for page in range(1,3):
            values=bounded.get(f"repos/{repo}/releases/{release['id']}/assets?per_page=100&page={page}")
            if not isinstance(values,list) or len(values)>100:raise ValueError('asset inventory')
            for value in values:
                if not isinstance(value,dict) or value.get('name') in actual or value.get('state')!='uploaded':raise ValueError('asset proof')
                actual[value['name']]={key:value.get(key) for key in ('size','digest')}
            if len(values)<100:break
        else:raise ValueError('complete inventory')
        if actual!=expected:raise ValueError('exact published asset set')
        received={}
        if not set(readback_files)<=set(expected):raise ValueError('readback binding')
        for name in readback_files:
            if expected[name]['size']>64*1024:raise ValueError('bounded control readback')
            record=next(value for value in values if value['name']==name)
            content=bounded.get(f"repos/{repo}/releases/assets/{record['id']}",raw=True)
            if not isinstance(content,bytes) or len(content)!=expected[name]['size'] or 'sha256:'+hashlib.sha256(content).hexdigest()!=expected[name]['digest']:raise ValueError('control readback conflict')
            received[name]=content
        bounded.remaining()
        return {**result,'release':release,'request_count':bounded.count,'readback_contents':received}
    except PublicationFailure:raise
    except PublicationBudgetExceeded:raise bounded.failure('deadline') from None
    except Exception:raise bounded.failure() from None


def hosted_job_seconds(run, *, source_sha, run_id, run_attempt, now):
    """The 240-minute host budget starts at the original Actions run clock."""
    if not isinstance(run,dict) or str(run.get('id'))!=run_id or run.get('run_attempt')!=run_attempt or run.get('head_sha')!=source_sha:
        raise ValueError('original hosted job binding required')
    try:start=datetime.fromisoformat(run['run_started_at'].replace('Z','+00:00'))
    except (KeyError,TypeError,AttributeError,ValueError):raise ValueError('aware hosted job clock required') from None
    if start.tzinfo is None or start.utcoffset() is None or now.tzinfo is None or now.utcoffset() is None or start>now:
        raise ValueError('aware ordered hosted job clock required')
    return max(0,int((start+timedelta(minutes=240)-now).total_seconds()))
