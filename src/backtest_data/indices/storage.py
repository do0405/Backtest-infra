"""Immutable index facts linked to existing hash-addressed official captures."""
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy.dialects.postgresql import insert
from ..db import new_id
from ..ingestion.jobs import stable_hash
from ..supplemental.storage import exact_json
from .models import IndexHistoryRecord
from .requests import START,END

DATES=('announced_date','published_date','effective_date','snapshot_date','weight_date')
TEXT=('member_identifier','member_name','action','effective_phase','weight_unit')

def normalize_record(row,captured_at):
    if not isinstance(captured_at,datetime) or captured_at.tzinfo is None: raise ValueError('Aware official capture time required')
    if row.get('record_kind') not in {'snapshot_member','change','announcement','access_gap'} or not row.get('index_key') or not row.get('row_key'):
        raise ValueError('Index source record identity required')
    result={k:row[k] for k in ('record_kind','index_key','row_key')}
    for name in DATES:
        value=row.get(name)
        if value is not None and type(value) is not date: raise ValueError('Exact independent source dates required')
        result[name]=value
    for name in TEXT: result[name]=row.get(name)
    # Output starts in 2016; genuinely earlier source publication stays exact.
    if result['published_date'] and result['published_date'] > END: raise ValueError('Public day outside original cutoff')
    if result['snapshot_date'] and not START <= result['snapshot_date'] <= END: raise ValueError('Snapshot outside original cutoff')
    weight=row.get('weight')
    if weight is not None:
        if not isinstance(weight,(Decimal,int)) or isinstance(weight,bool): raise ValueError('Exact source weight required')
        weight=Decimal(weight)
        if not weight.is_finite() or weight < 0 or abs(weight.as_tuple().exponent)>1000 or len(weight.as_tuple().digits)>1000:
            raise ValueError('Weight numeric envelope exceeded')
        if result['weight_date'] is None or result['weight_unit'] not in {'fraction','percent'}: raise ValueError('Exact weight date and unit required')
    if result['record_kind']=='snapshot_member' and (not result['member_identifier'] or not result['snapshot_date']):
        raise ValueError('Snapshot security identity and day required')
    if result['record_kind']=='change' and (result['action'] not in {'add','delete'} or not result['effective_date']):
        raise ValueError('Explicit change action and effective day required')
    if result['action'] is not None and result['action'] not in {'add','delete'}: raise ValueError('Unknown index action')
    if result['effective_phase'] not in {None,'before_open','after_close','unknown'}: raise ValueError('Unknown effective phase')
    issues=list(row.get('issues',[]))
    if result['effective_date'] and result['effective_date'] > END: issues.append('effective_after_original_cutoff')
    if result['published_date'] is None: issues.append('publication_unavailable')
    if weight is None: issues.append('weight_unavailable')
    result.update(weight=weight,captured_at=captured_at,fields=exact_json(row.get('fields',{})),
                  issues=list(dict.fromkeys(issues)),canonical_eligible=False)
    return result

def store_records(session,document,rows):
    values=[]
    for row in rows:
        record=normalize_record(row,document.captured_at)
        signature=stable_hash(['official_index_history_v1',document.id,exact_json(record)])
        values.append({'id':new_id(),'document_id':document.id,'signature':signature,**record})
    inserted=0
    for offset in range(0,len(values),100):
        inserted += len(session.execute(insert(IndexHistoryRecord).values(values[offset:offset+100])
            .on_conflict_do_nothing(index_elements=[IndexHistoryRecord.signature]).returning(IndexHistoryRecord.id)).all())
    return {'source_rows':len(values),'inserted':inserted,'document_id':document.id,'coverage_complete':False,'canonical_eligible':False}
