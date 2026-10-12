from datetime import datetime, timedelta, timezone
import json
import pytest
from backtest_data.remote import quota
NOW = datetime(2026,10,12,1,tzinfo=timezone.utc)
BINDING = {'stream_id':'krx-test','manifest_id':'m','handoff_id':'h','bootstrap_sha256':'a'*64}
def receipt(now=NOW):
    return quota.plan_allocation(BINDING,[],[],run_id='1',run_attempt=1,now=now,expires_at=now+timedelta(hours=2),budget_seconds=300)
def setup(monkeypatch,tmp_path,value=None,**changes):
    value = value or receipt()
    release = {'id':1,'tag_name':quota.allocation_tag(value),'draft':False,'prerelease':False,'created_at':'2020-01-01T00:00:00Z','published_at':value['created_at'],'assets':[{'id':8,'name':'quota-allocation.json','size':len(json.dumps(value).encode())}],**changes}
    calls=[]
    class API:
        def get(self,url,raw=False):
            if raw:calls.append(65536);return json.dumps(value).encode()
            return [release]
    monkeypatch.setattr(quota,'BoundedGhAPI',lambda *a,**k:API())
    return calls

def test_old_target_commit_recent_allocation_still_burns(monkeypatch,tmp_path):
    calls=setup(monkeypatch,tmp_path)
    assert quota.read_allocations('gh','a/b',BINDING,tmp_path,now=NOW)==[receipt()]
    assert calls==[65536]

@pytest.mark.parametrize('change',[{'published_at':None},{'published_at':'bad'},{'published_at':'2026-10-12T01:00:01Z'},{'published_at':'2026-10-11T00:00:00Z'},{'draft':True},{'prerelease':True},{'published_at':'2026-10-12T01:00:00'}])
def test_uncertain_publication_clock_fails_closed(monkeypatch,tmp_path,change):
    setup(monkeypatch,tmp_path,**change)
    with pytest.raises(ValueError):quota.read_allocations('gh','a/b',BINDING,tmp_path,now=NOW)

def test_old_receipt_validated_then_ignored(monkeypatch,tmp_path):
    calls=setup(monkeypatch,tmp_path,receipt(NOW-timedelta(days=5)))
    assert quota.read_allocations('gh','a/b',BINDING,tmp_path,now=NOW)==[]
    assert calls==[65536]

def test_old_rebound_receipt_is_not_silently_pruned(monkeypatch,tmp_path):
    value=receipt(NOW-timedelta(days=5));value['handoff_id']='wrong';setup(monkeypatch,tmp_path,value)
    with pytest.raises(ValueError):quota.read_allocations('gh','a/b',BINDING,tmp_path,now=NOW)

def test_quota_midnight_keeps_next_day_burn(monkeypatch,tmp_path):
    now=NOW.replace(hour=14,minute=30);value=receipt(now);setup(monkeypatch,tmp_path,value)
    assert set(value['credits_by_day'])=={'2026-10-12','2026-10-13'}
    assert quota.read_allocations('gh','a/b',BINDING,tmp_path,now=now)==[value]

def test_matching_tag_run_identity_is_validated_before_pruning(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path,tag_name='quota-krx-test-r2-a1')
    with pytest.raises(ValueError):quota.read_allocations('gh','a/b',BINDING,tmp_path,now=NOW)

def test_complete_pagination_required_before_admission(monkeypatch,tmp_path):
    class API:
        def get(self,url,raw=False):
            if url.endswith('&page=1'):return [{'tag_name':'unrelated'}]*100
            raise quota.PublicationFailure({'phase':'metadata','reason':'request_budget','http_status':0})
    with pytest.raises(quota.PublicationFailure):quota.read_allocations('gh','a/b',BINDING,tmp_path,now=NOW,api=API())

def test_current_hosted_source_sha_mismatch_never_reaches_receipts(monkeypatch,tmp_path):
    class API:
        def get(self,url,raw=False):return {'id':17,'run_attempt':2,'head_sha':'b'*40}
    monkeypatch.setattr(quota,'BoundedGhAPI',lambda *a,**k:API())
    monkeypatch.setattr(quota,'read_allocations',lambda *a,**k:pytest.fail('source rebound reached quota history'))
    with pytest.raises(ValueError):quota.reserve_hosted_allowance('gh','a/b',BINDING,tmp_path,run_id='17',run_attempt=2,budget_seconds=300,now=NOW,target='main',source_sha='a'*40)

def test_bounded_history_exhaustion_defers_without_publication_or_source_admission(monkeypatch,tmp_path):
    def api(*a,**k):raise quota.PublicationFailure({'phase':'metadata','reason':'request_budget','http_status':0})
    monkeypatch.setattr(quota,'BoundedGhAPI',api)
    monkeypatch.setattr(quota,'publish_allocation',lambda *a,**k:pytest.fail('partial history published new quota'))
    assert quota.reserve_hosted_allowance('gh','a/b',BINDING,tmp_path,run_id='17',run_attempt=2,budget_seconds=300,now=NOW,target='main',source_sha='a'*40) is None

def test_retry_boundary_outside_admission_deadline_defers_with_actual_403(monkeypatch,tmp_path):
    from types import SimpleNamespace as N
    from backtest_data.remote.krx_publication import BoundedGhAPI
    clock=[0.];calls=[];events=[];provider_calls=[]
    class Server:
        def request(self,*args,**kwargs):
            calls.append(args);return N(status=403,headers={'retry-after':'3000'},data={'message':'secondary rate limit PRIVATE_TOKEN'})
    api=BoundedGhAPI('gh',budget_seconds=600,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n),transport=Server())
    monkeypatch.setattr(quota,'BoundedGhAPI',lambda *a,**k:api)
    allowance=quota.reserve_hosted_allowance('gh','a/b',BINDING,tmp_path,run_id='17',run_attempt=2,budget_seconds=300,now=NOW,target='main',source_sha='a'*40,phase_callback=lambda phase,**kw:events.append((phase,kw)))
    if allowance is not None:provider_calls.append('source_http')
    assert allowance is None and not provider_calls and len(calls)==1 and clock[0]==0
    diagnostic=events[0][1]['publication_failure']
    assert events[0][0]=='quota_publication_deferred' and diagnostic['reason']=='deadline' and diagnostic['http_status']==403


def test_injected_admission_budget_is_reused_through_publication(monkeypatch, tmp_path):
    class API:
        def __init__(self): self.calls = []
        def get(self, url, raw=False):
            self.calls.append(url)
            if '/actions/runs/17' in url:
                return {'id': 17, 'run_attempt': 2, 'head_sha': 'a' * 40,
                        'run_started_at': NOW.isoformat()}
            if '/actions/workflows/' in url: return {'workflow_runs': []}
            return []
        def remaining(self): return 600

    api = API()
    monkeypatch.setattr(quota, 'BoundedGhAPI', lambda *a, **k: pytest.fail('new admission budget'))
    seen = []
    monkeypatch.setattr(quota, 'publish_allocation', lambda *a, **k: seen.append(k['api']) or a[2])
    value = quota.reserve_hosted_allowance('gh', 'a/b', BINDING, tmp_path,
        run_id='17', run_attempt=2, budget_seconds=300, now=NOW,
        target='main', source_sha='a' * 40, api=api)
    assert value['run_id'] == '17' and seen == [api] and len(api.calls) == 3
