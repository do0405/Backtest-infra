from datetime import datetime,timezone

import pytest

from backtest_data.fundamentals.domain import stable_hash


def audit():
    value={'format_version':'financial_publication_audit_v1','policy_version':'financial_public_pit_v1',
        'watermark':datetime(2026,10,9,tzinfo=timezone.utc).isoformat(),
        'original_target':{'manifest_id':'M','content_hash':'a'*64,'security_count':17758},
        'documents':[{'id':'D','content_hash':'b'*64,'source':'sec','issuer_id':'I','captured_at':'2026-10-08T00:00:00+00:00'}]}
    value['content_hash']=stable_hash(value)
    return value


def test_audit_target_is_hash_bound_and_full_document_ids_are_preserved():
    from backtest_data.fundamentals.publication_runner import validate_audit
    value=audit()
    assert validate_audit(value)['original_target']['security_count']==17758
    value['documents'][0]['content_hash']='c'*64
    with pytest.raises(ValueError,match='audit_hash'):
        validate_audit(value)


def test_cursor_cannot_resume_under_another_audit_target():
    from backtest_data.fundamentals.publication_runner import validate_progress
    value=audit()
    with pytest.raises(ValueError,match='audit_target'):
        validate_progress({'audit_hash':'different'},value)
    progress={'audit_hash':value['content_hash'],'cursor':'D','results':{'D':{'status':'unavailable'}}}
    assert validate_progress(progress,value) is progress


def test_cursor_outside_frozen_document_set_is_rejected():
    from backtest_data.fundamentals.publication_runner import validate_progress
    value=audit()
    with pytest.raises(ValueError,match='cursor'):
        validate_progress({'audit_hash':value['content_hash'],'cursor':'outside','results':{}},value)


def test_transport_uses_requested_bound_and_blocks_only_dart_original_stage():
    from types import SimpleNamespace
    from backtest_data.fundamentals.publication_runner import _OriginalTransport
    from backtest_data.fundamentals.public_version_batch import FetchRequest
    from backtest_data.ingestion.http import ProviderError
    class Secret:
        def reveal(self):return 'test'
    calls=[]
    class HTTP:
        def request(self,source,method,url,**kwargs):
            calls.append((url,kwargs['max_bytes']))
            return SimpleNamespace(content=b'<result><status>800</status></result>' if url.endswith('.xml') else b'{"status":"000"}')
    transport=_OriginalTransport(HTTP(),SimpleNamespace(opendart_api_key=Secret(),sec_user_agent='Test test@example.test'))
    original=FetchRequest('opendart','https://opendart.fss.or.kr/api/fnlttXbrl.xml',max_bytes=1234)
    with pytest.raises(ProviderError,match='800'):transport(original)
    with pytest.raises(ProviderError,match='source_stage_blocked'):transport(original)
    transport(FetchRequest('opendart','https://opendart.fss.or.kr/api/list.json',max_bytes=4321))
    assert calls==[(original.url,1234),('https://opendart.fss.or.kr/api/list.json',4321)]


@pytest.mark.parametrize('code',['response_size_exceeded','403'])
def test_repeated_sec_archive_permanent_failure_does_not_repeat_large_download(code):
    from types import SimpleNamespace
    from backtest_data.fundamentals.publication_runner import _OriginalTransport
    from backtest_data.fundamentals.public_version_batch import FetchRequest
    from backtest_data.ingestion.http import ProviderError
    calls=[]
    class HTTP:
        def request(self,*args,**kwargs):
            calls.append(kwargs['max_bytes'])
            raise ProviderError('sec',code)
    transport=_OriginalTransport(HTTP(),SimpleNamespace(sec_user_agent='Test test@example.test'))
    request=FetchRequest('sec','https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz',max_bytes=1234)
    for _ in range(2):
        with pytest.raises(ProviderError,match=code):transport(request)
    assert calls==[1234]
    with pytest.raises(ProviderError,match=code):transport(FetchRequest('sec',request.url,max_bytes=4321))
    assert calls==[1234,4321]


def test_retryable_sec_failure_can_be_retried_for_another_document():
    from types import SimpleNamespace
    from backtest_data.fundamentals.publication_runner import _OriginalTransport
    from backtest_data.fundamentals.public_version_batch import FetchRequest
    from backtest_data.ingestion.http import ProviderError
    calls=[]
    class HTTP:
        def request(self,*args,**kwargs):
            calls.append(1)
            if len(calls)==1:raise ProviderError('sec','429',retryable=True)
            return SimpleNamespace(content=b'archive')
    transport=_OriginalTransport(HTTP(),SimpleNamespace(sec_user_agent='Test test@example.test'))
    request=FetchRequest('sec','https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz')
    with pytest.raises(ProviderError,match='429'):transport(request)
    assert transport(request).content==b'archive'
    assert len(calls)==2


@pytest.mark.parametrize('compressed,total',[(0,512),(513,512),(64,0),(64,8193)])
def test_archive_resource_limits_reject_unsafe_values_before_sql_or_files(compressed,total):
    from backtest_data.fundamentals.publication_runner import verify_publications
    with pytest.raises(ValueError,match='archive_resource'):
        verify_publications(None,audit_path='missing',progress_path='missing',
            archive_compressed_mb=compressed,archive_total_mb=total)
