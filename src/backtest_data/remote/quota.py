"""Durable, pessimistic KRX admission before disposable runners perform HTTP.

An allocation is consumed even if its runner fails before publishing data. This
control namespace deliberately cannot be mistaken for a source-data checkpoint.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from ..db import utcnow
from .krx_publication import BoundedGhAPI, PublicationFailure, publish_control

DAILY_LIMIT = 9500
MAX_RECEIPT_BYTES = 64 * 1024
ZONE = ZoneInfo('Asia/Seoul')
BINDING_FIELDS = {'stream_id', 'manifest_id', 'handoff_id', 'bootstrap_sha256'}


def _clock(value):
    try:result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (TypeError,AttributeError,ValueError):raise ValueError('aware quota clock required') from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('aware quota clock required')
    return result.astimezone(timezone.utc)


def _binding(binding):
    if (set(binding) != BINDING_FIELDS or
            not re.fullmatch(r'[a-z0-9-]{1,60}', str(binding['stream_id'])) or
            not re.fullmatch(r'[a-f0-9]{64}', str(binding['bootstrap_sha256'])) or
            any(not isinstance(binding[field], str) or not 1 <= len(binding[field]) <= 120
                for field in ('manifest_id', 'handoff_id'))):
        raise ValueError('explicit original quota binding required')


def _days(start, end):
    start, end = _clock(start), _clock(end)
    if not start <= end <= start + timedelta(hours=2):
        raise ValueError('bounded hosted quota window required')
    first, last = start.astimezone(ZONE).date(), end.astimezone(ZONE).date()
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def validate_allocation(value, binding):
    _binding(binding)
    required = BINDING_FIELDS | {'format', 'run_id', 'run_attempt', 'created_at',
                                'expires_at', 'daily_limit', 'credits_by_day'}
    if (not isinstance(value, dict) or set(value) != required or
            value['format'] != 'krx_quota_allocation_v1' or
            any(value[field] != binding[field] for field in BINDING_FIELDS) or
            not re.fullmatch(r'[1-9]\d{0,29}', str(value['run_id'])) or
            type(value['run_id']) is not str or type(value['run_attempt']) is not int or
            not 1 <= value['run_attempt'] <= 1000 or value['daily_limit'] != DAILY_LIMIT):
        raise ValueError('invalid or rebound quota allocation')
    days = _days(value['created_at'], value['expires_at'])
    credits = value['credits_by_day']
    if (not isinstance(credits, dict) or set(credits) != set(days) or
            any(type(number) is not int or not 1 <= number <= DAILY_LIMIT for number in credits.values())):
        raise ValueError('bounded allocated Korean days required')
    return value


def plan_allocation(binding, allocations, legacy_runs, *, run_id, run_attempt,
                    now, expires_at, budget_seconds):
    """Never refund an earlier charge or reuse an already admitted attempt."""
    _binding(binding); now, expires_at = _clock(now), _clock(expires_at)
    if type(budget_seconds) is not int or not 30 <= budget_seconds <= 6000:
        raise ValueError('bounded source batch required')
    days = _days(now, expires_at)
    used = {day: 0 for day in days}; identities = set()
    for value in allocations:
        validate_allocation(value, binding)
        identity = (value['run_id'], value['run_attempt'])
        if identity in identities:
            raise ValueError('duplicate quota allocation identity')
        identities.add(identity)
        for day in days:
            used[day] += value['credits_by_day'].get(day, 0)
    if (str(run_id), run_attempt) in identities:
        return None
    legacy_identities = set()
    for run in legacy_runs:
        identity = (str(run['id']), run.get('run_attempt', 1))
        if identity in identities or identity == (str(run_id), run_attempt):
            continue
        if identity in legacy_identities:
            raise ValueError('duplicate legacy hosted attempt')
        legacy_identities.add(identity)
        if run['status'] != 'completed':
            raise ValueError('another remote acquisition remains active')
        start = _clock(run['run_started_at']); end = _clock(run['updated_at'])
        if end < start:
            raise ValueError('legacy acquisition clock unconfirmed')
        # Includes setup/export time: deliberately overestimates old HTTP calls.
        charge = min(DAILY_LIMIT, math.ceil((end - start).total_seconds() / 10) + 8)
        first, last = start.astimezone(ZONE).date().isoformat(), end.astimezone(ZONE).date().isoformat()
        for day in days:
            if first <= day <= last:
                used[day] += charge
    if any(number >= DAILY_LIMIT for number in used.values()):
        return None
    allowance = math.ceil(budget_seconds / 10) + 8
    value = {'format': 'krx_quota_allocation_v1', **binding, 'run_id': str(run_id),
             'run_attempt': run_attempt, 'created_at': now.isoformat(),
             'expires_at': expires_at.isoformat(), 'daily_limit': DAILY_LIMIT,
             'credits_by_day': {day: min(allowance, DAILY_LIMIT - used[day]) for day in days}}
    return validate_allocation(value, binding)


def allocation_tag(value):
    binding = {field: value[field] for field in BINDING_FIELDS}
    validate_allocation(value, binding)
    return f"quota-{value['stream_id']}-r{value['run_id']}-a{value['run_attempt']}"


def publish_allocation(gh, repo, value, work, *, target='main', source_sha=None,
                       budget_seconds=2400, phase_callback=None, api=None):
    """Only exact publication and receipt readback authorize source HTTP."""
    tag = allocation_tag(value); work = Path(work); work.mkdir(parents=True, exist_ok=True)
    path = work / 'quota-allocation.json'
    path.write_text(json.dumps(value, sort_keys=True, indent=2), encoding='utf-8')
    if phase_callback:phase_callback('quota_publication_started')
    result = publish_control(gh, repo, tag, [path], source_sha=source_sha or target,
        run_id=value['run_id'], run_attempt=value['run_attempt'],
        title='KRX request allowance',
        notes='Pessimistic request allocation. Failed runs never refund credits.',
        budget_seconds=budget_seconds, readback_files=('quota-allocation.json',),
        **({'api':api} if api is not None else {}))
    restored = json.loads(result['readback_contents']['quota-allocation.json'])
    validate_allocation(restored, {field: value[field] for field in BINDING_FIELDS})
    if restored != value:raise ValueError('quota allocation readback mismatch')
    published=_clock(result['release']['published_at'])
    observed_at=_clock(utcnow())
    if not _clock(value['created_at'])<=published<=_clock(value['expires_at']) or published>observed_at:
        raise ValueError('quota publication clock conflict')
    if phase_callback:phase_callback('quota_publication_verified')
    return restored


def _pages(api, endpoint, *, actions=False):
    # Every page is an individual bounded REST request. Partial history never
    # grants admission. This transitional scan intentionally remains O(history).
    for page in range(1,65):
        payload=api.get(endpoint+('&' if '?' in endpoint else '?')+f'per_page=100&page={page}')
        values=payload.get('workflow_runs') if actions and isinstance(payload,dict) else payload
        if not isinstance(values,list) or len(values)>100:raise ValueError('complete quota history required')
        yield values
        if len(values)<100:return
    raise ValueError('complete bounded quota history required')


def read_allocations(gh, repo, binding, work, *, now, api=None):
    _binding(binding); now = _clock(now)
    api=api or BoundedGhAPI(gh)
    pattern = re.compile(r'^quota-' + re.escape(binding['stream_id']) + r'-r([1-9]\d{0,29})-a([1-9]\d{0,3})$')
    result = [];identities=set();potential_days=set(_days(now,now+timedelta(hours=2)))
    for page in _pages(api,f'repos/{repo}/releases'):
        for release in page:
            if not isinstance(release,dict):raise ValueError('quota release proof required')
            tag=release.get('tag_name','')
            if not isinstance(tag,str):raise ValueError('quota tag proof required')
            if not tag.startswith('quota-'+binding['stream_id']+'-'):continue
            if not pattern.fullmatch(tag) or release.get('draft') is not False or release.get('prerelease') is not False:
                raise ValueError('uncertain quota publication')
            # Release.created_at can describe the old target commit. Validate
            # receipt contents first, then prune ONLY its allocated Korean days.
            published=_clock(release.get('published_at'))
            assets=release.get('assets')
            if not isinstance(assets,list) or len(assets)!=1:raise ValueError('exact quota receipt asset required')
            asset=assets[0]
            if asset.get('name')!='quota-allocation.json' or type(asset.get('id')) is not int or asset['id']<1 or type(asset.get('size')) is not int or not 0<asset['size']<=MAX_RECEIPT_BYTES:
                raise ValueError('bounded quota receipt required')
            content=api.get(f"repos/{repo}/releases/assets/{asset['id']}",raw=True)
            if not isinstance(content,bytes) or len(content)!=asset['size'] or len(content)>MAX_RECEIPT_BYTES:raise ValueError('quota receipt size mismatch')
            if asset.get('digest') and asset['digest']!='sha256:'+hashlib.sha256(content).hexdigest():raise ValueError('quota receipt digest mismatch')
            value=json.loads(content)
            validate_allocation(value,binding)
            if allocation_tag(value)!=tag:raise ValueError('quota tag identity mismatch')
            created=_clock(value['created_at']);expires=_clock(value['expires_at'])
            if not created<=published<=expires or published>now or created>now:raise ValueError('quota publication clock conflict')
            identity=(value['run_id'],value['run_attempt'])
            if identity in identities:raise ValueError('duplicate quota allocation identity')
            identities.add(identity)
            if potential_days.intersection(value['credits_by_day']):result.append(value)
    return result


def reserve_hosted_allowance(gh, repo, binding, work, *, run_id, run_attempt,
                             budget_seconds, now, target,record_not_before=None, source_sha=None,
                             publication_budget_seconds=2400, phase_callback=None, api=None):
    """Read the current owner lane, conservatively charge legacy generations."""
    try:
        now = _clock(now)
        if api is None:api=BoundedGhAPI(gh,budget_seconds=publication_budget_seconds)
        current=api.get(f'repos/{repo}/actions/runs/{run_id}')
        if str(current.get('id'))!=str(run_id) or current.get('head_sha')!=(source_sha or target) or current['run_attempt'] != run_attempt:
            raise ValueError('hosted attempt binding mismatch')
        deadline = _clock(current['run_started_at']) + timedelta(hours=2)
        if now >= deadline:
            return None
        allocations = read_allocations(gh, repo, binding, Path(work) / 'previous', now=now,api=api)
        pages = _pages(api,f'repos/{repo}/actions/workflows/remote-data-update.yml/runs',actions=True)
        runs = []
        for page in pages:
            for run in page:
                if str(run['id']) == str(run_id):
                    # A rerun may have earlier attempts on different Korean days.
                    attempts = range(1, run_attempt)
                elif _clock(run['updated_at']) >= now - timedelta(days=2):
                    attempts = range(1, run.get('run_attempt', 1) + 1)
                else:
                    continue
                for attempt in attempts:
                    if attempt == run.get('run_attempt', 1):
                        evidence = run
                    else:
                        evidence = api.get(f"repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}")
                    runs.append({**evidence, 'run_attempt': attempt})
        if record_not_before is not None:
            completed=[_clock(run['updated_at']) for run in runs if run['status']=='completed']
            if completed:record_not_before(max(completed)+timedelta(seconds=10))
        value = plan_allocation(binding, allocations, runs, run_id=str(run_id), run_attempt=run_attempt,
                                now=now, expires_at=deadline, budget_seconds=budget_seconds)
        if value is None:
            return None
        return publish_allocation(gh, repo, value, Path(work) / 'admission', target=target,source_sha=source_sha,
            budget_seconds=int(api.remaining()),phase_callback=phase_callback,api=api)
    except PublicationFailure as failure:
        if failure.diagnostic()['reason'] not in {'deadline','request_budget'}:raise
        if phase_callback:phase_callback('quota_publication_deferred',publication_failure=failure.diagnostic())
        return None


def deferred_handler(engine, raw_root, *, checkpoint, **kwargs):
    """Commit the existing checkpoint normally when no remote credit is left."""
    return {'source_handler_state':checkpoint.get('source_handler_state',{}),
            'source_handler_outcomes':[{'handler':'krx_quotes','status':'budget_deferred',
                'collection':{'blocked_sources':{'krx':'rate_budget_deferred'}},'coverage_complete':False}],
            'coverage_complete':False}
