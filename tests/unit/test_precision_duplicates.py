from backtest_data.fundamentals.xbrl import parse_xbrl


def instance(values):
    facts = "".join(f'<t:A contextRef="c" unitRef="u" decimals="{decimals}">{value}</t:A>' for value, decimals in values)
    return ('<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:t="urn:test"><context id="c"><entity><identifier scheme="urn:id">1</identifier></entity><period><instant>2025-12-31</instant></period></context><unit id="u"><measure>t:USD</measure></unit>' + facts + '</xbrl>').encode()


def test_reported_rounding_duplicates_are_preserved_without_false_conflict():
    result = parse_xbrl(instance([("403962000", "-3"), ("404000000", "-5")]))
    assert result.complete and len(result.facts) == 2
    assert result.parser_version == "financial_extraction_v5"
    exact = parse_xbrl(instance([("72733", "INF"), ("73000", "-3")]))
    assert exact.complete and len(exact.facts) == 2


def test_disjoint_or_ambiguous_boundary_duplicates_still_quarantine():
    assert not parse_xbrl(instance([("100", "0"), ("200", "0")])).complete
    assert not parse_xbrl(instance([("1", "0"), ("2", "0")])).complete
    assert not parse_xbrl(instance([("0", "0"), ("0.9", "0"), ("1.8", "0")])).complete
