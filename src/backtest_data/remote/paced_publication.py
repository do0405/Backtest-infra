"""Serial exact release publication using existing gh authentication only.

Drafts never become visible to consumers until every ciphertext/control asset
has an exact server SHA-256 and size proof. HTTP response details stay private.
"""
from email.utils import parsedate_to_datetime
import hashlib
import math
from pathlib import Path
import re
import time
from urllib.parse import quote
from .github_http import GhAPI, Response


class PublicationBudgetExceeded(RuntimeError):
    """Known absolute deadline exhaustion; RuntimeError callers remain compatible."""


def _digest(path):
    digest=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024**2),b''):digest.update(block)
    return digest.hexdigest()


def publication_assets(paths, *, max_assets=4096):
    """Validate the whole inventory before streaming any artifact hash."""
    paths = list(paths)
    if type(max_assets) is not int or not 1 <= max_assets <= 4096 or not 1 <= len(paths) <= max_assets:
        raise ValueError('bounded GitHub publication asset list required')
    expected = {}
    for source in paths:
        path = Path(source).absolute()
        name = path.name
        size = path.stat().st_size
        if (name in expected or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', name)
                or not path.is_file() or path.is_symlink() or not 0 < size <= 512 * 1024**2 + 44):
            raise ValueError('exact unique bounded publication asset required')
        expected[name] = {'path': path, 'size': size}
    for proof in expected.values():
        proof['digest'] = 'sha256:' + _digest(proof['path'])
    return expected


class _Publisher:
    def __init__(self,api,*,clock,wall_clock,sleep,budget_seconds,max_retries):
        self.api,self.clock,self.wall_clock,self.sleep=api,clock,wall_clock,sleep
        self.deadline=clock()+budget_seconds;self.retries=max_retries;self.last_write=None

    def wait(self,seconds):
        if not math.isfinite(seconds) or seconds<0 or self.clock()+seconds>=self.deadline:
            raise PublicationBudgetExceeded('GitHub publication budget exhausted')
        target=self.clock()+seconds
        while self.clock()<target:self.sleep(min(60,target-self.clock()))

    def retry_wait(self,response,attempt,*,allow_reconciliation=False):
        headers=response.headers;message=response.data.get('message','') if isinstance(response.data,dict) else ''
        if response.status in {401,403} and isinstance(message,str) and any(value in message.lower() for value in (
            'resource not accessible','bad credentials','requires authentication','permission denied')):
            raise ValueError('GitHub publication authorization requires action')
        rate=response.status==429 or response.status==403 and (
            'retry-after' in headers or headers.get('x-ratelimit-remaining')=='0'
            or isinstance(message,str) and any(value in message.lower() for value in ('secondary rate limit','api rate limit exceeded')))
        if response.status in {401,403} and not rate:raise ValueError('GitHub publication authorization requires action')
        if not rate and response.status!=0 and not 500<=response.status<600:
            raise ValueError('GitHub publication request rejected')
        if attempt>=self.retries and not allow_reconciliation:raise RuntimeError('GitHub publication retry limit exhausted')
        delay=60*2**attempt
        if 'retry-after' in headers:
            try:boundary=float(headers['retry-after'])
            except ValueError:
                try:boundary=parsedate_to_datetime(headers['retry-after']).timestamp()-self.wall_clock()
                except (TypeError,ValueError,OverflowError):raise ValueError('valid GitHub retry boundary required') from None
            if not math.isfinite(boundary) or boundary<0:raise ValueError('valid GitHub retry boundary required')
            delay=max(delay,boundary)
        if headers.get('x-ratelimit-remaining')=='0':
            try:reset=float(headers['x-ratelimit-reset'])-self.wall_clock()+1
            except (KeyError,ValueError):raise ValueError('valid GitHub rate reset boundary required') from None
            if not math.isfinite(reset):raise ValueError('valid GitHub rate reset boundary required')
            delay=max(delay,reset)
        self.wait(delay)

    def request(self,method,url,*,body=None,input_file=None,not_found=False,reconcile=None):
        for attempt in range(self.retries+1):
            if self.clock()>=self.deadline:raise PublicationBudgetExceeded('GitHub publication budget exhausted')
            if method!='GET':
                if self.last_write is not None:self.wait(max(0,2-(self.clock()-self.last_write)))
                self.last_write=self.clock()
            response=self.api.request(method,url,body=body,input_file=input_file)
            if 200<=response.status<300:return response.data
            if not_found and response.status==404:return None
            self.retry_wait(response,attempt,allow_reconciliation=reconcile is not None)
            # A failed upload may already have been accepted. Reconcile exact
            # remote content before any mutation is repeated in this invocation.
            if reconcile is not None:
                result=reconcile()
                if result is not None:return result
            if attempt>=self.retries:raise RuntimeError('GitHub publication retry limit exhausted')
        raise RuntimeError('GitHub publication retry limit exhausted')


def publish_release(gh,repository,tag,paths,*,target,title,notes,control_target=False,api=None,clock=time.monotonic,
                    wall_clock=time.time,sleep=time.sleep,budget_seconds=1800,max_retries=3):
    """Reuse exact draft assets, serially upload missing ones, then publish once."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}',tag):
        raise ValueError('explicit GitHub publication binding required')
    # Code artifacts stay bound to an immutable commit. Delivery control tags
    # opt into the repository's verified main branch to avoid stale workflow
    # differences requiring workflow-write permission from GITHUB_TOKEN.
    code_bound=control_target is False and isinstance(target,str) and re.fullmatch('[a-f0-9]{40}',target)
    control_bound=control_target is True and target=='main'
    if type(control_target) is not bool or not (code_bound or control_bound) or type(budget_seconds) is not int or not 1<=budget_seconds<=(7200 if control_target is True else 2400) or type(max_retries) is not int or not 0<=max_retries<=5:
        raise ValueError('bounded exact GitHub publication target required')
    expected=publication_assets(paths)
    publisher=_Publisher(api or GhAPI(gh),clock=clock,wall_clock=wall_clock,sleep=sleep,budget_seconds=budget_seconds,max_retries=max_retries)
    base=f'repos/{repository}/releases';lookup_url=base+'/tags/'+quote(tag,safe='')
    def bound_release(release):
        if not isinstance(release,dict) or type(release.get('id')) is not int or release['id']<1 or release.get('tag_name')!=tag or release.get('prerelease') is not False or type(release.get('draft')) is not bool:
            raise ValueError('exact GitHub release identity required')
        if release.get('target_commitish',target)!=target:raise ValueError('GitHub release target conflict')
        return release
    def lookup():
        value=publisher.request('GET',lookup_url,not_found=True)
        return bound_release(value) if value is not None else None
    release=lookup()
    if release is None:
        release=bound_release(publisher.request('POST',base,body={'tag_name':tag,'target_commitish':target,'name':title,
            'body':notes,'draft':True,'prerelease':False,'make_latest':'false'},reconcile=lookup))
    release_id=release['id']
    def inventory():
        result={}
        for page in range(1,43):
            values=publisher.request('GET',f'{base}/{release_id}/assets?per_page=100&page={page}')
            if not isinstance(values,list) or len(values)>100:raise ValueError('bounded exact GitHub asset inventory required')
            for value in values:
                if not isinstance(value,dict) or value.get('name') not in expected or value['name'] in result:
                    raise ValueError('unexpected or duplicate GitHub release asset')
                name=value['name'];proof=expected[name]
                if value.get('state')!='uploaded' or value.get('size')!=proof['size'] or value.get('digest')!=proof['digest']:
                    raise ValueError('GitHub release asset hash or size conflict')
                result[name]=value
            if len(values)<100:return result
        raise ValueError('bounded exact GitHub asset inventory required')
    existing=inventory();skipped=len(existing);uploaded=0
    if not release['draft']:
        if set(existing)!=set(expected):raise ValueError('published GitHub release asset set conflict')
        return {'release_id':release_id,'uploaded_assets':0,'skipped_assets':skipped,'published':True}
    upload_url=f'https://uploads.github.com/repos/{repository}/releases/{release_id}/assets'
    if release.get('upload_url')!=upload_url+'{?name,label}':raise ValueError('exact GitHub release upload endpoint required')
    for name,proof in expected.items():
        if name in existing:continue
        if proof['path'].stat().st_size!=proof['size'] or 'sha256:'+_digest(proof['path'])!=proof['digest']:
            raise ValueError('local publication asset changed')
        value=publisher.request('POST',upload_url+'?name='+quote(name,safe=''),input_file=proof['path'],
            reconcile=lambda name=name:inventory().get(name))
        if not isinstance(value,dict) or value.get('name')!=name or value.get('state')!='uploaded' or value.get('size')!=proof['size'] or value.get('digest')!=proof['digest']:
            raise ValueError('uploaded GitHub release asset proof conflict')
        uploaded+=1
    if set(inventory())!=set(expected):raise ValueError('complete GitHub release asset proof required before publishing')
    def published():
        value=lookup()
        if value is not None and not value['draft']:
            if value['id']!=release_id or set(inventory())!=set(expected):raise ValueError('published GitHub release identity conflict')
            return value
        return None
    committed=bound_release(publisher.request('PATCH',f'{base}/{release_id}',body={'draft':False,'make_latest':'false'},reconcile=published))
    if committed['id']!=release_id or committed['draft']:raise ValueError('GitHub release publication proof missing')
    return {'release_id':release_id,'uploaded_assets':uploaded,'skipped_assets':skipped,'published':True}
