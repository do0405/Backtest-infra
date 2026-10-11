from datetime import date
from decimal import Decimal

def test_form25_filing_date_is_not_a_delisting_date():
    from backtest_data.supplemental.official_catalogs import parse_edgar_delistings
    raw=b'CIK|Company Name|Form Type|Date Filed|Filename\n123|Issuer|25-NSE|2016-01-04|edgar/data/123/0000000123-16-000001.txt\n'
    row=parse_edgar_delistings(raw,start=date(2016,1,1),end=date(2026,10,7))[0]
    assert row['filing_date']==date(2016,1,4)
    assert row.get('effective_date') is None and row.get('published_date') is None

def test_lseg_preserves_provider_effective_date_field_and_later_effective_year():
    from backtest_data.supplemental.official_catalogs import parse_lseg
    row=parse_lseg({'Data':[{'id':Decimal('1'),'FamilyMnemonic':'FTSE','AnnouncedDate':'2016-12-30T00:00:00','ElectiveDate':'2017-01-05T00:00:00','title':'Deletion'}],'TotalCount':Decimal('1')})[0]
    assert row['announced_date']==date(2016,12,30)
    assert row['effective_date']==date(2017,1,5)
    assert row.get('weight') is None and row['record_kind']=='index_announcement'

def test_lseg_http200_error_is_not_data_success():
    from backtest_data.supplemental.official_catalogs import parse_lseg
    import pytest
    with pytest.raises(ValueError):parse_lseg({'ErrorCode':'400','ErrorDetails':'No Results found'})
