"""Publication/date gates and explicit request-traversal evidence.

09:00 Seoul and 06:00 New York are project scheduling choices, not promised
provider publication times. KRX's official publication hour is unverified.
KRX keeps calendar-day requests; price lanes use weekdays, without claiming a
qualified exchange holiday calendar. Filing discovery keeps calendar dates.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


PROJECT_PUBLICATION_GATES = {
    'krx': ('Asia/Seoul', 9, False),
    'kis': ('Asia/Seoul', 9, True),
    'opendart': ('Asia/Seoul', 9, False),
    'alpaca': ('America/New_York', 6, True),
    'kis_overseas': ('America/New_York', 6, True),
    'sec': ('America/New_York', 6, False),
}
LOCAL_REFRESH_SOURCES = {'prices': ('kis', 'kis_overseas'),
    'alpaca_prices': ('alpaca',), 'fundamentals': ('opendart', 'sec')}
TRAVERSED = frozenset({'succeeded', 'unsupported'})


def provider_ready_date(source, as_of):
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError('aware provider scheduling time required')
    if source not in PROJECT_PUBLICATION_GATES:
        raise ValueError('registered cadence source required')
    zone, hour, weekdays = PROJECT_PUBLICATION_GATES[source]
    local = as_of.astimezone(ZoneInfo(zone))
    ready = local.date() - timedelta(days=1 if local.hour >= hour else 2)
    while weekdays and ready.weekday() > 4:
        ready -= timedelta(days=1)
    return ready


def krx_original_complete(jobs, scope, *, planned_through):
    """Prove 39,330 original successes; freshness and queue emptiness are irrelevant."""
    from ..prices.krx_quote_storage import KRX_SERVICES
    start, end = date(2016, 1, 1), date(2026, 10, 7)
    if (scope.get('output_start'), scope.get('output_end'), planned_through) != (
            start.isoformat(), end.isoformat(), end.isoformat()):
        return False
    seen = set()
    for job in jobs:
        request = job.request
        if (job.source != 'krx' or job.domain != 'market_quotes'
                or request.get('manifest_id') != scope['manifest_id'] or 'refresh_anchor' in request):
            continue
        try:
            day = date.fromisoformat(request.get('session_date', ''))
        except (TypeError, ValueError):
            continue
        service = request.get('service')
        if service not in KRX_SERVICES or not start <= day <= end:
            continue
        if job.status != 'succeeded':
            return False
        seen.add((service, day))
    return len(seen) == ((end - start).days + 1) * len(KRX_SERVICES)


def _root_scope(members, issuers, source):
    if source in {'kis', 'kis_overseas', 'alpaca'}:
        market = 'KR' if source == 'kis' else 'US'
        return 'prices', {m['security_id'] for m in members if m['market'] == market}
    if source in {'opendart', 'sec'}:
        market = 'KR' if source == 'opendart' else 'US'
        return 'issuer_discovery', {i['id'] for i in issuers if i['market'] == market}
    raise ValueError('registered local refresh source required')


def original_traversal(jobs, members, issuers, *, source, manifest_id, scope):
    """Terminal unsupported roots are explicit gaps, never successful backfill."""
    domain, expected = _root_scope(members, issuers, source)
    expected = {(domain, entity) for entity in expected}
    if source in {'sec','opendart'}:
        market='KR' if source=='opendart' else 'US'
        expected.update(('fundamentals',m['security_id']) for m in members if m['market']==market)
    seen, gaps, unresolved = set(), set(), False
    for job in jobs:
        if job.source != source or job.status == 'superseded':
            continue
        request = job.request
        if request.get('manifest_id') != manifest_id or request.get('refresh_policy'):
            continue
        if job.status not in TRAVERSED:
            unresolved = True
        identity=(job.domain,job.entity_id)
        if (any(request.get(key) != scope[key] for key in ('output_start', 'output_end')) or identity not in expected):
            continue
        seen.add(identity)
        if job.status == 'unsupported':
            gaps.add(identity)
    return {'request_pass_complete': bool(expected) and seen == expected and not unresolved,
        'terminal_gaps': len(gaps), 'expected_roots': len(expected), 'coverage_complete': False}


def completed_refresh_cutoff(jobs, members, issuers, *, source, manifest_id, scope):
    """Only a whole completed root denominator advances catch-up traversal."""
    domain, expected = _root_scope(members, issuers, source)
    groups, unresolved = {}, set()
    for job in jobs:
        request = job.request
        if (job.source != source or job.domain != domain or job.status == 'superseded'
                or job.entity_id not in expected or request.get('manifest_id') != manifest_id
                or not request.get('refresh_policy')
                or request.get('original_backfill_start') != scope['output_start']
                or request.get('original_backfill_end') != scope['output_end']):
            continue
        try:
            end = date.fromisoformat(request.get('output_end', ''))
        except (TypeError, ValueError):
            continue
        if job.status in TRAVERSED:
            groups.setdefault(end, set()).add(job.entity_id)
        else:
            unresolved.add(end)
    complete = [end for end, entities in groups.items() if expected and entities == expected and end not in unresolved]
    return max([date.fromisoformat(scope['output_end']), *complete])


def prepare_refresh_plan(members,issuers,jobs,*,manifest_id,scope,enabled_sources,as_of,
                         previous=None,max_jobs=250,mappings=None):
    """A frozen, resumable planning cursor; commit before persisting its result."""
    from ..ingestion.jobs import stable_hash
    from ..remote_updates import build_update_jobs
    if type(max_jobs) is not int or not 1<=max_jobs<=1000:raise ValueError('bounded source planning batch required')
    sources=tuple(sorted(enabled_sources));binding={'manifest_id':manifest_id,'scope':dict(scope),
        'sources':list(sources),'target_count':len(members)}
    if previous is not None and any(previous.get(key)!=value for key,value in binding.items()):
        raise ValueError('completed refresh checkpoint binding mismatch')
    proofs={source:original_traversal(jobs,members,issuers,source=source,manifest_id=manifest_id,scope=scope)
        for source in sources}
    if not all(proof['request_pass_complete'] for proof in proofs.values()):
        return {'status':'original_pass_unconfirmed','proofs':proofs,'checkpoint':previous,'jobs':[],'coverage_complete':False}
    if previous is not None:
        as_of=datetime.fromisoformat(previous['as_of'])
        cutoffs={source:date.fromisoformat(previous['source_since'][source]) for source in sources}
    else:
        cutoffs={source:completed_refresh_cutoff(jobs,members,issuers,source=source,manifest_id=manifest_id,scope=scope)
            for source in sources}
    for source,cutoff in cutoffs.items():
        domain,expected=_root_scope(members,issuers,source)
        gaps={job.entity_id for job in jobs if job.source==source and job.domain==domain and job.entity_id in expected
            and job.status=='unsupported' and job.request.get('manifest_id')==manifest_id and job.request.get('refresh_policy')
            and job.request.get('output_end')==cutoff.isoformat()
            and job.request.get('original_backfill_start')==scope['output_start']
            and job.request.get('original_backfill_end')==scope['output_end']}
        proofs[source].update(latest_refresh_cutoff=cutoff.isoformat(),refresh_terminal_gaps=len(gaps))
    specifications=build_update_jobs(members,issuers,manifest_id=manifest_id,original_scope=scope,as_of=as_of,
        enabled_sources=sources,source_since=cutoffs,publication_aware=True,max_catchup_days=31,mappings=mappings)
    fingerprint=stable_hash(specifications)
    cursor=previous.get('cursor',0) if previous else 0
    if type(cursor) is not int or not 0<=cursor<=len(specifications):raise ValueError('completed refresh cursor outside plan')
    if previous is not None and previous.get('plan_hash')!=fingerprint:raise ValueError('completed refresh plan binding changed')
    batch=specifications[cursor:cursor+max_jobs];cursor+=len(batch)
    status='up_to_date' if not specifications else 'ready' if cursor==len(specifications) else 'planning'
    checkpoint={**binding,'as_of':as_of.isoformat(),'source_since':{source:value.isoformat() for source,value in cutoffs.items()},
        'plan_hash':fingerprint,'cursor':cursor,'planned_roots':len(specifications),'status':status,'coverage_complete':False}
    return {'status':status,'proofs':proofs,'checkpoint':checkpoint,'jobs':batch,'coverage_complete':False}
