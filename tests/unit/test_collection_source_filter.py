from types import SimpleNamespace

import pytest

from backtest_data.cli import _parser
from backtest_data.ingestion.supervisor import run_collection


def test_cli_explicit_secondary_source_preserves_requested_scope():
    args = _parser().parse_args(["collect", "--market", "us", "--domain", "prices", "--source", "alpaca"])
    assert args.source_filter == ["alpaca"]
    assert args.market == "us" and args.mode == "incremental"


@pytest.mark.parametrize("market,domain,selected", [
    ("kr", "prices", ["alpaca"]), ("us", "prices", ["opendart"]),
    ("both", "fundamentals", ["alpaca"]), ("both", "prices", []),
    ("both", "prices", ["made_up"]),
])
def test_invalid_explicit_source_is_rejected_before_transport(monkeypatch, market, domain, selected):
    import backtest_data.ingestion.supervisor as module
    monkeypatch.setattr(module, "ProviderHTTP", lambda *a, **k: pytest.fail("transport must not open"))
    with pytest.raises(ValueError, match="source"):
        run_collection(None, None, market=market, domain=domain, source_filter=selected)
