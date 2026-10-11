from datetime import date, datetime, timezone
from decimal import Decimal
import pytest

def test_russell_two_real_extraction_layouts_are_changes_not_snapshot():
    from backtest_data.indices.parsers import parse_russell_pages
    pages=['Company\nSymbol\nIndustry\n1STDIBS.COM\nDIBS\nTechnology\nBIGLARI HOLDINGS A\nBH.A\nConsumer Discretionary',
           'Russell US Indexes\nlseg.com/ftse-russell 2\nCORPORATE\nCompany Symbol Industry\nALPHA TEKNOVA TKNO Health Care\n© 2026 London Stock Exchange Group']
    rows=parse_russell_pages(pages,index_key='FTSE_RUSSELL:RUSSELL_3000',action='add',
        effective_date=date(2026,6,26),published_date=None,effective_phase='after_close')
    assert [r['member_identifier'] for r in rows]==['DIBS','BH.A','TKNO']
    assert all(r['record_kind']=='change' and r['weight'] is None and r['published_date'] is None for r in rows)
    assert rows[1]['member_name']=='BIGLARI HOLDINGS A'
    assert 'baseline_unavailable' in rows[0]['issues']

@pytest.mark.parametrize('text',['Company\nSymbol\nIndustry\nExample\nTICK\nUnknown Sector',
                                    'Company Symbol Industry\nExample TICK Health Care\nBROKEN TABLE ROW',
                                    'Company\nSymbol\nIndustry\nExample\nTICK'])
def test_russell_malformed_table_is_not_partial_success(text):
    from backtest_data.indices.parsers import parse_russell_pages
    with pytest.raises(ValueError):
        parse_russell_pages([text],index_key='FTSE_RUSSELL:RUSSELL_3000',action='delete',
            effective_date=date(2026,6,26),published_date=None,effective_phase='after_close')

def test_russell_2026_table_footer_is_not_a_security_row():
    from backtest_data.indices.parsers import parse_russell_pages
    page='Company\nSymbol\nIndustry\nCOMSTOCK\nCHCI\nReal Estate\nFTSE Russell\n| Final index Adds\n2\nReconstitution\nRussell US Indexes\nRussell 3000 Index - Additions'
    rows=parse_russell_pages([page],index_key='FTSE_RUSSELL:RUSSELL_3000',action='add',effective_date=date(2026,6,26),published_date=None,effective_phase='after_close')
    assert len(rows)==1 and rows[0]['member_identifier']=='CHCI'

def test_russell_ipo_layout_effective_day_is_not_as_at_or_public_day():
    from backtest_data.indices.parsers import parse_russell_ipo_pages
    pages=['Russell 3000® Index\nFinal List of IPO Additions\nEffective September 21, 2026\nTicker     Company Name\nINIO       Innio NV\nITG        ITG Inc\nRussell 3000 Index Initial Public Offering Additions. Data as at: 4 September 2026\n1 of 2 Please see disclaimer for important legal information.',
           '© 2024 London Stock Exchange Group. All rights reserved.']
    rows=parse_russell_ipo_pages(pages)
    assert [row['member_identifier'] for row in rows]==['INIO','ITG']
    assert rows[1]['member_name']=='ITG Inc' and rows[0]['effective_date']==date(2026,9,21)
    assert rows[0]['effective_phase']=='unknown' and rows[0]['published_date'] is None
    assert rows[0]['fields']['data_as_at']=='2026-09-04'

def test_nasdaq_compact_html_parse_dateline_does_not_capture_corporate_boilerplate():
    from backtest_data.indices.acquisition import parse_response
    from backtest_data.indices.requests import IndexRequest
    raw=b'<p>NEW YORK, Dec. 11, 2020 (GLOBE NEWSWIRE) -- Nasdaq-100 will become effective prior to market open on Monday, December 21, 2020.</p><p>The following companies will be added to the Index: A (Nasdaq:AAA).</p><p>The following companies will be removed from the Index: B (Nasdaq:BBB).</p><p>About Nasdaq</p><p>Nasdaq (Nasdaq:NDAQ) is a company.</p>'
    rows,_=parse_response(IndexRequest('nasdaq_index','https://ir.nasdaq.com/node/102851','nasdaq_html'),raw)
    assert [row['member_identifier'] for row in rows]==['AAA','BBB']

def test_nasdaq_notice_multiple_security_classes_and_separate_dates():
    from backtest_data.indices.parsers import parse_nasdaq_notice
    text='''Annual Changes to the Nasdaq-100 Index®\nDecember 11, 2020
NEW YORK, Dec. 11, 2020 (GLOBE NEWSWIRE) -- Nasdaq announced the annual reconstitution of the Nasdaq-100 Index, which will become effective prior to market open on Monday, December 21, 2020.
The following six companies will be added to the Index: American Electric Power (Nasdaq: AEP), Marvell (Nasdaq: MRVL).
The following six companies will be removed from the Index: Liberty Global (Nasdaq: LBTYA/LBTYK), Ulta Beauty (Nasdaq: ULTA).
About Nasdaq\nNasdaq (Nasdaq: NDAQ) is a provider.'''
    rows=parse_nasdaq_notice(text)
    assert [r['member_identifier'] for r in rows]==['AEP','MRVL','LBTYA','LBTYK','ULTA']
    assert {r['published_date'] for r in rows}=={date(2020,12,11)}
    assert {r['effective_date'] for r in rows}=={date(2020,12,21)}
    assert all(r['effective_phase']=='before_open' and r['weight'] is None for r in rows)
    assert 'NDAQ' not in [r['member_identifier'] for r in rows]

def test_nasdaq_without_release_day_cannot_use_effective_day_as_publication():
    from backtest_data.indices.parsers import parse_nasdaq_notice
    with pytest.raises(ValueError):parse_nasdaq_notice('Nasdaq-100 effective prior to market open on December 21, 2020. The following companies will be added: X (Nasdaq:X). The following companies will be removed: Y (Nasdaq:Y).')

def test_lseg_catalog_preserves_exact_misspelled_effective_field_not_snapshot():
    from backtest_data.indices.parsers import parse_lseg_catalog
    rows,total=parse_lseg_catalog({'Data':[{'id':Decimal('123'),'FamilyMnemonic':'RUSSELL-US','title':'Reconstitution','AnnouncedDate':'2016-12-30T00:00:00','ElectiveDate':'2017-01-05T00:00:00'}],'TotalCount':Decimal('1')})
    assert total==1 and rows[0]['record_kind']=='announcement'
    assert rows[0]['effective_date']==date(2017,1,5)
    assert rows[0]['fields']['ElectiveDate']=='2017-01-05T00:00:00'
    assert rows[0]['member_identifier'] is None

def test_csv_snapshot_has_independent_snapshot_and_weight_date_exact_units():
    from backtest_data.indices.parsers import parse_snapshot_csv
    rows=parse_snapshot_csv(b'Symbol,Company,Weight\nAAA,Issuer,0.123456789123456789\n',
        index_key='OFFICIAL:INDEX',snapshot_date=date(2016,1,4),weight_date=date(2016,1,5),
        published_date=None,weight_unit='percent',expected_member_count=1)
    assert rows[0]['weight']==Decimal('0.123456789123456789')
    assert rows[0]['snapshot_date']!=rows[0]['weight_date']
    assert rows[0]['published_date'] is None and rows[0]['record_kind']=='snapshot_member'
    assert rows[0]['fields']['declared_member_count']==1
    with pytest.raises(ValueError):parse_snapshot_csv(b'Symbol,Company,Weight\nAAA,Issuer,0.1\n',index_key='OFFICIAL:INDEX',snapshot_date=date(2016,1,4),weight_date=date(2016,1,4),published_date=None,weight_unit='percent',expected_member_count=2)

def test_requests_frozen_and_fullscope_filters_apply_after_announcement():
    from backtest_data.indices.requests import lseg_catalog_request, original_scope
    request=lseg_catalog_request(2016,0)
    assert request.method=='POST' and request.payload()['family_mnemonic']==''
    assert original_scope()=={'start':'2016-01-01','end':'2026-10-07','target_count':17758,'market_counts':{'KR':4507,'US':13251}}
    with pytest.raises(Exception):request.url='https://evil.example'

def test_normalization_future_announcements_and_weights_fail_closed():
    from backtest_data.indices.storage import normalize_record
    cap=datetime(2026,10,10,tzinfo=timezone.utc)
    base={'record_kind':'change','index_key':'NASDAQ:NDX','row_key':'add:X','action':'add','member_identifier':'X','effective_date':date(2026,12,1),'published_date':date(2026,10,7),'effective_phase':'before_open','fields':{},'issues':[]}
    normalized=normalize_record(base,cap)
    assert 'effective_after_original_cutoff' in normalized['issues'] and normalized['canonical_eligible'] is False
    assert normalized['weight'] is None
    with pytest.raises(ValueError):normalize_record({**base,'published_date':date(2026,10,8)},cap)
    with pytest.raises(ValueError):normalize_record({**base,'weight':0.1,'weight_date':date(2026,10,7),'weight_unit':'fraction'},cap)

def test_membership_view_never_reconstructs_from_changes_without_baseline():
    from backtest_data.indices.query import membership_from_records
    row={'record_kind':'change','index_key':'NASDAQ:NDX','action':'add','member_identifier':'AAA','published_date':date(2016,1,4),'effective_date':date(2016,1,5),'effective_phase':'before_open'}
    result=membership_from_records([row],index_key='NASDAQ:NDX',on=date(2016,1,6),as_of=date(2016,1,6))
    assert result['status']=='unavailable' and 'baseline_unavailable' in result['issues'] and result['members']==[]

def test_membership_snapshot_future_public_and_weight_day_are_not_substituted():
    from backtest_data.indices.query import membership_from_records
    row={'record_kind':'snapshot_member','index_key':'NASDAQ:NDX','member_identifier':'AAA','snapshot_date':date(2016,1,4),'published_date':date(2016,1,8),'weight_date':date(2016,1,4),'weight':Decimal('1'),'fields':{'declared_member_count':1,'source_snapshot_complete':True}}
    assert membership_from_records([row],index_key='NASDAQ:NDX',on=date(2016,1,4),as_of=date(2016,1,7))['status']=='unavailable'
    assert membership_from_records([row],index_key='NASDAQ:NDX',on=date(2016,1,4),as_of=date(2016,1,8))['status']=='source_snapshot'

def test_snapshot_query_does_not_expose_future_indicative_weight_as_same_day_weight():
    from backtest_data.indices.query import membership_from_records
    row={'record_kind':'snapshot_member','index_key':'NASDAQ:NDX','member_identifier':'AAA','snapshot_date':date(2016,1,4),'published_date':date(2016,1,4),'weight_date':date(2016,1,8),'weight':Decimal('0.5'),'fields':{'declared_member_count':1,'source_snapshot_complete':True}}
    result=membership_from_records([row],index_key='NASDAQ:NDX',on=date(2016,1,4),as_of=date(2016,1,4))
    assert result['members'][0]['weight'] is None
    assert 'weight_date_not_snapshot_day' in result['issues']
    assert row['weight']==Decimal('0.5')

def test_future_dated_snapshot_is_not_actual_membership_before_snapshot_day():
    from backtest_data.indices.query import membership_from_records
    row={'record_kind':'snapshot_member','index_key':'OFFICIAL:TEST','member_identifier':'AAA','snapshot_date':date(2016,1,8),'published_date':date(2016,1,4),'fields':{'declared_member_count':1,'source_snapshot_complete':True}}
    result=membership_from_records([row],index_key='OFFICIAL:TEST',on=date(2016,1,8),as_of=date(2016,1,4))
    assert result['status']=='unavailable' and 'future_snapshot_unavailable' in result['issues']

def test_download_context_cannot_mislabel_other_russell_index(monkeypatch):
    from backtest_data.indices import acquisition as mod
    from backtest_data.indices.requests import public_download_requests
    monkeypatch.setattr(mod,'pdf_pages',lambda _:['Russell Microcap® Index - Additions\nCompany Symbol Industry\nExample AAA Health Care'])
    with pytest.raises(ValueError):mod.parse_response(public_download_requests()[0],b'%PDF-source')

def test_public_request_post_body_cannot_smuggle_config_or_credentials():
    from backtest_data.indices.requests import IndexRequest,LSEG_CATALOG
    with pytest.raises(ValueError):IndexRequest('lseg',LSEG_CATALOG,'lseg_catalog','POST',json_body='{"apikey":"not-allowed"}')

def test_public_client_collection_uses_existing_shared_source_rate_key(monkeypatch):
    from backtest_data.indices import acquisition as mod
    from backtest_data.indices.requests import IndexRequest
    import httpx
    class RateSession:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def begin(self):return self
    keys=[]
    def reserve(session,key,now,interval,**kwargs):keys.append((key,interval));return now
    monkeypatch.setattr(mod,'Session',lambda _:RateSession())
    monkeypatch.setattr(mod,'reserve_rate_slot',reserve)
    transport=httpx.MockTransport(lambda _:httpx.Response(200,content=b'public'))
    with mod.PublicIndexClient(engine=object(),transport=transport) as client:
        client.fetch(IndexRequest('lseg','https://www.lseg.com/test','russell_pdf'),timeout_seconds=1)
    assert keys==[('lseg:shared_account',1.0)]

def test_request_source_and_port_cannot_escape_declared_official_provider():
    from backtest_data.indices.requests import IndexRequest
    with pytest.raises(ValueError):IndexRequest('nasdaq_index','https://www.lseg.com/test','nasdaq_html')
    with pytest.raises(ValueError):IndexRequest('lseg','https://www.lseg.com:444/test','russell_pdf')

def test_catalog_pagination_is_source_count_not_target_denominator():
    from backtest_data.indices.acquisition import parse_response
    from backtest_data.indices.requests import lseg_catalog_request
    response=b'{"Data":[{"id":1,"FamilyMnemonic":"RUSSELL-US","title":"Notice","AnnouncedDate":"2016-01-04T00:00:00","ElectiveDate":"2016-01-05T00:00:00"}],"TotalCount":3}'
    rows,followups=parse_response(lseg_catalog_request(2016,0,page_size=1),response)
    assert len(rows)==1 and followups[0].payload()['page_number']==1
    assert followups[0].payload()['family_mnemonic']==''
    with pytest.raises(ValueError):parse_response(lseg_catalog_request(2016,1,page_size=1),b'{"Data":[],"TotalCount":3}')

def test_catalog_nonfinal_short_page_is_not_accepted_as_exhausted():
    from backtest_data.indices.acquisition import parse_response
    from backtest_data.indices.requests import lseg_catalog_request
    raw=b'{"Data":[{"id":1,"AnnouncedDate":"2016-01-04T00:00:00"}],"TotalCount":3}'
    with pytest.raises(ValueError):parse_response(lseg_catalog_request(2016,0,page_size=2),raw)

def test_exact_snapshot_can_have_known_publication_before_output_start():
    from backtest_data.indices.storage import normalize_record
    row={'record_kind':'snapshot_member','index_key':'OFFICIAL:TEST','row_key':'AAA','member_identifier':'AAA','snapshot_date':date(2016,1,1),'published_date':date(2015,12,31),'fields':{},'issues':[]}
    result=normalize_record(row,datetime(2026,10,10,tzinfo=timezone.utc))
    assert result['published_date']==date(2015,12,31) and not result['canonical_eligible']

def test_catalog_source_dates_beyond_cutoff_are_archived_but_not_staged_as_in_scope():
    from backtest_data.indices.acquisition import parse_response
    from backtest_data.indices.requests import lseg_catalog_request
    raw=b'{"Data":[{"id":1,"AnnouncedDate":"2026-10-08T00:00:00","ElectiveDate":"2026-12-01T00:00:00"}],"TotalCount":1}'
    rows,followups=parse_response(lseg_catalog_request(2026,0),raw)
    assert rows==[] and followups==[]

def test_nasdaq_catalog_follows_only_same_provider_news_and_known_page():
    from backtest_data.indices.acquisition import parse_response
    from backtest_data.indices.requests import nasdaq_catalog_request
    content=b'<a href="/news-releases/news-release-details/annual-changes-nasdaq-100-index-5">Annual Changes to the Nasdaq-100 Index</a><a href="https://evil.example/x">Nasdaq-100</a><a rel="next" href="/search?page=1">Next</a>'
    rows,requests=parse_response(nasdaq_catalog_request(2016),content)
    assert rows==[] and len(requests)==2
    assert requests[0].parser=='nasdaq_html' and requests[1].parser=='nasdaq_catalog'
    assert requests[1].params[-1]==('page','1')

def test_public_client_rejects_credentials_and_bounds_streamed_bytes():
    from backtest_data.indices.acquisition import PublicIndexClient
    from backtest_data.indices.requests import IndexRequest
    import httpx
    with pytest.raises(ValueError):IndexRequest('lseg','https://www.lseg.com/test','russell_pdf',params=(('apikey','no'),))
    transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'12345'))
    with PublicIndexClient(max_bytes=4,transport=transport) as client:
        with pytest.raises(ValueError):client.fetch(IndexRequest('lseg','https://www.lseg.com/test','russell_pdf'),timeout_seconds=1)

def test_supplemental_projection_preserves_change_action_and_never_promotes_catalog():
    from backtest_data.indices.acquisition import supplemental_record
    from types import SimpleNamespace
    source=SimpleNamespace(record_kind='index_member',entity_key='S&P 500',row_key='1:2',member_identifier=None,member_name='Issuer',announced_date=date(2016,1,22),published_date=date(2016,1,22),effective_date=date(2016,1,29),fields={'action':'ADDED','effective_phase':'after_close','cells':['Issuer','Tech']},issues=['weight_unavailable'])
    row=supplemental_record(source)
    assert row['record_kind']=='change' and row['index_key']=='SPDJI:SP500' and row['action']=='add'
    assert row['member_identifier'] is None and row['published_date']==source.published_date
    assert 'historical_security_identity_unresolved' in row['issues']
