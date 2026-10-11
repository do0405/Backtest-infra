import pytest

def test_only_published_data_search_post_is_allowed():
    from backtest_data.supplemental.transport import validate_request,LSEG_CATALOG
    validate_request('lseg','POST',LSEG_CATALOG)
    with pytest.raises(ValueError):validate_request('lseg','POST','https://www.lseg.com/account/billing')

def test_alpaca_orders_and_credentials_in_url_are_rejected():
    from backtest_data.supplemental.transport import validate_request
    validate_request('alpaca','GET','https://data.alpaca.markets/v1/corporate-actions')
    with pytest.raises(ValueError):validate_request('alpaca','POST','https://api.alpaca.markets/v2/orders')
    with pytest.raises(ValueError):validate_request('sec','GET','https://secret@www.sec.gov/files/x')

def test_encoded_allowed_symbol_is_supported_but_encoded_separator_is_not():
    from backtest_data.supplemental.transport import validate_request
    validate_request('alpaca','GET','https://data.alpaca.markets/v2/stocks/ABC%24A/trades')
    with pytest.raises(ValueError):validate_request('alpaca','GET','https://data.alpaca.markets/v2/stocks/ABC%2FA/trades')

def test_cache_is_bound_to_source_url_and_bytes(tmp_path):
    import json,hashlib
    from backtest_data.supplemental.transport import SourceHTTP
    path=tmp_path/'archive.zip';path.write_bytes(b'original')
    receipt=path.with_suffix('.capture.json')
    receipt.write_text(json.dumps({'url':'https://www.sec.gov/files/old.zip','bytes':8,'sha256':hashlib.sha256(b'original').hexdigest()}))
    http=SourceHTTP(None)
    try:
        with pytest.raises(ValueError):http.download('sec','https://www.sec.gov/files/new.zip',path)
        path.write_bytes(b'modified')
        with pytest.raises(ValueError):http.download('sec','https://www.sec.gov/files/old.zip',path)
    finally:http.close()
