"""Explicit source-evidence allowlist; payloads cannot nominate SQL operations."""
from datetime import date, datetime
from decimal import Decimal
import copy
import json
import re
from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, JSON, Text, cast, literal
from .codec import dumps, PaginationCursor

ALLOWED_TABLES = frozenset({"source_contract", "issuer", "security", "identifier_history", "exchange_session",
    "catalog_observation", "source_qualification", "target_universe_manifest", "raw_object", "ingestion_attempt",
    "filing", "financial_source_document", "source_financial_fact", "fundamental_fact", "fund_product_fact",
    "financial_publication_evidence", "financial_daily_publication_evidence", "selection_decision", "validation_issue", "derived_fact",
    "price_observation", "corporate_action_observation", "vwap_daily_stat",
    "krx_quote_observation", "supplemental_document", "supplemental_observation", "supplemental_api_call",
    "index_history_record"})


def tables():
    from ..db import Base, load_models
    load_models()
    from . import models
    return {name: Base.metadata.tables[name] for name in ALLOWED_TABLES}


def validate_row(row):
    if set(row) != {"table", "values"} or row["table"] not in ALLOWED_TABLES:
        raise ValueError("table outside explicit source allowlist")
    table = tables()[row["table"]]
    values = row["values"]
    if not isinstance(values, dict) or set(values) != set(table.c.keys()):
        raise ValueError("complete original SQL columns required")
    for column in table.c:
        value = values[column.name]
        if value is None:
            if not column.nullable: raise ValueError("nonnullable SQL column missing")
            continue
        if isinstance(column.type, Boolean):
            if type(value) is not bool: raise ValueError("exact boolean required")
        elif isinstance(column.type, Numeric):
            if isinstance(value, bool) or not isinstance(value, (Decimal, int)): raise ValueError("exact Decimal required")
        elif isinstance(column.type, DateTime):
            if not isinstance(value, datetime) or value.tzinfo is None: raise ValueError("aware original SQL clock required")
        elif isinstance(column.type, Date):
            if type(value) is not date: raise ValueError("exact original SQL date required")
        elif isinstance(column.type, Integer):
            if type(value) is not int: raise ValueError("exact integer required")
    logical_parents(row)
    dumps(row)
    return table


def logical_parents(row):
    """Explicit validated dependencies for audited lineage outside SQL FKs."""
    name,values=row['table'],row['values'];result=[]
    def reference(domain,identity):
        if domain not in ALLOWED_TABLES or not isinstance(identity,str) or not 1<=len(identity)<=120:
            raise ValueError('unknown or unbound evidence lineage')
        result.append((domain,identity))
    if name=='derived_fact':
        entity_domain={'issuer':'issuer','security':'security'}.get(values.get('entity_type'))
        if entity_domain is None:raise ValueError('unknown derived entity lineage')
        reference(entity_domain,values.get('entity_id'))
        refs=values.get('input_refs')
        if not isinstance(refs,list) or not 1<=len(refs)<=4096:raise ValueError('bounded derived input lineage required')
        for ref in refs:
            if not isinstance(ref,dict) or set(ref)!={'domain','id'}:raise ValueError('invalid derived input lineage')
            reference(ref['domain'],ref['id'])
    elif name=='selection_decision':
        reference(values.get('record_domain'),values.get('record_id'))
    elif name=='financial_daily_publication_evidence':
        refs=values.get('evidence',{}).get('calendar_evidence_ids',[])
        if not isinstance(refs,list) or len(refs)>4096:raise ValueError('bounded calendar lineage required')
        for identity in refs:reference('exchange_session',identity)
    elif name=='vwap_daily_stat':
        evidence=values.get('evidence',{})
        for field,domain in (('page_document_ids','supplemental_document'),('krx_quote_ids','krx_quote_observation'),
                ('identifier_history_ids','identifier_history')):
            refs=evidence.get(field,[])
            if not isinstance(refs,list) or len(refs)>4096:raise ValueError('bounded VWAP source lineage required')
            for identity in refs:reference(domain,identity)
    elif name=='supplemental_document':
        original=values.get('request_scope',{}).get('revalidation_of_document_id')
        if original is not None:reference('supplemental_document',original)
    elif name=='target_universe_manifest':
        members=values.get('members')
        if not isinstance(members,list) or not 1<=len(members)<=17758:raise ValueError('bounded target member lineage required')
        for member in members:
            if not isinstance(member,dict):raise ValueError('invalid target member lineage')
            reference('security',member.get('security_id'))
            if member.get('issuer_id') is not None:reference('issuer',member['issuer_id'])
        evidence=values.get('scope',{}).get('directory_evidence',[])
        if not isinstance(evidence,list) or len(evidence)>1000:raise ValueError('bounded directory lineage required')
        for capture in evidence:
            if not isinstance(capture,dict):raise ValueError('invalid directory lineage')
            reference('raw_object',capture.get('raw_id'))
    elif name=='catalog_observation' and values.get('source')=='kis_overseas_master':
        reference('raw_object',values.get('fields',{}).get('master_raw_id'))
    elif name=='validation_issue':
        if values.get('record_id') is not None:
            reference({'price':'price_observation'}.get(values.get('record_domain'),values.get('record_domain')),values['record_id'])
        evidence=values.get('evidence',{})
        if values.get('rule') in {'supplier_price_reconciliation_v1','supplier_price_reconciliation_v2'}:
            refs=evidence.get('observation_ids',[])
            if not isinstance(refs,list) or not 1<=len(refs)<=64:raise ValueError('bounded price comparison lineage required')
            for identity in refs:reference('price_observation',identity)
        if not isinstance(evidence,dict):raise ValueError('invalid validation issue lineage')
        for field,domain in (('security_id','security'),('raw_id','raw_object')):
            if evidence.get(field) is not None:reference(domain,evidence[field])
    return result


def wire_row(row):
    """Type only the known Alpaca request-evidence pagination field."""
    result=copy.deepcopy(row)
    if result['table']=='ingestion_attempt':
        evidence=result['values'].get('evidence',{})
        if re.fullmatch(r'https://data\.alpaca\.markets/v2/stocks/[^/]+/bars',evidence.get('endpoint','')):
            params=evidence.get('params',{})
            if isinstance(params.get('page_token'),str):params['page_token']=PaginationCursor(params['page_token'])
    return result


def database_values(row):
    def unwrap(value):
        if isinstance(value,PaginationCursor):return value.value
        if isinstance(value,dict):return {k:unwrap(v) for k,v in value.items()}
        if isinstance(value,list):return [unwrap(v) for v in value]
        return value
    values=unwrap(row['values'])
    for column in tables()[row['table']].c:
        if isinstance(column.type,JSON) and values[column.name] is not None:
            # Bind exact JSON text as a parameter, never as arbitrary SQL.
            values[column.name]=cast(literal(exact_json_text(values[column.name]),type_=Text),column.type)
    return values


def exact_json_text(value):
    """SQL JSON numbers stay numbers without passing through binary floats."""
    if value is None:return 'null'
    if type(value) is bool:return 'true' if value else 'false'
    if isinstance(value,str):return json.dumps(value,ensure_ascii=False)
    if type(value) is int:return str(value)
    if isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('nonfinite JSON Decimal')
        return str(value)
    if isinstance(value,float):raise ValueError('binary float is forbidden in exact JSON')
    if isinstance(value,list):return '['+','.join(exact_json_text(v) for v in value)+']'
    if isinstance(value,dict):
        if any(not isinstance(k,str) for k in value):raise ValueError('string JSON keys required')
        return '{'+','.join(json.dumps(k,ensure_ascii=False)+':'+exact_json_text(v) for k,v in sorted(value.items()))+'}'
    raise ValueError('unsupported original SQL JSON value')
