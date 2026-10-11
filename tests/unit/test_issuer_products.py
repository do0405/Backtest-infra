"""Issuer snapshots must not manufacture publication or historical market prices."""
from datetime import date
from decimal import Decimal
import importlib

import pytest


DAY = date(2026, 10, 7)


def issuer():
    name = "backtest_data.supplemental.issuer_products"
    try:
        spec = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "official issuer parsers are required"
    return importlib.import_module(name)


def kodex_pdf(asof="20261007"):
    return {"pdf": {"gijunYMD": asof, "list": [{"itmNo": "005930", "secNm": "삼성전자",
        "applyQ": "201", "evalA": "253260000", "ratio": "21.07", "curp": "262000", "risep": "-6500"}]}}


def test_kodex_rejects_future_or_mismatched_pcf_before_emitting_rows():
    with pytest.raises(ValueError, match="date"):
        issuer().parse_kodex_holdings(kodex_pdf("20261012"), "2ETF01", requested_date=DAY)


def test_kodex_preserves_exact_values_and_marks_units_without_using_live_quote():
    row = issuer().parse_kodex_holdings(kodex_pdf(), "2ETF01", requested_date=DAY)[0]
    assert (row["quantity"], row["value"], row["weight"]) == (Decimal("201"), Decimal("253260000"), Decimal("21.07"))
    assert row["published_date"] is None
    assert row["report_date"] == DAY
    assert row["fields"]["curp"] == "262000"
    assert "value_unit_unverified" in row["issues"]
    assert "live_quote_not_historical_valuation" in row["issues"]


def test_binary_float_input_is_rejected_instead_of_claiming_exact_precision():
    payload = kodex_pdf()
    payload["pdf"]["list"][0]["ratio"] = 21.07
    with pytest.raises(ValueError, match="float"):
        issuer().parse_kodex_holdings(payload, "2ETF01", requested_date=DAY)


def test_kodex_catalog_discovers_all_pages_and_separate_dates():
    payload = [{"fId": "2ETF01", "fNm": "KODEX 200", "stkTicker": "069500", "listD": "20021014",
                "gijunYMD": "20261007", "totalCnt": "243", "basp": "105618.8700", "nav": "245831"}]
    mod = issuer()
    row = mod.parse_kodex_catalog(payload)[0]
    assert row["entity_key"] == "2ETF01"
    assert row["member_identifier"] == "069500"
    assert row["effective_date"] == date(2002, 10, 14)
    assert row["published_date"] is None
    assert row["nav"] == Decimal("105618.8700")
    requests = mod.kodex_catalog_requests(payload)
    assert len(requests) == 13
    assert requests[-1]["params"]["pageNo"] == 13
    assert all(set(r) == {"url", "params"} for r in requests)


def test_kodex_distributions_never_conflate_basis_and_payment_dates():
    rows = issuer().parse_kodex_distributions({"dividList": [{"basicD": "20260731", "payD": "20260804",
        "dividA": "183", "taxDividA": "168", "dividY": "0.2049"}]}, "2ETF01")
    assert rows[0]["record_date"] == date(2026, 7, 31)
    assert rows[0]["pay_date"] == date(2026, 8, 4)
    assert rows[0]["ex_date"] is None
    assert rows[0]["published_date"] is None


def test_tiger_catalog_uses_isin_identity_and_retains_full_snapshot():
    html = '<div class="c-data-row" data-tot-cnt="233" data-ksd-fund="KR7102110004">' \
           '<input data-ksd-fund-nm="TIGER 200" value="KR7102110004"><span>102110</span></div>'
    row = issuer().parse_tiger_catalog(html, observation_date=DAY)[0]
    assert row["entity_key"] == "KR7102110004"
    assert row["entity_name"] == "TIGER 200"
    assert row["published_date"] is None
    assert issuer().tiger_catalog_request()["params"]["listCnt"] >= 233


def test_tiger_historical_values_keep_displayed_units_and_request_date_uncertainty():
    html = '<tr><td>005930</td><td>삼성전자</td><td>201</td><td>1,260,000</td><td>20.69</td><td>-5.07</td></tr>'
    row = issuer().parse_tiger_holdings(html, "KR7102110004", requested_date=date(2016, 1, 4))[0]
    assert row["value"] == Decimal("1260000")
    assert row["quantity"] == Decimal("201")
    assert row["fields"]["displayed_value"] == "1,260,000"
    assert "value_semantics_unverified" in row["issues"]
    assert "source_asof_not_present" in row["issues"]


def test_tiger_date_mismatch_is_rejected():
    with pytest.raises(ValueError, match="date"):
        issuer().parse_tiger_holdings("", "KR7102110004", requested_date=DAY, source_date=date(2026, 10, 8))


def test_tiger_distribution_html_and_request_helpers():
    html = '<tr><td>2026-07-31</td><td>2026-08-04</td><td>133</td><td>125</td></tr>'
    mod = issuer()
    row = mod.parse_tiger_distributions(html, "KR7102110004")[0]
    assert row["record_date"] == date(2026, 7, 31)
    assert row["pay_date"] == date(2026, 8, 4)
    assert row["value"] == Decimal("133")
    assert mod.tiger_holdings_request("KR7102110004", DAY)["params"]["fixDate"] == "2026.10.07"
    assert mod.kodex_holdings_request("2ETF01", DAY)["params"]["gijunYMD"] == "2026.10.07"
    assert "id" in mod.kodex_distributions_request("2ETF01")["params"]
    assert "ksdFund" in mod.tiger_distributions_request("KR7102110004")["params"]


def test_ishares_csv_requires_actual_asof_instead_of_ignored_query_parameter():
    csv = 'iShares Core S&P 500 ETF\nFund Holdings as of,"Oct 08, 2026"\n\nTicker,Name,Weight (%),Quantity,Market Value,Currency\nNVDA,NVIDIA,8.3200,321703458,74146212999.84,USD\n'
    with pytest.raises(ValueError, match="date"):
        issuer().parse_ishares_holdings_csv(csv, "239726", requested_date=DAY)
    row = issuer().parse_ishares_holdings_csv(csv, "239726")[0]
    assert row["report_date"] == date(2026, 10, 8)
    assert row["weight"] == Decimal("8.3200")
    assert row["published_date"] is None


def test_ishares_catalog_discovers_identifiers_without_truncating_to_examples():
    html = '<table><tr><th>Ticker</th><th>Name</th></tr><tr><td><a href="/us/products/239726/ishares-core-sp-500-etf">IVV</a></td><td>iShares Core S&amp;P 500 ETF</td></tr><tr><td><a href="/us/products/239458/ishares-msci-emerging-markets-etf">EEM</a></td><td>iShares MSCI Emerging Markets ETF</td></tr></table>'
    rows = issuer().parse_ishares_catalog(html, observation_date=DAY)
    assert [r["entity_key"] for r in rows] == ["239726", "239458"]
    assert rows[0]["member_identifier"] == "IVV"
    assert issuer().ishares_workbook_request("239726")["params"]["component"] == "fundDownload"


def test_ishares_xml_keeps_record_ex_payment_and_exact_amount():
    xml = '''<?xml version="1.0"?><ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><ss:Worksheet ss:Name="Distributions"><ss:Table><ss:Row><ss:Cell><ss:Data>Record Date</ss:Data></ss:Cell><ss:Cell><ss:Data>Ex-Date</ss:Data></ss:Cell><ss:Cell><ss:Data>Payable Date</ss:Data></ss:Cell><ss:Cell><ss:Data>Total Distribution</ss:Data></ss:Cell></ss:Row><ss:Row><ss:Cell><ss:Data>Jun 23, 2000</ss:Data></ss:Cell><ss:Cell><ss:Data>Jun 21, 2000</ss:Data></ss:Cell><ss:Cell><ss:Data>Jun 30, 2000</ss:Data></ss:Cell><ss:Cell><ss:Data>0.119400</ss:Data></ss:Cell></ss:Row></ss:Table></ss:Worksheet></ss:Workbook>'''
    row = issuer().parse_ishares_workbook(xml, "239726")[0]
    assert (row["record_date"], row["ex_date"], row["pay_date"]) == (date(2000, 6, 23), date(2000, 6, 21), date(2000, 6, 30))
    assert row["value"] == Decimal("0.119400")
    assert row["published_date"] is None


def test_contract_fields_are_complete_for_every_kind():
    required = {"record_kind", "entity_key", "row_key", "report_date", "published_date", "effective_date",
        "ex_date", "record_date", "pay_date", "currency", "member_identifier", "member_name", "entity_name",
        "product_type", "quantity", "value", "weight", "nav", "net_assets", "fee_rate", "fields", "issues", "date_basis"}
    row = issuer().parse_kodex_holdings(kodex_pdf(), "2ETF01", requested_date=DAY)[0]
    assert required <= row.keys()


def test_ishares_catalog_excludes_separate_mutual_fund_table():
    html = '<table><tr><th>Ticker</th><th>Name</th><th>12m Trailing Yield (%)</th></tr>' \
        '<tr><td><a href="/us/products/239726/ivv">IVV</a></td><td>iShares Core ETF</td><td>1.01</td></tr></table>' \
        '<table><tr><th>Ticker</th><th>Name</th><th>YTD (%)</th><th>NAV</th></tr>' \
        '<tr><td><a href="/us/products/227724/index-fund">MDIIX</a></td><td>Index Fund</td><td>1</td><td>20.97</td></tr></table>'
    rows = issuer().parse_ishares_catalog(html, observation_date=DAY)
    assert [r["entity_key"] for r in rows] == ["239726"]


def test_request_discovery_uses_only_official_csv_link_and_full_tiger_pagination():
    mod = issuer()
    html = '<a href="/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv">Download</a>' \
           '<a href="https://other.example/latest-holdings.csv">Other</a>'
    requests = mod.ishares_holdings_requests(html)
    assert len(requests) == 1
    assert requests[0]["url"].startswith("https://www.ishares.com/us/products/")
    assert requests[0]["params"] == {}
    assert mod.ishares_catalog_request()["url"] == mod.ISHARES_CATALOG_URL
    html = '<div class="c-data-row" data-tot-cnt="700" data-ksd-fund="KR7102110004"></div>'
    assert [r["params"]["firstIndex"] for r in mod.tiger_catalog_requests(html)] == [0, 500]


def test_xml_external_entities_are_rejected():
    with pytest.raises(ValueError, match="entities"):
        issuer().parse_ishares_workbook('<!DOCTYPE root [<!ENTITY file SYSTEM "file:///secret">]><root/>', "239726")


def test_ishares_metadata_keeps_inception_separate_from_observation_and_fee_units():
    html = '<tr><th>Ticker</th><th>Name</th><th>Incept. Date</th><th>Gross Expense Ratio (%)</th><th>Net Assets (USD)</th></tr>' \
           '<tr><td><a href="/us/products/239726/ivv">IVV</a></td><td>iShares ETF</td><td>May 15, 2000</td><td>0.0300</td><td>2,643,344,035.01</td></tr>'
    row = issuer().parse_ishares_catalog(html, observation_date=DAY)[0]
    assert row["effective_date"] == date(2000, 5, 15)
    assert row["report_date"] == DAY
    assert row["fee_rate"] == Decimal("0.0300")
    assert row["fields"]["fee_rate_unit"] == "percent"
    assert row["net_assets"] == Decimal("2643344035.01")


def test_shinhan_issuance_decodes_euc_kr_and_retains_every_row_without_inventing_codes():
    html = '<table><tr><th>종목명</th><th>상장수량</th><th>발행원본액</th><th>발행총액</th></tr>' + ''.join(
        f'<tr><td>신한 인버스 ETN {i}</td><td>2,000,000</td><td>20,000,000,000</td><td>20,000,000,000</td></tr>'
        for i in range(52)) + '</table>'
    rows = issuer().parse_shinhan_etn_issuance(html.encode('euc-kr'), observation_date=DAY)
    assert len(rows) == 52
    assert len({row["row_key"] for row in rows}) == 52
    assert rows[0]["entity_name"] == "신한 인버스 ETN 0"
    assert rows[0]["quantity"] == Decimal("2000000")
    assert rows[0]["value"] == Decimal("20000000000")
    assert rows[0]["member_identifier"] is None
    assert rows[0]["maturity_date"] is None
    assert rows[0]["published_date"] is None
    assert rows[0]["report_date"] == DAY
    assert rows[0]["currency"] is None
    assert "source_product_identifier_missing" in rows[0]["issues"]
    assert rows[0]["fields"]["발행원본액"] == "20,000,000,000"
    assert "leverage" not in rows[0]["fields"]


def test_shinhan_catalog_preserves_explicit_codes_fee_index_without_name_based_leverage():
    html = '<tr><th>구분</th><th>종목명</th><th>코드</th><th>현재가(원)</th><th>운용보수(%, 연)</th><th>기초지수</th></tr>' \
        '<tr><td>국내주식</td><td><a href="ETN_PW_FO_0220_01.jsp?stbd_code=500069">신한 레버리지 ETN</a></td>' \
        '<td>500069</td><td>169,455</td><td>0.2500</td><td>코스피 200 선물 TWAP 레버리지 지수</td></tr>'
    row = issuer().parse_shinhan_etn_catalog(html, observation_date=DAY)[0]
    assert row["entity_key"] == "500069"
    assert row["member_identifier"] == "500069"
    assert row["fee_rate"] == Decimal("0.2500")
    assert row["fields"]["기초지수"] == "코스피 200 선물 TWAP 레버리지 지수"
    assert row["fields"]["fee_rate_unit"] == "annual_percent"
    assert row["published_date"] is None
    assert row["maturity_date"] is None
    assert "leverage" not in row["fields"]


def test_shinhan_explicit_report_publication_issue_and_maturity_dates_are_separate():
    html = '<tr><th>종목명</th><th>상장수량</th><th>발행원본액</th><th>발행총액</th><th>기준일</th><th>공개일</th><th>발행일</th><th>만기일</th></tr>' \
        '<tr><td>신한 ETN</td><td>1</td><td>100</td><td>100</td><td>2026-09-30</td><td>2026-10-02</td><td>2020-08-01</td><td>2030-08-01</td></tr>'
    row = issuer().parse_shinhan_etn_issuance(html, observation_date=DAY)[0]
    assert row["report_date"] == date(2026, 9, 30)
    assert row["published_date"] == date(2026, 10, 2)
    assert row["effective_date"] == date(2020, 8, 1)
    assert row["maturity_date"] == date(2030, 8, 1)


def test_shinhan_requests_are_public_read_only_catalog_shapes():
    mod = issuer()
    assert mod.shinhan_etn_issuance_request() == {"url": mod.SHINHAN_ETN_ISSUANCE_URL, "params": {}}
    assert mod.shinhan_etn_catalog_request() == {"url": mod.SHINHAN_ETN_CATALOG_URL, "params": {}}
