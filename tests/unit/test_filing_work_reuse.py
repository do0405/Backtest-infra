from types import SimpleNamespace as N

from backtest_data.fundamentals import collection as mod


def request(**changes):
    return {'manifest_id':'whole','issuer_id':'issuer','market':'US','cik':'123',
        'filing':{'accessionNumber':'0000000123-26-000001','filingDate':'2026-10-09','form':'10-Q','primaryDocument':'original.htm'},
        'disclosure_profile':'periodic_report','output_start':'2016-01-01','output_end':'2026-10-10',
        'discovery_start':'2026-07-01',**changes}


def test_unchanged_accession_reuses_original_checkpoint_across_discovery_windows(monkeypatch):
    old=N(request=request(),status='succeeded',checkpoint={'stored_source_facts':42})
    session=N(scalars=lambda _: [old])
    monkeypatch.setattr(mod,'plan_job',lambda *_args,**_kwargs: (_ for _ in ()).throw(AssertionError('must reuse old work')))
    assert mod._plan_discovered_filing(session,source='sec',accession='0000000123-26-000001',
        request=request(output_end='2026-10-11',discovery_start='2026-07-02')) is False
    assert old.checkpoint=={'stored_source_facts':42}


def test_changed_filing_metadata_creates_new_work_but_identity_ignores_discovery_window(monkeypatch):
    old=N(request=request(),status='succeeded');planned=[]
    session=N(scalars=lambda _: [old])
    monkeypatch.setattr(mod,'plan_job',lambda *_args,**kwargs:planned.append(kwargs))
    changed={**request()['filing'],'primaryDocument':'corrected.htm'}
    assert mod._plan_discovered_filing(session,source='sec',accession='0000000123-26-000001',request=request(filing=changed))
    from backtest_data.ingestion.jobs import logical_request
    assert logical_request(planned[0]['request'])==mod.filing_work_request(request(filing=changed,output_end='2026-10-12',discovery_start='2026-07-03'))
    assert 'discovery_start' not in planned[0]['request']


def test_provider_display_metadata_does_not_redownload_unchanged_accession(monkeypatch):
    old_request=request(filing={**request()['filing'],'size':1000,'items':'old-display'})
    old=N(request=old_request,status='succeeded');session=N(scalars=lambda _: [old])
    monkeypatch.setattr(mod,'plan_job',lambda *_args,**_kwargs: (_ for _ in ()).throw(AssertionError('unchanged source document')))
    new=request(filing={**request()['filing'],'size':1001,'items':'new-display'})
    assert mod._plan_discovered_filing(session,source='sec',accession='0000000123-26-000001',request=new) is False


def test_new_accession_work_retains_manifest_lineage_without_window_identity(monkeypatch):
    planned=[];session=N(scalars=lambda _: [])
    monkeypatch.setattr(mod,'plan_job',lambda *_args,**kwargs:planned.append(kwargs))
    mod._plan_discovered_filing(session,source='sec',accession='0000000123-26-000001',request=request())
    assert planned[0]['request']['manifest_id']=='whole'
    assert 'output_end' not in planned[0]['request']
