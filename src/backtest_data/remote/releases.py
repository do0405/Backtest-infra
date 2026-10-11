"""Committed public Release deltas; credentials never enter transfer files."""
import gzip
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from uuid import UUID
from sqlalchemy import text
from .registry import ALLOWED_TABLES

from .bundle import digest, read_manifest, iter_bundle_rows
from .codec import dumps, loads
from .sql import import_bundle
from ..db import transaction, utcnow

MAX_ASSET=512*1024**2
MAX_STATE=128*1024**2


def github(gh, args, *, output=None, timeout=120):
    if output is None:
        r=subprocess.run([str(gh),*args],capture_output=True,timeout=timeout)
    else:
        with Path(output).open('wb') as f:
            r=subprocess.run([str(gh),*args],stdout=f,stderr=subprocess.PIPE,timeout=timeout)
    if r.returncode:
        failure=(r.stderr or b'').lower()
        if any(v in failure for v in (b'http 401',b'http 403',b'authentication',b'permission denied')):
            raise ValueError('github_access_requires_action')
        raise RuntimeError('github_operation_failed')
    return r.stdout if output is None else None


def release_tag(stream, sequence, *, attempt=None):
    if not re.fullmatch('[a-z0-9-]{1,60}',stream) or type(sequence)is not int or sequence<1:
        raise ValueError('explicit release stream/sequence required')
    if attempt is not None and not re.fullmatch(r'\d+-a\d+',attempt):raise ValueError('explicit hosted run attempt required')
    return 'data-'+stream+'-'+str(sequence).zfill(9)+('-r'+attempt if attempt else '')


def inventory(gh, repo, stream):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('exact repository required')
    pages=json.loads(github(gh,['api','--paginate','--slurp',f'repos/{repo}/releases?per_page=100']))
    values=[]
    pattern=re.compile(r'^data-'+re.escape(stream)+r'-(\d{9,})(?:-r\d+-a\d+)?$')
    for page in pages:
        for value in page:
            match=pattern.fullmatch(value.get('tag_name',''))
            if match and not value.get('draft') and not value.get('prerelease'):
                values.append((int(match[1]),value))
    if len({sequence for sequence,_ in values})!=len(values):raise ValueError('published checkpoint fork requires review')
    return [v for _,v in sorted(values)]


def download_asset(gh,repo,release,name,directory,*,max_bytes=MAX_ASSET):
    assets=[v for v in release['assets'] if v['name']==name]
    if len(assets)!=1 or type(assets[0].get('size'))is not int or not 0<assets[0]['size']<=max_bytes:
        raise ValueError('committed release asset missing or oversized')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    destination=directory/name
    if destination.exists():raise ValueError('fresh release download required')
    github(gh,['api','-H','Accept: application/octet-stream',f"repos/{repo}/releases/assets/{assets[0]['id']}"],output=destination,timeout=300)
    if destination.stat().st_size!=assets[0]['size']:raise ValueError('release download size mismatch')
    remote_digest=assets[0].get('digest')
    if remote_digest and remote_digest!='sha256:'+digest(destination):raise ValueError('GitHub asset digest mismatch')
    return destination


def read_commit(path, *, stream, bootstrap_hash, tag=None):
    if Path(path).stat().st_size>1024**2:raise ValueError('bounded commit required')
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    required={'format','stream_id','start_sequence','end_sequence','previous_bundle_id','head',
              'previous_tag','previous_commit_sha256','bootstrap_sha256','files','cloud_run_id','created_at','counts','source_status','coverage_complete'}
    if set(value)!=required or value['format']!='backtest_release_commit_v1' or value['stream_id']!=stream:
        raise ValueError('explicit committed stream required')
    if value['bootstrap_sha256']!=bootstrap_hash or not re.fullmatch('[a-f0-9]{64}',bootstrap_hash):
        raise ValueError('original catalog binding mismatch')
    if type(value['start_sequence'])is not int or type(value['end_sequence'])is not int or not 1<=value['start_sequence']<=value['end_sequence']:
        raise ValueError('ordered committed sequence required')
    head=value['head']
    try:
        if set(head)!={'bundle_id','sequence','manifest_hash'} or str(UUID(head['bundle_id']))!=head['bundle_id'] or type(head['sequence'])is not int or head['sequence']!=value['end_sequence'] or not re.fullmatch('[a-f0-9]{64}',head['manifest_hash']):
            raise ValueError('invalid head')
        created=datetime.fromisoformat(value['created_at'])
        if created.tzinfo is None or not re.fullmatch(r'\d{1,30}',value['cloud_run_id']):raise ValueError('invalid clock/run')
        if value['start_sequence']==1:
            if any(value[k] is not None for k in ('previous_bundle_id','previous_tag','previous_commit_sha256')):raise ValueError('unexpected predecessor')
        elif str(UUID(value['previous_bundle_id']))!=value['previous_bundle_id'] or not re.fullmatch(re.escape(release_tag(stream,value['start_sequence']-1))+r'(?:-r\d+-a\d+)?',value['previous_tag']) or not re.fullmatch('[a-f0-9]{64}',value['previous_commit_sha256']):
            raise ValueError('invalid predecessor')
        if not isinstance(value['counts'],dict) or any(k not in ALLOWED_TABLES or type(v)is not int or not 0<=v<=10**9 for k,v in value['counts'].items()):raise ValueError('invalid counts')
        if not isinstance(value['source_status'],list) or len(value['source_status'])>1:raise ValueError('bounded KRX outcome list required')
        for outcome in value['source_status']:
            if set(outcome)!={'handler','status','reason','requires_user'} or outcome['handler']!='krx_quotes' or type(outcome['requires_user'])is not bool or not re.fullmatch('[a-z_]{1,60}',outcome['status']) or outcome['reason'] not in {'authentication_required','temporary_provider_wait','unclassified_source_failure',None}:raise ValueError('invalid outcome')
    except (TypeError,KeyError,AttributeError,ValueError) as exc:
        raise ValueError('invalid committed control metadata') from exc
    if tag is not None and not re.fullmatch(re.escape(release_tag(stream,value['end_sequence']))+r'(?:-r\d+-a\d+)?',tag):raise ValueError('release sequence binding mismatch')
    if set(value['files'])!={'delta.zip','state.json.gz'}:raise ValueError('committed data assets required')
    for name,record in value['files'].items():
        if set(record)!={'bytes','sha256'} or type(record['bytes'])is not int or not 0<record['bytes']<=MAX_ASSET or not re.fullmatch('[a-f0-9]{64}',record['sha256']):
            raise ValueError('bounded hashed assets required')
    if value['coverage_complete']is not False:raise ValueError('transport does not qualify coverage')
    return value


def source_status(outcomes):
    """Sanitized actionable status, never provider messages or credentials."""
    result=[]
    for outcome in outcomes:
        status=outcome.get('status','source_error')
        blocked=outcome.get('collection',outcome).get('blocked_sources',{})
        codes={str(code).lower() for code in blocked.values()}
        auth=bool(codes & {'401','403','requires_credentials','authentication_error','access_denied'})
        temporary=outcome.get('retryable') is True or bool(codes) and all(code in {'429','500','502','503','504','020','800','900','rate_limit','lease_lost','network_error','transport_failed','rate_budget_deferred','retry_deferred','shared_call_budget_deferred'} or re.fullmatch(r'http_(?:429|5\d\d)',code) for code in codes)
        failed=(status=='source_error' or bool(codes)) and not temporary
        reason='authentication_required' if auth else 'temporary_provider_wait' if temporary else 'unclassified_source_failure' if failed else None
        result.append({'handler':outcome['handler'],'status':status,'reason':reason,'requires_user':auth or failed})
    return result


def verify_file(path,record):
    if Path(path).stat().st_size!=record['bytes'] or digest(path)!=record['sha256']:
        raise ValueError('committed release asset mismatch')


def write_state(path,checkpoint):
    content=dumps(checkpoint).encode('utf-8')
    if len(content)>MAX_STATE:raise ValueError('portable checkpoint bound exceeded')
    with Path(path).open('wb') as file,gzip.GzipFile(fileobj=file,mode='wb',mtime=0,filename='') as output:output.write(content)


def read_state(path):
    with gzip.open(path,'rb') as source:content=source.read(MAX_STATE+1)
    if len(content)>MAX_STATE:raise ValueError('portable checkpoint expansion bound exceeded')
    return loads(content)


def zip_store(store,path):
    store=Path(store)
    with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
        for file in sorted(store.rglob('*')):
            if file.is_file():
                relative=file.relative_to(store).as_posix()
                if not relative.startswith(('heads/','bundles/')):raise ValueError('only committed source bundles allowed')
                output.write(file,relative)
    if Path(path).stat().st_size>MAX_ASSET:raise ValueError('per-run release asset bound exceeded')


def import_release(engine,store,commit,raw_root):
    """Independent group includes complete parent closure, even after old runs."""
    paths=list((Path(store)/'bundles').glob('*/manifest.json'))
    groups=sorted(((read_manifest(p.parent),p.parent) for p in paths),key=lambda pair:pair[0]['sequence'])
    expected=commit['start_sequence'];previous=commit['previous_bundle_id'];results=[]
    if len(groups)!=commit['end_sequence']-expected+1:raise ValueError('committed group is incomplete')
    for manifest,path in groups:
        if manifest['stream_id']!=commit['stream_id'] or manifest['sequence']!=expected or manifest['previous_bundle_id']!=previous:
            raise ValueError('committed stream chain mismatch')
        for _ in iter_bundle_rows(path):pass
        expected+=1;previous=manifest['bundle_id']
    final,final_path=groups[-1]
    if commit['head']!={'bundle_id':final['bundle_id'],'sequence':final['sequence'],'manifest_hash':digest(final_path/'manifest.json')}:
        raise ValueError('final checkpoint hash mismatch')
    for _,path in groups:
        # Each transaction preserves original values and writes its receipt.
        with transaction(engine) as session:
            session.execute(text("SET LOCAL application_name='backtest_release_sync'"))
            session.execute(text("SET LOCAL statement_timeout='120s'"))
            results.append(import_bundle(session,path,raw_root=raw_root))
    return {'checked_at':utcnow().isoformat(),'status':'local_sql_synced','stream_id':commit['stream_id'],
        'end_sequence':commit['end_sequence'],'bundles':len(results),
        'inserted_rows':sum(value['inserted_rows'] for value in results),'results':results,'coverage_complete':False}
