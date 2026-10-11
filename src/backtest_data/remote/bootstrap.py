"""Read-only whole-target catalog bootstrap; no numeric history or live jobs."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
from sqlalchemy import select
from ..catalog.models import Security,IdentifierHistory,CatalogObservation,ExchangeSession,SourceContract
from ..db import make_engine,transaction
from ..ingestion.models import TargetUniverseManifest
from .folder import FolderTransport
from .bundle import safe_path
from .sql import export_deltas


def bootstrap_binding(target):
    if target is None or len(target.members)!=17758:raise ValueError('whole original target required')
    if Counter(m.get('market') for m in target.members)!=Counter({'KR':4507,'US':13251}):
        raise ValueError('whole original market target required')
    identities=[m.get('security_id') for m in target.members]
    if any(not isinstance(identity,str) or not identity for identity in identities):raise ValueError('original security identity required')
    if len(set(identities))!=len(identities):raise ValueError('duplicate original target identity')
    if (target.scope.get('output_start'),target.scope.get('output_end'))!=('2016-01-01','2026-10-07'):
        raise ValueError('original backfill scope required')
    return {'manifest_id':target.id,'target_hash':target.content_hash,'target_count':17758,
            'market_target_counts':{'KR':4507,'US':13251},'output_start':'2016-01-01','output_end':'2026-10-07'},identities


def bootstrap_roots(session,manifest_id):
    target=session.get(TargetUniverseManifest,manifest_id);scope,identities=bootstrap_binding(target)
    rows=session.execute(select(Security.id,Security.market,Security.source_identifier,Security.issuer_id).where(Security.id.in_(identities))).all()
    if len(rows)!=17758:raise ValueError('whole bootstrap security population missing')
    expected={member['security_id']:member['market'] for member in target.members}
    if any(market!=expected[identity] for identity,market,_,_ in rows):raise ValueError('bootstrap security market mismatch')
    catalog_keys=set(identities)|{identifier for _,_,identifier,_ in rows}
    catalog_keys.update(m['symbol'] for m in target.members if m.get('symbol'))
    catalog_keys.update(issuer for _,_,_,issuer in rows if issuer)
    roots={
        'source_contract':list(session.scalars(select(SourceContract.id).order_by(SourceContract.id))),
        'security':identities,
        'identifier_history':list(session.scalars(select(IdentifierHistory.id).where(IdentifierHistory.security_id.in_(identities)).order_by(IdentifierHistory.id))),
        'catalog_observation':list(session.scalars(select(CatalogObservation.id).where(CatalogObservation.identifier.in_(catalog_keys)).order_by(CatalogObservation.id))),
        'exchange_session':list(session.scalars(select(ExchangeSession.id).where(ExchangeSession.market.in_({'KR','US'})).order_by(ExchangeSession.id))),
        # Emit after preceding catalog chunks, reusing their original parents.
        'target_universe_manifest':[manifest_id],
    }
    return scope,roots


def create_catalog_bootstrap(session,*,manifest_id,output_root,private_store,stream_id,max_private_bytes,
                             raw_root,max_roots_per_bundle=5000):
    transport=FolderTransport(private_store,max_retained_bytes=max_private_bytes)
    if safe_path(private_store,'heads/'+stream_id+'.json').exists():raise ValueError('bootstrap requires a new independent stream')
    scope,roots=bootstrap_roots(session,manifest_id)
    checkpoint={'version':2,'bootstrap_kind':'catalog_only','task_state':[],'completed_task_ids':[],
                'identity_handoff_ready':False,'coverage_complete':False,
                'data_export_cursor':{'stream_id':stream_id,'sequence':1,'previous_bundle_id':None}}
    paths=export_deltas(session,roots,output_root,stream_id=stream_id,sequence=1,scope=scope,
        raw_root=raw_root,checkpoint=checkpoint,max_roots_per_bundle=max_roots_per_bundle)
    head=transport.publish_group(paths)
    return {'status':'private_catalog_bootstrap_prepared','head':head,'delta_chunks':len(paths),
            'scope':scope,'identity_handoff_ready':False,'coverage_complete':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Private whole-target catalog bootstrap; SQL reads only')
    parser.add_argument('--manifest-id',required=True);parser.add_argument('--stream-id',required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--private-store',type=Path,required=True)
    parser.add_argument('--raw-root',type=Path,required=True);parser.add_argument('--max-private-bytes',type=int,required=True)
    args=parser.parse_args(argv);engine=make_engine(os.environ['BACKTEST_DATABASE_URL'])
    try:
        with transaction(engine) as session:
            # Enforce read-only SQL independently of caller account permissions.
            from sqlalchemy import text
            session.execute(text('SET TRANSACTION READ ONLY'))
            result=create_catalog_bootstrap(session,manifest_id=args.manifest_id,output_root=args.output,
                private_store=args.private_store,stream_id=args.stream_id,raw_root=args.raw_root,max_private_bytes=args.max_private_bytes)
        print(json.dumps(result));return 0
    finally:engine.dispose()


if __name__=='__main__':raise SystemExit(main())
