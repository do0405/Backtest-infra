"""Pure public issuer parsers and GET request descriptions; no network or storage.

Publication timestamps are unavailable in these sources. A report, effective or
payment date is never promoted to publication evidence. Live catalogs enrich the
universe; they cannot establish a complete historical or delisted universe.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import io
import json
import math
import re
from typing import Any
from urllib.parse import urljoin
import xml.etree.ElementTree as ET


KODEX_BASE = "https://www.samsungfund.com"
TIGER_BASE = "https://investments.miraeasset.com/tigeretf"
ISHARES_BASE = "https://www.ishares.com"
ISHARES_CATALOG_URL = ISHARES_BASE + "/us/products/etf-investments"
ISHARES_WORKBOOK_URL = "https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
SHINHAN_ETN_CATALOG_URL = "https://open.shinhansec.com/etn/product/ETN_PW_FO_0210_01.jsp"
SHINHAN_ETN_ISSUANCE_URL = "https://open.shinhansec.com/etn/company/ETN_PW_FO_0440.jsp"


def _payload(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        return json.loads(value, parse_float=Decimal)
    return value


def _date(value: Any) -> date | None:
    if value is None or value in ("", "-", "--"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"invalid source date: {value!r}")


def _number(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float):
        raise ValueError("binary float source value cannot preserve exact precision")
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric source value")
    text = str(value).strip()
    if text in ("", "-", "--", "N/A"):
        return None
    text = text.replace(",", "").replace("$", "").removesuffix("%")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric source value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("non-finite numeric source value")
    return result


def _key(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid issuer entity identifier")
    return value


def _base(kind: str, entity: str, report: date, fields: dict, basis: str,
          member: str | None = None) -> dict:
    if report is None:
        raise ValueError("source report date is required")
    row = {name: None for name in ("published_date", "effective_date", "ex_date", "record_date", "pay_date",
        "currency", "member_identifier", "member_name", "entity_name", "product_type", "quantity", "value",
        "weight", "nav", "net_assets", "fee_rate")}
    row.update(record_kind=kind, entity_key=str(entity), report_date=report,
               row_key=f"{kind}:{entity}:{report.isoformat()}:{member or ''}",
               fields=dict(fields), issues=[], date_basis=basis)
    return row


def _checked_date(source: Any, requested: date | None) -> date:
    actual = _date(source)
    if actual is None or (requested is not None and actual != _date(requested)):
        raise ValueError("source date does not match requested date")
    return actual


def _get(url: str, **params: Any) -> dict:
    return {"url": url, "params": params}


def kodex_catalog_requests(first_page: Any = None, *, page_size: int = 20) -> list[dict]:
    """Site ignores pageRows; its observed fixed page size is twenty."""
    if page_size != 20:
        raise ValueError("KODEX observed catalog page size is 20")
    items = _payload(first_page) if first_page is not None else []
    total = int(items[0]["totalCnt"]) if items else 20
    return [_get(KODEX_BASE + "/api/v1/kodex/product.do", ordrColm="NAV", ordrSort="DESC", pageNo=p, srchTerm="w")
            for p in range(1, max(1, math.ceil(total / page_size)) + 1)]


def kodex_holdings_request(entity_key: str, report_date: date) -> dict:
    return _get(KODEX_BASE + f"/api/v1/kodex/product-pdf/{_key(entity_key)}.do", gijunYMD=_date(report_date).strftime("%Y.%m.%d"))


def kodex_distributions_request(entity_key: str) -> dict:
    return _get(KODEX_BASE + "/api/v1/kodex/divid-info.do", id=_key(entity_key))


def tiger_catalog_request(*, first_index: int = 0, list_count: int = 500) -> dict:
    if first_index < 0 or list_count < 1:
        raise ValueError("invalid pagination")
    return _get(TIGER_BASE + "/ko/product/search/list.ajax", pdfNameYn="N", pageIndex=1,
                firstIndex=first_index, listCnt=list_count, periodType="short", listType="table", cateNameYn="N")


def tiger_holdings_request(entity_key: str, report_date: date) -> dict:
    return _get(TIGER_BASE + "/ko/product/search/detail/pdfListAjax.ajax", ksdFund=_key(entity_key),
                pageIndex=1, firstIndex=0, listCnt=10000, fixDate=_date(report_date).strftime("%Y.%m.%d"))


def tiger_distributions_request(entity_key: str) -> dict:
    return _get(TIGER_BASE + "/ko/product/search/detail/refDivAjax.ajax", ksdFund=_key(entity_key),
                pageIndex=1, firstIndex=0, listCnt=10000)


def ishares_workbook_request(entity_key: str) -> dict:
    return _get(ISHARES_WORKBOOK_URL, appType="PRODUCT_PAGE", appSubType="ISHARES", targetSite="us-ishares",
                locale="en_US", portfolioId=_key(entity_key), component="fundDownload", userType="individual")


def ishares_catalog_request() -> dict:
    return _get(ISHARES_CATALOG_URL)


def ishares_holdings_requests(product_html: str | bytes) -> list[dict]:
    """Discover offered CSV links; never imply latest CSV honors old date queries."""
    urls = []
    for anchor in _HTML(product_html).anchors:
        url = urljoin(ISHARES_BASE, anchor["href"])
        if re.fullmatch(r"https://www\.ishares\.com/us/products/\d+/[A-Za-z0-9_-]+/latest-holdings\.csv", url) and url not in urls:
            urls.append(url)
    return [_get(url) for url in urls]


def tiger_catalog_requests(catalog_html: str | bytes, *, list_count: int = 500) -> list[dict]:
    if list_count < 1:
        raise ValueError("invalid pagination")
    counts = [int(b["attributes"].get("data-tot-cnt", "0")) for b in _HTML(catalog_html).blocks]
    total = max(counts, default=0)
    return [tiger_catalog_request(first_index=offset, list_count=list_count)
            for offset in range(0, max(total, 1), list_count)]


def parse_kodex_catalog(payload: Any) -> list[dict]:
    result = []
    for raw in _payload(payload):
        row = _base("product", raw["fId"], _date(raw["gijunYMD"]), raw, "issuer_catalog_valuation_date")
        row.update(entity_name=raw.get("fNm"), member_identifier=raw.get("stkTicker"), product_type="ETF",
                   currency="KRW", effective_date=_date(raw.get("listD")), nav=_number(raw.get("basp")))
        # nav is displayed in 100 million KRW, not the per-share NAV field basp.
        row["fields"].update(net_assets_source_field="nav", net_assets_unit="KRW_100_million_unverified")
        row["issues"].append("net_assets_unit_unverified")
        result.append(row)
    return result


def parse_kodex_holdings(payload: Any, entity_key: str, *, requested_date: date) -> list[dict]:
    pdf = _payload(payload)["pdf"]
    asof = _checked_date(pdf.get("gijunYMD"), requested_date)
    result = []
    for raw in pdf.get("list", []):
        member = str(raw.get("itmNo") or raw.get("secNm") or "")
        row = _base("holding", entity_key, asof, raw, "portfolio_deposit_applicable_date", member)
        row.update(effective_date=asof, currency="KRW", member_identifier=raw.get("itmNo"),
                   member_name=raw.get("secNm"), quantity=_number(raw.get("applyQ")),
                   value=_number(raw.get("evalA")), weight=_number(raw.get("ratio")))
        row["fields"].update(quantity_unit="unverified_creation_unit", value_unit="KRW_unverified_creation_unit",
                             weight_unit="unverified_percentage_excluding_cash")
        row["issues"].extend(["quantity_unit_unverified", "value_unit_unverified", "weight_unit_unverified"])
        if raw.get("curp") is not None or raw.get("risep") is not None:
            row["issues"].append("live_quote_not_historical_valuation")
        result.append(row)
    return result


def parse_kodex_distributions(payload: Any, entity_key: str) -> list[dict]:
    result = []
    for raw in _payload(payload).get("dividList", []):
        record, pay = _date(raw.get("basicD")), _date(raw.get("payD"))
        row = _base("distribution", entity_key, record, raw, "distribution_record_date", str(pay))
        row.update(record_date=record, pay_date=pay, currency="KRW", value=_number(raw.get("dividA")))
        row["fields"]["amount_unit"] = "KRW_per_share"
        result.append(row)
    return result


class _HTML(HTMLParser):
    """Retain table cells, product anchors and TIGER catalog divs without JS."""
    def __init__(self, text: str | bytes):
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.blocks: list[dict] = []
        self.anchors: list[dict] = []
        self._row = None
        self._cell = None
        self._anchor = None
        self._block = None
        self._depth = 0
        self.feed(text.decode("utf-8-sig") if isinstance(text, bytes) else text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = {"cells": [], "anchors": [], "attributes": attrs}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        if tag == "a":
            self._anchor = {"href": attrs.get("href", ""), "text": ""}
        if tag == "div":
            if self._block is not None:
                self._depth += 1
            elif "c-data-row" in attrs.get("class", "").split() and attrs.get("data-ksd-fund"):
                self._block = {"attributes": attrs, "name": None, "text": ""}
                self._depth = 1
        if self._block is not None and attrs.get("data-ksd-fund-nm"):
            self._block["name"] = attrs["data-ksd-fund-nm"]

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row["cells"].append(" ".join(self._cell).strip())
            self._cell = None
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        if tag == "a" and self._anchor is not None:
            self._anchor["text"] = self._anchor["text"].strip()
            self.anchors.append(self._anchor)
            if self._row is not None:
                self._row["anchors"].append(self._anchor)
            self._anchor = None
        if tag == "div" and self._block is not None:
            self._depth -= 1
            if self._depth == 0:
                self.blocks.append(self._block)
                self._block = None

    def handle_data(self, text):
        if self._cell is not None and text.strip():
            self._cell.append(text.strip())
        if self._anchor is not None:
            self._anchor["text"] += text
        if self._block is not None:
            self._block["text"] += text


def parse_tiger_catalog(html: str | bytes, *, observation_date: date) -> list[dict]:
    result, seen = [], set()
    for block in _HTML(html).blocks:
        key = block["attributes"]["data-ksd-fund"]
        if key in seen:
            continue
        seen.add(key)
        row = _base("product", key, _date(observation_date), block, "catalog_observation_date")
        row.update(entity_name=block["name"], member_identifier=key, product_type="ETF", currency="KRW")
        row["issues"].append("current_catalog_not_historical_universe")
        result.append(row)
    return result


def parse_tiger_holdings(html: str | bytes, entity_key: str, *, requested_date: date,
                         source_date: date | None = None) -> list[dict]:
    asof = _checked_date(source_date, requested_date) if source_date else _date(requested_date)
    result = []
    for raw in _HTML(html).rows:
        cells = raw["cells"]
        if len(cells) >= 6 and cells[0].isdigit() and len(cells[0]) < 6:
            cells = cells[1:]  # HTML Excel has a leading serial-number column.
        if len(cells) < 5 or not re.fullmatch(r"[A-Za-z0-9_.-]{5,}", cells[0]):
            continue
        fields = {"displayed_identifier": cells[0], "displayed_name": cells[1], "displayed_quantity": cells[2],
                  "displayed_value": cells[3], "displayed_weight": cells[4], "cells": cells,
                  "value_unit": "displayed_KRW_semantics_unverified", "weight_unit": "percent"}
        row = _base("holding", entity_key, asof, fields,
                    "source_portfolio_date" if source_date else "requested_portfolio_date_unconfirmed", cells[0])
        row.update(member_identifier=cells[0], member_name=cells[1], currency="KRW",
                   quantity=_number(cells[2]), value=_number(cells[3]), weight=_number(cells[4]))
        row["issues"].extend(["value_semantics_unverified", "quantity_unit_unverified"])
        if source_date is None:
            row["issues"].append("source_asof_not_present")
        result.append(row)
    return result


def parse_tiger_distributions(html: str | bytes, entity_key: str) -> list[dict]:
    result = []
    for raw in _HTML(html).rows:
        cells = raw["cells"]
        if len(cells) != 4 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        record, pay = _date(cells[0]), _date(cells[1])
        fields = dict(zip(("record_date", "pay_date", "distribution_amount", "tax_amount"), cells))
        row = _base("distribution", entity_key, record, fields, "distribution_record_date", str(pay))
        row.update(record_date=record, pay_date=pay, value=_number(cells[2]), currency="KRW")
        row["fields"]["amount_unit"] = "KRW_per_share"
        result.append(row)
    return result


def parse_ishares_catalog(html: str | bytes, *, observation_date: date) -> list[dict]:
    parsed = _HTML(html)
    result, seen = [], set()
    headers = []
    for raw in parsed.rows:
        cells = raw["cells"]
        if cells and cells[0] == "Ticker":
            headers = cells
            continue
        if "YTD (%)" in headers and "12m Trailing Yield (%)" not in headers:
            continue  # The site's second table contains index mutual funds.
        anchor = next((a for a in raw["anchors"] if re.search(r"/us/products/\d+/", a["href"])), None)
        if anchor is None:
            continue
        key = re.search(r"/us/products/(\d+)/", anchor["href"])[1]
        if key in seen:
            continue
        seen.add(key)
        fields = dict(zip(headers, cells)) if len(headers) == len(cells) else {"cells": cells}
        fields.update(source_headers=headers[:], source_cells=cells[:])
        fields["product_url"] = urljoin(ISHARES_BASE, anchor["href"])
        row = _base("product", key, _date(observation_date), fields, "catalog_observation_date")
        row.update(member_identifier=cells[0] if cells else anchor["text"],
                   entity_name=cells[1] if len(cells) > 1 else None, product_type="ETF", currency="USD")
        row.update(effective_date=_date(fields.get("Incept. Date")),
                   net_assets=_number(fields.get("Net Assets (USD)")),
                   fee_rate=_number(fields.get("Gross Expense Ratio (%)")))
        row["fields"]["fee_rate_unit"] = "percent"
        row["issues"].append("current_catalog_not_historical_universe")
        result.append(row)
    return result


def _ishares_holding_rows(rows: list[list[str]], entity_key: str, *, requested_date: date | None) -> list[dict]:
    asof, header_index = None, None
    for index, values in enumerate(rows):
        if values and values[0] == "Fund Holdings as of" and len(values) > 1:
            asof = _date(values[1])
        if values and values[0] == "Ticker" and "Name" in values:
            header_index = index
            break
    if header_index is None:
        raise ValueError("holdings header is absent")
    asof = _checked_date(asof, requested_date)
    headers, result = rows[header_index], []
    for values in rows[header_index + 1:]:
        if len(values) != len(headers) or not any(values):
            continue  # CSV footer/disclaimer is outside the holdings table.
        raw = dict(zip(headers, values))
        identity = raw.get("ISIN") or raw.get("CUSIP") or raw.get("Ticker") or raw.get("Name")
        member = ":".join(str(raw.get(k, "")) for k in ("Ticker", "Name", "Asset Class", "Location", "Exchange"))
        row = _base("holding", entity_key, asof, raw, "fund_holdings_asof_date", member)
        row.update(member_identifier=identity, member_name=raw.get("Name"), currency=raw.get("Currency"),
                   quantity=_number(raw.get("Quantity")), value=_number(raw.get("Market Value")), weight=_number(raw.get("Weight (%)")))
        row["fields"]["weight_unit"] = "percent"
        result.append(row)
    return result


def parse_ishares_holdings_csv(content: str | bytes, entity_key: str, *, requested_date: date | None = None) -> list[dict]:
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("\ufeff")
    return _ishares_holding_rows(list(csv.reader(io.StringIO(text))), entity_key, requested_date=requested_date)


def parse_ishares_workbook(content: str | bytes, entity_key: str, *, requested_date: date | None = None) -> list[dict]:
    """Read issuer .xls XML worksheets; reject DTD and external entity content."""
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("XML document entities are forbidden")
    # Official XML contains literal unescaped ampersands in fund names.
    text = re.sub(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|amp;|lt;|gt;|quot;|apos;)", "&amp;", text)
    root = ET.fromstring(text)
    ns = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
    result = []
    for sheet in root.findall("ss:Worksheet", ns):
        name = sheet.get("{" + ns["ss"] + "}Name")
        rows = []
        for raw_row in sheet.findall(".//ss:Row", ns):
            cells = []
            for cell in raw_row.findall("ss:Cell", ns):
                index = cell.get("{" + ns["ss"] + "}Index")
                if index:
                    cells.extend([""] * max(0, int(index) - len(cells) - 1))
                data = cell.find("ss:Data", ns)
                cells.append("".join(data.itertext()) if data is not None else "")
            rows.append(cells)
        if name == "Holdings":
            result.extend(_ishares_holding_rows(rows, entity_key, requested_date=requested_date))
        elif name == "Distributions" and rows:
            headers = rows[0]
            for values in rows[1:]:
                if len(values) != len(headers) or not any(values):
                    continue
                raw = dict(zip(headers, values))
                record, ex, pay = (_date(raw.get(k)) for k in ("Record Date", "Ex-Date", "Payable Date"))
                report = record or ex or pay
                row = _base("distribution", entity_key, report, raw, "distribution_record_date" if record else "distribution_event_date", str(pay))
                row.update(record_date=record, ex_date=ex, pay_date=pay, currency="USD", value=_number(raw.get("Total Distribution")))
                row["fields"]["amount_unit"] = "USD_per_share"
                result.append(row)
    return result


def shinhan_etn_issuance_request() -> dict:
    return _get(SHINHAN_ETN_ISSUANCE_URL)


def shinhan_etn_catalog_request() -> dict:
    return _get(SHINHAN_ETN_CATALOG_URL)


def _parse_shinhan_etn_table(content: str | bytes, *, observation_date: date,
                            issuance: bool, encoding: str) -> list[dict]:
    text = content.decode(encoding) if isinstance(content, bytes) else content
    headers, result = [], []
    for source_row in _HTML(text).rows:
        cells = source_row["cells"]
        if "종목명" in cells and ("상장수량" in cells if issuance else "코드" in cells):
            headers = cells
            continue
        if not headers or len(cells) != len(headers):
            continue
        fields = dict(zip(headers, cells))
        name = fields.get("종목명", "").strip()
        if not name or name == "종목명":
            continue
        code = fields.get("코드") or fields.get("종목코드") or None
        # Issuance table has names and amounts but no product code. Its local
        # name identity is explicit and must never be mistaken for a ticker.
        entity = code or "shinhan_etn_name:" + name
        source_report = _date(fields.get("기준일") or fields.get("보고일"))
        report = source_report or _date(observation_date)
        fields.update(source_headers=headers[:], source_cells=cells[:],
                      links=[dict(link) for link in source_row["anchors"]],
                      identity_basis="explicit_source_code" if code else "source_name_only")
        row = _base("product", entity, report, fields,
                    "source_report_date" if source_report else "catalog_observation_date")
        row.update(entity_name=name, member_identifier=code, product_type="ETN",
                   published_date=_date(fields.get("공개일")),
                   effective_date=_date(fields.get("발행일") or fields.get("상장일")),
                   maturity_date=_date(fields.get("만기일") or fields.get("만기일자")))
        row["issues"].append("current_catalog_not_historical_universe")
        if source_report is None:
            row["issues"].append("source_report_date_absent")
        if code is None:
            row["issues"].append("source_product_identifier_missing")
        if issuance:
            row.update(quantity=_number(fields.get("상장수량")), value=_number(fields.get("발행총액")))
            row["fields"].update(quantity_basis="상장수량", value_basis="발행총액")
            row["issues"].append("issuance_amount_currency_unstated")
        else:
            row["fee_rate"] = _number(fields.get("운용보수(%, 연)"))
            row["fields"]["fee_rate_unit"] = "annual_percent"
            if "현재가(원)" in fields:
                row["currency"] = "KRW"
                row["price"] = _number(fields["현재가(원)"])
        result.append(row)
    return result


def parse_shinhan_etn_issuance(content: str | bytes, *, observation_date: date,
                               encoding: str = "euc-kr") -> list[dict]:
    """Parse every official issuance row; absent codes/dates remain unknown."""
    return _parse_shinhan_etn_table(content, observation_date=observation_date, issuance=True, encoding=encoding)


def parse_shinhan_etn_catalog(content: str | bytes, *, observation_date: date,
                             encoding: str = "euc-kr") -> list[dict]:
    """Keep explicitly displayed codes, fees, index names and source links."""
    return _parse_shinhan_etn_table(content, observation_date=observation_date, issuance=False, encoding=encoding)
