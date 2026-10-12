import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as N
import pytest

class Clock:
    def __init__(self):self.value=0.;self.waits=[]
    def now(self):return self.value
    def sleep(self,n):self.waits.append(n);self.value+=n

class Server:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.release=None;self.assets={};self.failure=None;self.lost=False;self.conflict=False
    def request(self,method,url,*,body=None,input_file=None,timeout=None,raw=False):
        self.calls.append((method,url,self.clock.now(),timeout))
        if self.failure:return self.failure(method,url)
        if url=='repos/a/b':return N(status=200,headers={},data={'default_branch':'main'})
        if '/actions/runs/' in url:return N(status=200,headers={},data={'id':17,'run_attempt':2,'head_sha':'a'*40})
        if '/releases/tags/' in url:
            value=dict(self.release) if self.release else None
            if value and self.conflict and not value['draft']:value['body']='secret conflict'
            return N(status=200 if value else 404,headers={},data=value)
        if method=='POST' and url.endswith('/releases'):
            self.release={'id':7,**body,'published_at':None,'upload_url':'https://uploads.github.com/repos/a/b/releases/7/assets{?name,label}'}
            return N(status=201,headers={},data=dict(self.release))
        if method=='GET' and '/assets?' in url:return N(status=200,headers={},data=list(self.assets.values()))
        if method=='POST' and '/assets?' in url:
            path=Path(input_file);item={'id':8,'name':path.name,'size':path.stat().st_size,'state':'uploaded','digest':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()}
            self.assets[path.name]=item
            if self.lost:self.lost=False;return N(status=502,headers={},data={'message':'PRIVATE_TOKEN'})
            return N(status=201,headers={},data=item)
        if method=='PATCH':
            self.release.update(body);self.release['published_at']='2026-10-12T01:00:00Z';return N(status=200,headers={},data=dict(self.release))
        raise AssertionError((method,url))

def publish(tmp_path,server,clock,**kwargs):
    from backtest_data.remote.krx_publication import publish_control
    path=tmp_path/'commit.json';path.write_bytes(b'original exact commit')
    return publish_control('gh','a/b','data-krx-test-000000001-r17-a2',[path],source_sha='a'*40,run_id='17',run_attempt=2,title='KRX',notes='original identities',api=server,clock=clock.now,wall_clock=lambda:100+clock.now(),sleep=clock.sleep,**kwargs)

def test_stale_source_sha_uses_main_with_exact_provenance_and_final_get(tmp_path):
    clock=Clock();server=Server(clock);result=publish(tmp_path,server,clock)
    assert result['published'] and server.release['target_commitish']=='main'
    assert json.loads(server.release['body'].split('\n\n')[-1])=={'source_sha':'a'*40,'run_id':'17','run_attempt':2}
    assert server.calls[-2][1].endswith('/tags/data-krx-test-000000001-r17-a2')
    assert '/assets?' in server.calls[-1][1]

def test_lost_ack_reconciles_same_original_asset(tmp_path):
    clock=Clock();server=Server(clock);server.lost=True
    assert publish(tmp_path,server,clock)['published']
    assert len([c for c in server.calls if c[0]=='POST' and '/assets?' in c[1]])==1
    assert len(server.assets)==1 and clock.value>=60

def test_final_get_conflict_cannot_claim_commit(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock);server.conflict=True
    with pytest.raises(PublicationFailure) as caught:publish(tmp_path,server,clock)
    assert caught.value.diagnostic()['reason']=='proof_conflict'
    assert 'secret' not in str(caught.value)

@pytest.mark.parametrize('status,headers,message,reason',[(401,{},'PRIVATE_TOKEN','authorization'),(403,{},'Resource not accessible PRIVATE_TOKEN','authorization'),(403,{'retry-after':'9999'},'secondary rate limit PRIVATE_TOKEN','deadline'),(429,{'retry-after':'9999'},'PRIVATE_TOKEN','deadline'),(0,{},'PRIVATE_TOKEN','deadline')])
def test_failure_classifies_safe_numeric_diagnostics(tmp_path,status,headers,message,reason):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock);server.failure=lambda *a:N(status=status,headers=headers,data={'message':message})
    with pytest.raises(PublicationFailure) as caught:publish(tmp_path,server,clock,budget_seconds=20,max_retries=0)
    value=caught.value.diagnostic();assert value['http_status']==status and value['reason']==reason
    assert 'PRIVATE_TOKEN' not in json.dumps(value) and 'PRIVATE_TOKEN' not in str(caught.value)
    assert len(server.calls)==1

def test_absolute_deadline_also_includes_http_time(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock)
    def response(*a):clock.value+=20;return N(status=200,headers={},data={'default_branch':'main'})
    server.failure=response
    with pytest.raises(PublicationFailure):publish(tmp_path,server,clock,budget_seconds=20)
    assert len(server.calls)==1 and server.calls[0][3]==20

def test_rest_cap_stops_before_next_io(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock)
    with pytest.raises(PublicationFailure) as caught:publish(tmp_path,server,clock,max_requests=3)
    assert len(server.calls)==3 and caught.value.diagnostic()['reason']=='request_budget'

def test_current_run_source_binding_mismatch_fails(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock);original=server.request
    def request(method,url,**kw):
        value=original(method,url,**kw)
        if '/actions/runs/' in url:value.data['head_sha']='b'*40
        return value
    server.request=request
    with pytest.raises(PublicationFailure):publish(tmp_path,server,clock)
    assert not any(c[0]!='GET' for c in server.calls)

def test_no_response_does_not_invent_http_401(monkeypatch):
    from backtest_data.remote import krx_publication as module
    from backtest_data.remote import github_http
    clock=Clock();monkeypatch.setattr(github_http.subprocess,'run',lambda *a,**k:N(stdout=b'',stderr=b'authentication PRIVATE_TOKEN HTTP 403',returncode=1))
    api=module.BoundedGhAPI('gh',clock=clock.now)
    assert api.request('GET','repos/a/b').status==0

def test_quota_publication_requires_receipt_download_before_admission(tmp_path,monkeypatch):
    from backtest_data.remote import quota
    from datetime import datetime,timedelta,timezone
    clock=Clock();server=Server(clock);payload={};original=server.request
    now=datetime(2026,10,12,1,tzinfo=timezone.utc)
    binding={'stream_id':'krx-test','manifest_id':'m','handoff_id':'h','bootstrap_sha256':'b'*64}
    receipt=quota.plan_allocation(binding,[],[],run_id='17',run_attempt=2,now=now,expires_at=now+timedelta(hours=2),budget_seconds=300)
    def request(method,url,**kwargs):
        if '/releases/assets/' in url:return N(status=200,headers={},data=payload['bytes'])
        if kwargs.get('input_file'):payload['bytes']=Path(kwargs['input_file']).read_bytes()
        return original(method,url,**kwargs)
    server.request=request
    from backtest_data.remote.krx_publication import publish_control
    monkeypatch.setattr(quota,'publish_control',lambda *a,**kw:publish_control(*a,**kw,api=server,clock=clock.now,wall_clock=lambda:100+clock.now(),sleep=clock.sleep))
    assert quota.publish_allocation('gh','a/b',receipt,tmp_path,target='main',source_sha='a'*40)==receipt
    assert payload['bytes']==(tmp_path/'quota-allocation.json').read_bytes()

@pytest.mark.parametrize('change',[{'draft':True},{'target_commitish':'old-main'},{'published_at':None},{'published_at':'bad'}])
def test_final_publication_proof_rejects_uncertain_controls(tmp_path,change):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock);original=server.request
    def request(method,url,**kwargs):
        result=original(method,url,**kwargs)
        if '/releases/tags/' in url and server.release and not server.release['draft']:result.data.update(change)
        return result
    server.request=request
    with pytest.raises(PublicationFailure):publish(tmp_path,server,clock)

def test_final_independent_asset_digest_conflict_is_rejected(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock);original=server.request
    def request(method,url,**kwargs):
        result=original(method,url,**kwargs)
        if method=='GET' and '/assets?' in url and server.release and not server.release['draft']:result.data=[{**v,'digest':'sha256:'+'f'*64} for v in result.data]
        return result
    server.request=request
    with pytest.raises(PublicationFailure):publish(tmp_path,server,clock)

def test_default_branch_must_be_main_before_any_mutation(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock);server.failure=lambda *a:N(status=200,headers={},data={'default_branch':'master'})
    with pytest.raises(PublicationFailure):publish(tmp_path,server,clock)
    assert len(server.calls)==1

def test_secondary_rate_retry_retains_pacing_then_succeeds(tmp_path):
    clock=Clock();server=Server(clock);original=server.request;once=False
    def request(method,url,**kwargs):
        nonlocal once
        if method=='POST' and '/assets?' in url and not once:
            once=True;return N(status=403,headers={'retry-after':'123'},data={'message':'secondary rate limit PRIVATE_TOKEN'})
        return original(method,url,**kwargs)
    server.request=request
    assert publish(tmp_path,server,clock)['published']
    assert clock.value>=125

def test_pacing_exhausts_absolute_budget_without_extra_io(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock)
    with pytest.raises(PublicationFailure):publish(tmp_path,server,clock,budget_seconds=2)
    assert not any(c[0]=='PATCH' for c in server.calls)
    assert server.release['draft']

def test_quota_publish_clock_outside_internal_window_never_authorizes(tmp_path,monkeypatch):
    from backtest_data.remote import quota
    from datetime import datetime,timedelta,timezone
    now=datetime(2026,10,12,1,tzinfo=timezone.utc)
    binding={'stream_id':'krx-test','manifest_id':'m','handoff_id':'h','bootstrap_sha256':'a'*64}
    value=quota.plan_allocation(binding,[],[],run_id='17',run_attempt=2,now=now,expires_at=now+timedelta(hours=2),budget_seconds=300)
    monkeypatch.setattr(quota,'publish_control',lambda *a,**k:{'release':{'published_at':'2026-10-12T03:00:01Z'},'readback_contents':{'quota-allocation.json':json.dumps(value).encode()}})
    with pytest.raises(ValueError):quota.publish_allocation('gh','a/b',value,tmp_path,target='a'*40)

def test_safe_failure_uses_only_whitelisted_primitives():
    from backtest_data.remote.krx_publication import PublicationFailure
    value=PublicationFailure({'phase':'PRIVATE_TOKEN','reason':'PRIVATE_TOKEN','http_status':'PRIVATE_TOKEN','elapsed_seconds':float('inf'),'request_count':10**9,'raw':'PRIVATE_TOKEN','rate_remaining':'PRIVATE_TOKEN'}).diagnostic()
    assert 'PRIVATE_TOKEN' not in json.dumps(value)
    assert set(value)=={'phase','reason','http_status','elapsed_seconds','request_count','retry_after_seconds','rate_remaining','rate_reset_epoch'}
    assert value['http_status']==0 and value['reason']=='proof_conflict'

def test_code_artifacts_keep_strict_sha_target(tmp_path):
    from backtest_data.remote.paced_publication import publish_release
    path=tmp_path/'code.enc';path.write_bytes(b'original code')
    clock=Clock();server=Server(clock)
    with pytest.raises(ValueError):publish_release('gh','a/b','code',[path],target='main',title='code',notes='code',api=server)
    assert not server.calls

def test_hosted_job_budget_uses_original_actions_clock():
    from backtest_data.remote.krx_publication import hosted_job_seconds
    from datetime import datetime,timezone
    run={'id':17,'run_attempt':2,'head_sha':'a'*40,'run_started_at':'2026-10-12T00:00:00Z'}
    assert hosted_job_seconds(run,source_sha='a'*40,run_id='17',run_attempt=2,now=datetime(2026,10,12,3,50,tzinfo=timezone.utc))==600
    with pytest.raises(ValueError):hosted_job_seconds({**run,'run_started_at':'2026-10-12T00:00:00'},source_sha='a'*40,run_id='17',run_attempt=2,now=datetime(2026,10,12,3,50,tzinfo=timezone.utc))

def test_spent_history_cap_cannot_mint_another_publication_budget(tmp_path):
    from backtest_data.remote.krx_publication import BoundedGhAPI,PublicationFailure,publish_control
    clock=Clock();server=Server(clock)
    api=BoundedGhAPI('gh',clock=clock.now,wall_clock=lambda:100+clock.now(),sleep=clock.sleep,transport=server)
    api.count=63
    path=tmp_path/'quota-allocation.json';path.write_bytes(b'original receipt')
    with pytest.raises(PublicationFailure) as caught:
        publish_control('gh','a/b','quota-krx-test-r17-a2',[path],source_sha='a'*40,run_id='17',run_attempt=2,title='quota',notes='original',api=api)
    assert caught.value.diagnostic()['reason']=='request_budget'
    assert api.count==64 and len(server.calls)==1
    assert not any(c[0]!='GET' for c in server.calls)

def test_data_control_retains_original_grace_across_fifty_minute_primary_reset(tmp_path):
    clock=Clock();server=Server(clock);original=server.request;failed=False
    def request(method,url,**kwargs):
        nonlocal failed
        if method=='POST' and '/assets?' in url and not failed:
            failed=True;return N(status=403,headers={'x-ratelimit-remaining':'0','x-ratelimit-reset':'3100'},data={'message':'API rate limit exceeded PRIVATE_TOKEN'})
        return original(method,url,**kwargs)
    server.request=request
    assert publish(tmp_path,server,clock,budget_seconds=7200)['published']
    assert clock.value>=3001 and clock.value<7200
    assert '/assets?' in server.calls[-1][1]


def test_reset_beyond_actual_remaining_job_does_not_perform_extra_io(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock);original=server.request;failed=False
    def request(method,url,**kwargs):
        nonlocal failed
        if method=='POST' and '/assets?' in url:
            failed=True;return N(status=403,headers={'retry-after':'3000'},data={'message':'secondary rate limit'})
        assert not failed,'extra I/O after the unavailable retry boundary'
        return original(method,url,**kwargs)
    server.request=request
    with pytest.raises(PublicationFailure):publish(tmp_path,server,clock,budget_seconds=600)
    assert failed and clock.value==2 and server.release['draft']


def test_grace_extension_only_allows_explicit_controls(tmp_path):
    from backtest_data.remote.paced_publication import publish_release
    path=tmp_path/'code.enc';path.write_bytes(b'exact code')
    clock=Clock();server=Server(clock)
    with pytest.raises(ValueError):publish_release('gh','a/b','code',[path],target='a'*40,title='code',notes='code',api=server,budget_seconds=7200)
    with pytest.raises(ValueError):publish_release('gh','a/b','data',[path],target='main',control_target=True,title='data',notes='data',api=server,budget_seconds=7201)
    assert not server.calls


def test_quota_control_keeps_its_original_forty_minute_budget(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure,publish_control
    clock=Clock();server=Server(clock);path=tmp_path/'quota-allocation.json';path.write_bytes(b'quota')
    with pytest.raises(PublicationFailure):publish_control('gh','a/b','quota-krx-test-r17-a2',[path],source_sha='a'*40,run_id='17',run_attempt=2,title='quota',notes='original',budget_seconds=7200,api=server,clock=clock.now,sleep=clock.sleep)
    assert not server.calls

def test_current_receipt_future_publication_never_verifies_or_admits_provider(tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from backtest_data.remote import quota
    now=datetime(2026,10,12,1,tzinfo=timezone.utc)
    binding={'stream_id':'krx-test','manifest_id':'m','handoff_id':'h','bootstrap_sha256':'a'*64}
    value=quota.plan_allocation(binding,[],[],run_id='17',run_attempt=2,now=now,expires_at=now+timedelta(hours=2),budget_seconds=300)
    events=[];provider_calls=[]
    def publish(*args,**kwargs):
        events.append('readback_complete')
        return {'release':{'published_at':'2026-10-12T02:00:00Z'},'readback_contents':{'quota-allocation.json':json.dumps(value).encode()}}
    def observed():events.append('fresh_clock');return now+timedelta(minutes=5)
    monkeypatch.setattr(quota,'publish_control',publish)
    monkeypatch.setattr(quota,'utcnow',observed,raising=False)
    with pytest.raises(ValueError,match='publication clock'):
        quota.publish_allocation('gh','a/b',value,tmp_path,target='main',source_sha='a'*40,phase_callback=lambda phase,**kw:events.append(phase))
        provider_calls.append('source_http')
    assert events==['quota_publication_started','readback_complete','fresh_clock']
    assert not provider_calls


def test_publication_wait_uses_fresh_clock_after_readback(tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from backtest_data.remote import quota
    now=datetime(2026,10,12,1,tzinfo=timezone.utc);observed=[now];events=[]
    binding={'stream_id':'krx-test','manifest_id':'m','handoff_id':'h','bootstrap_sha256':'a'*64}
    value=quota.plan_allocation(binding,[],[],run_id='17',run_attempt=2,now=now,expires_at=now+timedelta(hours=2),budget_seconds=300)
    def publish(*args,**kwargs):
        observed[0]=now+timedelta(minutes=10);events.append('readback_complete')
        return {'release':{'published_at':'2026-10-12T01:09:00Z'},'readback_contents':{'quota-allocation.json':json.dumps(value).encode()}}
    def clock():events.append('fresh_clock');return observed[0]
    monkeypatch.setattr(quota,'publish_control',publish)
    monkeypatch.setattr(quota,'utcnow',clock,raising=False)
    assert quota.publish_allocation('gh','a/b',value,tmp_path,target='main',source_sha='a'*40,phase_callback=lambda phase,**kw:events.append(phase))==value
    assert events==['quota_publication_started','readback_complete','fresh_clock','quota_publication_verified']

def test_bounded_read_retry_past_deadline_keeps_typed_deadline_and_http_status():
    from backtest_data.remote.krx_publication import BoundedGhAPI,PublicationFailure
    clock=Clock();server=Server(clock)
    server.failure=lambda *a:N(status=403,headers={'retry-after':'3000'},data={'message':'secondary rate limit PRIVATE_TOKEN'})
    api=BoundedGhAPI('gh',budget_seconds=600,clock=clock.now,wall_clock=lambda:100+clock.now(),sleep=clock.sleep,transport=server)
    with pytest.raises(PublicationFailure) as caught:api.get('repos/a/b')
    diagnostic=caught.value.diagnostic()
    assert diagnostic['reason']=='deadline' and diagnostic['http_status']==403
    assert diagnostic['retry_after_seconds']==3000 and api.count==1 and not clock.waits
    assert 'PRIVATE_TOKEN' not in json.dumps(diagnostic)


def test_existing_exact_draft_wrong_provenance_is_rejected_before_any_write(tmp_path):
    from backtest_data.remote.krx_publication import PublicationFailure
    clock=Clock();server=Server(clock);path=tmp_path/'commit.json';path.write_bytes(b'original exact commit')
    server.release={'id':7,'tag_name':'data-krx-test-000000001-r17-a2','draft':True,'prerelease':False,'target_commitish':'main','body':'conflicting PRIVATE_TOKEN provenance','upload_url':'https://uploads.github.com/repos/a/b/releases/7/assets{?name,label}'}
    server.assets[path.name]={'id':8,'name':path.name,'state':'uploaded','size':path.stat().st_size,'digest':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(PublicationFailure):publish(tmp_path,server,clock)
    assert server.release['draft'] is True
    assert not any(c[0]!='GET' for c in server.calls)


def test_lost_create_ack_reconciles_original_draft_with_exact_provenance(tmp_path):
    clock=Clock();server=Server(clock);original=server.request;lost=False
    def request(method,url,**kwargs):
        nonlocal lost
        response=original(method,url,**kwargs)
        if method=='POST' and url.endswith('/releases') and not lost:
            lost=True;return N(status=502,headers={},data={'message':'ambiguous PRIVATE_TOKEN'})
        return response
    server.request=request
    assert publish(tmp_path,server,clock)['published']
    assert len([c for c in server.calls if c[0]=='POST' and c[1].endswith('/releases')])==1
    assert server.release['body'].endswith(json.dumps({'source_sha':'a'*40,'run_id':'17','run_attempt':2},sort_keys=True))

def test_shared_budget_type_preserves_runtimeerror_message_and_absolute_paths():
    from backtest_data.remote.paced_publication import _Publisher,PublicationBudgetExceeded
    clock=Clock();server=Server(clock)
    publisher=_Publisher(server,clock=clock.now,wall_clock=lambda:100,sleep=clock.sleep,budget_seconds=600,max_retries=0)
    with pytest.raises(PublicationBudgetExceeded) as caught:publisher.wait(3000)
    assert isinstance(caught.value,RuntimeError) and str(caught.value)=='GitHub publication budget exhausted'
    clock.value=600
    with pytest.raises(PublicationBudgetExceeded):publisher.request('GET','repos/a/b')
    assert not server.calls


def test_arbitrary_error_text_never_becomes_a_deadline_diagnostic():
    from backtest_data.remote.krx_publication import BoundedGhAPI,PublicationFailure
    clock=Clock()
    class Transport:
        def request(self,*args,**kwargs):raise RuntimeError('GitHub publication budget exhausted PRIVATE_TOKEN')
    api=BoundedGhAPI('gh',clock=clock.now,sleep=clock.sleep,transport=Transport())
    with pytest.raises(PublicationFailure) as caught:api.get('repos/a/b')
    assert caught.value.diagnostic()['reason']=='proof_conflict'
    assert 'PRIVATE_TOKEN' not in str(caught.value)


def test_rate_retry_count_exhaustion_keeps_rate_reason_with_available_time():
    from backtest_data.remote.krx_publication import BoundedGhAPI,PublicationFailure
    clock=Clock();server=Server(clock)
    server.failure=lambda *a:N(status=403,headers={'retry-after':'1'},data={'message':'secondary rate limit PRIVATE_TOKEN'})
    api=BoundedGhAPI('gh',clock=clock.now,wall_clock=lambda:100+clock.now(),sleep=clock.sleep,transport=server)
    with pytest.raises(PublicationFailure) as caught:api.get('repos/a/b')
    assert caught.value.diagnostic()['reason']=='rate_limit' and caught.value.diagnostic()['http_status']==403
    assert api.count==4 and clock.value==420
