from backtest_data.fundamentals.adapters.opendart import parse_financial_json


def duplicate_accounts():
    row = {"rcept_no": "20260320000123", "corp_code": "00123456", "bsns_year": "2025",
           "reprt_code": "11011", "sj_div": "IS", "account_id": "-표준계정코드 미사용-",
           "account_nm": "First anonymous account", "currency": "KRW", "thstrm_amount": "100"}
    return {"status": "000", "list": [row, {**row, "account_nm": "Second anonymous account"}]}


def test_each_response_account_row_has_a_distinct_context_and_signature():
    parsed = parse_financial_json(duplicate_accounts(), requested_receipt="20260320000123",
                                 requested_corp_code="00123456", requested_fs_div="CFS")
    assert parsed.parser_version == "opendart_financial_json_v3"
    assert len({fact.signature for fact in parsed.facts}) == 2
    assert len(parsed.contexts) == parsed.inventory.numeric_nodes == parsed.inventory.extracted_nodes == 2
    assert [fact.source_node_ids for fact in parsed.facts] == [
        ("row:0:thstrm_amount",), ("row:1:thstrm_amount",)]


def test_recovery_mode_preserves_exact_v2_contexts_and_collision_signatures():
    parsed = parse_financial_json(duplicate_accounts(), requested_receipt="20260320000123",
                                 requested_corp_code="00123456", requested_fs_div="CFS",
                                 legacy_context_ids=True)
    assert parsed.parser_version == "opendart_financial_json_v2"
    assert {fact.context_id for fact in parsed.facts} == {"20260320000123:CFS:IS:thstrm_amount:"}
    assert len({fact.signature for fact in parsed.facts}) == 1
