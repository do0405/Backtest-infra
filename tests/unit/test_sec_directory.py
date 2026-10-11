import json

import pytest

from backtest_data.catalog.sec_directory import parse_sec_directory


def test_current_cik_mapping_keeps_multiple_security_classes():
    body = json.dumps({"fields": ["cik", "name", "ticker", "exchange"],
                       "data": [[1, "Example", "A", "NYSE"], [1, "Example", "B", "NYSE"]]}).encode()
    rows = parse_sec_directory(body)
    assert len(rows) == 2
    assert rows[0]["cik"] == "0000000001"
    with pytest.raises(ValueError, match="schema"):
        parse_sec_directory(b'{"fields": ["ticker"], "data": []}')
