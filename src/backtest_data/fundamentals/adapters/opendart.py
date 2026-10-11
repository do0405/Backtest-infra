"""Pure full-financial JSON extraction; receipt identity is mandatory."""

from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re

from ..domain import Context, ExtractionInventory, ExtractionIssue, ExtractionResult, SourceFact


class ReceiptMismatch(ValueError):
    pass


AMOUNT_FIELDS = ('thstrm_amount', 'thstrm_add_amount', 'frmtrm_amount', 'frmtrm_q_amount', 'frmtrm_add_amount',
                 'bfefrmtrm_amount', 'bfefrmtrm_add_amount')


def parse_financial_json(payload: dict | bytes | str, *, requested_receipt: str,
                         periods: dict[str | tuple[str, str], tuple[str, date | None, date | None]] | None = None,
                         requested_fs_div: str | None = None, requested_corp_code: str | None = None,
                         legacy_context_ids: bool = False,
                         max_bytes: int = 32*1024*1024, max_rows: int = 100000) -> ExtractionResult:
    """Preserve every amount node; legacy contexts are only for hash-bound repair."""
    if isinstance(payload, (bytes, str)):
        if len(payload if isinstance(payload,bytes) else payload.encode('utf-8'))>max_bytes:
            raise ValueError('document_size_limit')
        payload = json.loads(payload,parse_float=Decimal)
    if payload.get('status') != '000':
        raise ValueError(f"dart_response_status:{payload.get('status')}")
    rows = payload.get('list', [])
    if len(rows)>max_rows:
        raise ValueError('account_row_limit')
    if any(str(row.get('rcept_no', '')) != requested_receipt for row in rows):
        raise ReceiptMismatch('latest_only: returned receipt does not match requested original receipt')
    if requested_fs_div is not None and requested_fs_div not in {'CFS', 'OFS'}:
        raise ValueError('invalid_requested_financial_scope')
    if requested_corp_code is not None and any(str(row.get('corp_code', '')) != requested_corp_code for row in rows):
        raise ValueError('corp_code_mismatch')
    if requested_fs_div is not None and any(row.get('fs_div') not in {None, requested_fs_div} for row in rows):
        raise ValueError('financial_scope_mismatch')
    facts: list[SourceFact] = []
    contexts: dict[str, Context] = {}
    issues: list[ExtractionIssue] = []
    periods = periods or {}
    for row_index, row in enumerate(rows):
        for amount_field in AMOUNT_FIELDS:
            if amount_field not in row:
                continue
            raw = str(row[amount_field]) if row[amount_field] is not None else ''
            node_id = f'row:{row_index}:{amount_field}'
            fs_div = requested_fs_div or row.get('fs_div')
            scope = {'CFS': 'consolidated', 'OFS': 'separate'}.get(fs_div, 'unknown')
            period_kind, start, end = periods.get((str(row.get('sj_div', '')), amount_field),
                                                periods.get(amount_field, ('unknown', None, None)))
            dimensions = {'statement': str(row.get('sj_div', '')), 'source_period_field': amount_field,
                          'account_detail': str(row.get('account_detail', ''))}
            context_id = f"{requested_receipt}:{fs_div}:{row.get('sj_div')}:{amount_field}:{row.get('account_detail', '')}"
            if not legacy_context_ids:
                context_id = f"{context_id}:row:{row_index}"
            contexts[context_id] = Context(context_id, entity_identifier=requested_corp_code,
                                           entity_scheme='opendart.corp_code' if requested_corp_code else None,
                                           period_kind=period_kind, period_start=start, period_end=end,
                                           dimensions=dimensions, scope=scope)
            is_nil = not raw.strip() or raw.strip() == '-'
            status = 'ok'
            value = None
            if not is_nil:
                try:
                    if not re.fullmatch(r'[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?',raw.strip()):
                        raise InvalidOperation
                    value = Decimal(raw.replace(',', '').strip())
                    if not value.is_finite():
                        raise InvalidOperation
                except InvalidOperation:
                    status = 'invalid_numeric'
                    issues.append(ExtractionIssue(status, node_id, 'DART amount is not a finite Decimal'))
            facts.append(SourceFact(node_id, str(row.get('account_id') or row.get('account_nm', '')),
                                    'opendart', context_id, value, raw, row.get('currency'), row.get('currency'),
                                    account_name=row.get('account_nm'), dimensions=dimensions, period_kind=period_kind,
                                    period_start=start, period_end=end, scope=scope, is_nil=is_nil, parse_status=status,
                                    source_node_ids=(node_id,), evidence={'receipt': requested_receipt, 'amount_field': amount_field,
                                                                         'source_row': dict(row), 'requested_fs_div': requested_fs_div}))
    return ExtractionResult(tuple(facts), contexts, {},
                            ExtractionInventory(len(facts), len(facts), tuple(contexts), (),
                                                unsupported_nodes=len(issues)), tuple(issues),
                            parser_version='opendart_financial_json_v2' if legacy_context_ids else 'opendart_financial_json_v3')
