import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as N
from urllib.parse import parse_qs,urlsplit

import pytest


class Clock:
    def __init__(self):self.value=0.;self.waits=[]
    def now(self):return self.value
    def sleep(self,seconds):self.waits.append(seconds);self.value+=seconds


class Server:
    def __init__(self,clock,*,draft=False):
        self.clock=clock;self.release={'id':7,'tag_name':'generation','draft':True,'prerelease':False,
            'upload_url':'https://uploads.github.com/repos/owner/repo/releases/7/assets{?name,label}'} if draft else None
        self.assets={};self.calls=[];self.failure=None;self.final_verified=False
    def response(self,status,data,headers=None):return N(status=status,data=data,headers=headers or {})
    def request(self,method,url,*,body=None,input_file=None):
        self.calls.append((method,url,self.clock.now(),input_file))
        if self.failure:
            result=self.failure(method,url,input_file)
            if result is not None:return result
        if '/releases/tags/' in url:return self.response(200,self.release) if self.release else self.response(404,{})
        if method=='POST' and url.endswith('/releases'):
            self.release={'id':7,**body,'upload_url':'https://uploads.github.com/repos/owner/repo/releases/7/assets{?name,label}'}
            return self.response(201,self.release)
        if method=='GET' and '/assets?' in url:
            query=parse_qs(urlsplit(url).query);page=int(query['page'][0]);values=list(self.assets.values())
            self.final_verified=True;return self.response(200,values[(page-1)*100:page*100])
        if method=='POST' and '/assets?' in url:
            path=Path(input_file);name=parse_qs(urlsplit(url).query)['name'][0]
            item={'id':len(self.assets)+1,'name':name,'state':'uploaded','size':path.stat().st_size,
                'digest':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()}
            self.assets[name]=item;self.final_verified=False;return self.response(201,item)
        if method=='PATCH':
            assert self.final_verified,'publishing preceded the complete exact inventory proof'
            self.release={**self.release,**body};return self.response(200,self.release)
        raise AssertionError((method,url))


def assets(root,count=2):
    paths=[]
    for index in range(count):
        path=root/f'asset-{index:03d}.enc';path.write_bytes(f'exact ciphertext {index}'.encode());paths.append(path)
    return paths


def publish(server,clock,paths,**kwargs):
    from backtest_data.remote.paced_publication import publish_release
    return publish_release('gh','owner/repo','generation',paths,target='a'*40,title='private checkpoint',notes='ciphertext',
        api=server,clock=clock.now,wall_clock=lambda:100+clock.now(),sleep=clock.sleep,**kwargs)


@pytest.mark.parametrize('target,control_target,valid',[
    ('main',True,True),('main',False,False),('master',True,False),
    ('refs/heads/main',True,False),('a'*40,False,True),('a'*40,True,False),
    ('main','true',False),('main',1,False)])
def test_only_explicit_main_control_target_keeps_code_targets_sha_bound(tmp_path,target,control_target,valid):
    from backtest_data.remote.paced_publication import publish_release
    clock=Clock();server=Server(clock)
    def invoke():
        return publish_release('gh','owner/repo','generation',assets(tmp_path,1),
            target=target,control_target=control_target,title='delivery control',notes='original run recorded',
            api=server,clock=clock.now,wall_clock=lambda:100+clock.now(),sleep=clock.sleep)
    if valid:
        assert invoke()['uploaded_assets']==1
        assert server.release['target_commitish']==target
    else:
        with pytest.raises(ValueError):invoke()
        assert not server.calls


def test_256_assets_use_serial_spaced_writes_and_verify_every_asset_before_publish(tmp_path):
    clock=Clock();server=Server(clock);paths=assets(tmp_path,256)
    result=publish(server,clock,paths)
    writes=[call for call in server.calls if call[0] in {'POST','PATCH'}]
    assert len(writes)==258 and all(later[2]-prior[2]>=2 for prior,later in zip(writes,writes[1:]))
    assert clock.now()==514 and result['uploaded_assets']==256 and server.release['draft'] is False
    assert len([call for call in server.calls if call[0]=='GET'])==5


def test_resume_draft_skips_only_exact_existing_size_and_sha256(tmp_path):
    clock=Clock();server=Server(clock,draft=True);paths=assets(tmp_path)
    first=paths[0];server.assets[first.name]={'id':1,'name':first.name,'state':'uploaded','size':first.stat().st_size,
        'digest':'sha256:'+hashlib.sha256(first.read_bytes()).hexdigest()}
    result=publish(server,clock,paths)
    assert result['skipped_assets']==1 and result['uploaded_assets']==1
    assert len([call for call in server.calls if call[0]=='POST'])==1


@pytest.mark.parametrize('change',[{'size':0},{'digest':'sha256:'+'f'*64},{'state':'starter'},{'digest':None}])
def test_draft_conflicting_asset_fails_closed_without_clobber_or_publish(tmp_path,change):
    clock=Clock();server=Server(clock,draft=True);path=assets(tmp_path,1)[0]
    server.assets[path.name]={'id':1,'name':path.name,'state':'uploaded','size':path.stat().st_size,
        'digest':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest(),**change}
    with pytest.raises(ValueError,match='asset'):publish(server,clock,[path])
    assert not any(call[0] in {'POST','PATCH','DELETE'} for call in server.calls)


def test_secondary_limit_preserves_retry_after_and_reset_without_early_retry(tmp_path):
    clock=Clock();server=Server(clock);failed=False
    def failure(method,url,path):
        nonlocal failed
        if method=='POST' and '/assets?' in url and not failed:
            failed=True;return server.response(403,{'message':'secondary rate limit'},
                {'retry-after':'123','x-ratelimit-remaining':'0','x-ratelimit-reset':'300'})
    server.failure=failure
    publish(server,clock,assets(tmp_path,1))
    uploads=[call for call in server.calls if call[0]=='POST' and '/assets?' in call[1]]
    assert uploads[1][2]-uploads[0][2]>=199 and clock.now()>=203


@pytest.mark.parametrize('headers',[{'x-ratelimit-remaining':'900','x-ratelimit-reset':'200'},
    {'retry-after':'120','x-ratelimit-remaining':'0','x-ratelimit-reset':'200'}])
def test_authorization_403_never_retries_or_exposes_response_text(tmp_path,capsys,headers):
    clock=Clock();server=Server(clock)
    server.failure=lambda *_:server.response(403,{'message':'Resource not accessible PRIVATE_TOKEN'},headers)
    with pytest.raises(ValueError) as caught:publish(server,clock,assets(tmp_path,1))
    assert len(server.calls)==1 and not clock.waits and 'PRIVATE_TOKEN' not in str(caught.value)
    assert capsys.readouterr().out==''


def test_repeated_secondary_limit_has_bounded_exponential_retries(tmp_path):
    clock=Clock();server=Server(clock)
    server.failure=lambda *_:server.response(429,{'message':'secondary rate limit'})
    with pytest.raises(RuntimeError,match='retry'):publish(server,clock,assets(tmp_path,1),max_retries=3)
    assert len(server.calls)==4 and sum(clock.waits)==60+120+240


def test_ambiguous_upload_success_is_reconciled_and_reused_within_same_invocation(tmp_path):
    clock=Clock();server=Server(clock);failed=False
    def failure(method,url,path):
        nonlocal failed
        if method=='POST' and '/assets?' in url and not failed:
            failed=True;value=Path(path)
            server.assets[value.name]={'id':1,'name':value.name,'state':'uploaded','size':value.stat().st_size,
                'digest':'sha256:'+hashlib.sha256(value.read_bytes()).hexdigest()}
            return server.response(502,{'message':'ambiguous upstream failure'})
    server.failure=failure;result=publish(server,clock,assets(tmp_path))
    assert result['uploaded_assets']==2
    assert len([call for call in server.calls if call[0]=='POST' and '/assets?' in call[1]])==2
    assert server.release['draft'] is False


def test_last_allowed_upload_success_is_reconciled_after_backoff(tmp_path):
    clock=Clock();server=Server(clock);attempts=0
    def failure(method,url,path):
        nonlocal attempts
        if method=='POST' and '/assets?' in url:
            attempts+=1
            if attempts==2:
                value=Path(path);server.assets[value.name]={'id':1,'name':value.name,'state':'uploaded',
                    'size':value.stat().st_size,'digest':'sha256:'+hashlib.sha256(value.read_bytes()).hexdigest()}
            return server.response(502,{'message':'ambiguous upstream failure'})
    server.failure=failure;result=publish(server,clock,assets(tmp_path,1),max_retries=1)
    assert result['uploaded_assets']==1 and attempts==2 and server.release['draft'] is False
    assert sum(clock.waits)>=60+120


def test_last_allowed_patch_actual_publish_success_is_reconciled_after_backoff(tmp_path):
    clock=Clock();server=Server(clock);attempts=0
    def failure(method,url,path):
        nonlocal attempts
        if method=='PATCH':
            attempts+=1
            if attempts==2:server.release={**server.release,'draft':False}
            return server.response(502,{'message':'published but response lost'})
    server.failure=failure;result=publish(server,clock,assets(tmp_path,1),max_retries=1)
    assert result['published'] and attempts==2 and server.release['draft'] is False
    assert sum(clock.waits)>=60+120


def test_terminal_authorization_response_has_no_reconciliation_or_backoff(tmp_path):
    clock=Clock();server=Server(clock);attempts=0
    def failure(method,url,path):
        nonlocal attempts
        if method=='POST' and '/assets?' in url:
            attempts+=1
            return server.response(502,{}) if attempts==1 else server.response(403,
                {'message':'Resource not accessible by integration'},{'retry-after':'999','x-ratelimit-remaining':'0','x-ratelimit-reset':'999'})
    server.failure=failure
    with pytest.raises(ValueError,match='authorization'):publish(server,clock,assets(tmp_path,1),max_retries=1)
    assert attempts==2 and clock.now()==62 and server.calls[-1][0]=='POST'


def test_terminal_reconciliation_obeys_rate_reset_before_proving_accepted_asset(tmp_path):
    clock=Clock();server=Server(clock)
    def failure(method,url,path):
        if method=='POST' and '/assets?' in url:
            value=Path(path);server.assets[value.name]={'id':1,'name':value.name,'state':'uploaded',
                'size':value.stat().st_size,'digest':'sha256:'+hashlib.sha256(value.read_bytes()).hexdigest()}
            return server.response(403,{'message':'secondary rate limit'},
                {'retry-after':'90','x-ratelimit-remaining':'0','x-ratelimit-reset':'250'})
    server.failure=failure
    assert publish(server,clock,assets(tmp_path,1),max_retries=0)['published']
    proof_reads=[call for call in server.calls if call[0]=='GET' and call[2]>0]
    assert proof_reads[0][2]>=151 and len([call for call in server.calls if call[0]=='POST' and '/assets?' in call[1]])==1


def test_retry_wait_beyond_publication_budget_leaves_draft_unpublished(tmp_path):
    clock=Clock();server=Server(clock,draft=True)
    server.failure=lambda *_:server.response(429,{}, {'retry-after':'3600'})
    with pytest.raises(RuntimeError,match='budget'):publish(server,clock,assets(tmp_path,1),budget_seconds=1800)
    assert server.release['draft'] is True and len(server.calls)==1


def test_gh_api_keeps_headers_and_file_input_private(monkeypatch,tmp_path,capsys):
    from backtest_data.remote import paced_publication as module
    from backtest_data.remote import github_http
    calls=[]
    def run(command,**kwargs):
        calls.append((command,kwargs));return N(returncode=1,stdout=b'HTTP/2.0 403 Forbidden\nRetry-After: 90\nX-RateLimit-Remaining: 0\n\n{"message":"secondary rate limit"}\n',stderr=b'PRIVATE_TOKEN gh failure')
    monkeypatch.setattr(github_http.subprocess,'run',run)
    path=assets(tmp_path,1)[0];response=module.GhAPI('gh').request('POST',
        'https://uploads.github.com/repos/owner/repo/releases/7/assets?name='+path.name,input_file=path)
    assert response.status==403 and response.headers['retry-after']=='90'
    assert '--include' in calls[0][0] and calls[0][0][calls[0][0].index('--input')+1]==str(path)
    assert 'Content-Length: '+str(path.stat().st_size) in calls[0][0]
    assert 'PRIVATE_TOKEN' not in repr(response) and capsys.readouterr().out==''
