"""Source snapshot inspection only; never claim missing historical baseline."""
from collections import defaultdict
from datetime import date

def membership_from_records(records,*,index_key,on,as_of):
    if type(on) is not date or type(as_of) is not date: raise ValueError('Exact source query dates required')
    if on>as_of:
        return {'status':'unavailable','members':[],'coverage_complete':False,'issues':['future_snapshot_unavailable']}
    groups=defaultdict(list)
    for row in records:
        if row.get('index_key') != index_key or row.get('record_kind')!='snapshot_member' or row.get('snapshot_date')!=on:
            continue
        public=row.get('published_date')
        if public is None or public>as_of: continue
        groups[row.get('document_id','provided_snapshot')].append(row)
    valid=[]
    for rows in groups.values():
        declared={row.get('fields',{}).get('declared_member_count') for row in rows}
        symbols={row.get('member_identifier') for row in rows}
        if len(declared)==1 and next(iter(declared))==len(rows)==len(symbols) and None not in symbols and all(row.get('fields',{}).get('source_snapshot_complete') is True for row in rows):
            valid.append(rows)
    if len(valid)!=1:
        return {'status':'unavailable','members':[],'coverage_complete':False,'issues':['baseline_unavailable' if not valid else 'ambiguous_source_snapshots']}
    members=[];issues=['historical_security_identity_unresolved','no_continuous_history_claim']
    for source in valid[0]:
        row=dict(source)
        if row.get('weight') is not None and row.get('weight_date')!=on:
            row['weight']=None;issues.append('weight_date_not_snapshot_day')
        members.append(row)
    return {'status':'source_snapshot','members':members,'coverage_complete':False,'canonical_eligible':False,
            'issues':list(dict.fromkeys(issues))}
