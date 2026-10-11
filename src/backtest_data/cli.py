import argparse
from dataclasses import asdict
import json
import sys
from pathlib import Path
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .db import make_engine


def _parser():
    parser = argparse.ArgumentParser(description="Point-in-time clean market dataset")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="Apply PostgreSQL migrations")
    status = sub.add_parser("status", help="Report full target and source gaps")
    status.add_argument("--market", choices=["us", "kr", "both"], default="both")
    universe = sub.add_parser("universe", help="Discover and plan every target")
    universe.add_argument("action", choices=["sync", "link-issuers", "sync-aliases", "sync-kis-us"])
    universe.add_argument("--manifest-id")
    universe.add_argument("--market", choices=["us", "kr", "both"], default="both")
    universe.add_argument("--start", default="2016-01-01")
    universe.add_argument("--end", default=datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat())
    resolve = sub.add_parser("resolve", help="Resolve a current SQL alias to one security; report collisions")
    resolve.add_argument("identifier")
    resolve.add_argument("--market", choices=["us", "kr", "both"], default="both")
    resolve.add_argument("--venue")
    resolve.add_argument("--namespace")
    resolve.add_argument("--manifest-id")
    resolve.add_argument("--as-of")
    resolve.add_argument("--session-date", type=date.fromisoformat)
    resolve.add_argument("--history-mode", choices=["reconstructed_public", "observed_system"], default="observed_system")
    collect = sub.add_parser("collect", help="Run resumable collection without shrinking target scope")
    collect.add_argument("--market", choices=["us", "kr", "both"], default="both")
    collect.add_argument("--domain", choices=["prices", "fundamentals", "indices"], required=True)
    collect.add_argument("--mode", choices=["bootstrap", "incremental", "repair"], default="incremental")
    collect.add_argument("--budget-seconds", type=float)
    collect.add_argument("--source", dest="source_filter", action="append", choices=["kis", "kis_overseas", "alpaca", "opendart", "sec"],
        help="Run only the named supplier queue; repeatable, without switching existing sources")
    sources = sub.add_parser("sources", help="Check configured read-only supplier access")
    sources.add_argument("action", choices=["probe"])
    sources.add_argument("--source", choices=["kis", "opendart", "sec", "alpaca", "krx"], required=True)
    krx = sub.add_parser("krx",help="Stage KRX market rows in SQL before unit/identity qualification")
    krx.add_argument("action",choices=["plan","collect"])
    krx.add_argument("--manifest-id")
    krx.add_argument("--start",type=date.fromisoformat,default=date(2016,1,1))
    krx.add_argument("--end",type=date.fromisoformat,default=date(2026,10,7))
    krx.add_argument("--service",dest="services",action="append",choices=["kospi","kosdaq","konex","etf","etn",
        "kospi_basic","kosdaq_basic","krx_index","kospi_index","kosdaq_index"])
    krx.add_argument("--budget-seconds",type=float,default=300)
    validate = sub.add_parser("validate")
    validate.add_argument("--scope", choices=["all"], default="all")
    normalize = sub.add_parser("normalize", help="Append exact canonical accounts from all eligible SQL source rows")
    normalize.add_argument("action", choices=["fundamentals","financial-completion"])
    normalize.add_argument("--batch-size", type=int, default=500)
    normalize.add_argument('--after-document-id',default='')
    normalize.add_argument('--max-documents',type=int,default=10)
    coverage=sub.add_parser('coverage',help='Persist full-target observed coverage and explicit unknown/missing inputs')
    coverage.add_argument('action',choices=['materialize','report'])
    coverage.add_argument('--manifest-id')
    coverage.add_argument('--run-id')
    coverage.add_argument('--batch-size',type=int,default=25)
    coverage.add_argument('--max-batches',type=int,default=1)
    eligibility=sub.add_parser('coverage-eligibility',help='Append separate calendar/listing eligibility over unchanged coverage')
    eligibility.add_argument('action',choices=['calendar-capture','calendar-import','materialize','report'])
    eligibility.add_argument('--start',type=date.fromisoformat,default=date(2016,1,1))
    eligibility.add_argument('--end',type=date.fromisoformat,default=date(2026,10,7))
    eligibility.add_argument('--run-id',default='1b5527a5-b914-4154-ad4c-4969d7f65755')
    eligibility.add_argument('--overlay-id')
    eligibility.add_argument('--cursor')
    eligibility.add_argument('--batch-size',type=int,default=25)
    eligibility.add_argument('--evidence-cutoff')
    eligibility.add_argument('--profile',choices=['qualified','provider_declared_research'],default='provider_declared_research')
    eligibility.add_argument('--live-account',action='store_true',help='Use live-account calendar read-only endpoint')
    derive = sub.add_parser("derive", help="Materialize financial metrics from eligible point-in-time canonical inputs")
    derive.add_argument("action", choices=["metrics"])
    derive.add_argument("--as-of", required=True)
    derive.add_argument("--history-mode", choices=["reconstructed_public", "observed_system"], required=True)
    derive.add_argument("--metric-id", action="append", default=[])
    derive.add_argument("--batch-size", type=int, default=500)
    derive.add_argument('--query-version',choices=['release_query_v5','release_query_v6','release_query_v7','release_query_v8_daily'],default='release_query_v8_daily')
    release = sub.add_parser("release")
    release.add_argument("action", choices=["build"])
    release.add_argument("--manifest-id", required=True)
    release.add_argument('--query-version',choices=['release_query_v5','release_query_v6','release_query_v7','release_query_v8_daily'],default='release_query_v8_daily')
    release.add_argument('--financial-scope',help='Explicit JSON whole-document research scope')
    release.add_argument('--price-start',type=date.fromisoformat)
    release.add_argument('--price-end',type=date.fromisoformat)
    publication=sub.add_parser('publication',help='Audit and verify complete original financial versions')
    publication.add_argument('action',choices=['audit','verify'])
    publication.add_argument('--manifest-id')
    publication.add_argument('--audit-path',required=True)
    publication.add_argument('--progress-path')
    publication.add_argument('--watermark')
    publication.add_argument('--batch-size',type=int,default=5)
    publication.add_argument('--budget-seconds',type=float,default=300)
    publication.add_argument('--retry-unavailable',action='store_true')
    publication.add_argument('--archive-compressed-mb',type=int,default=64)
    publication.add_argument('--archive-total-mb',type=int,default=512)
    daily=sub.add_parser('publication-daily',help='Use source publication-date metadata for daily PIT')
    daily.add_argument('--manifest-id',required=True)
    daily.add_argument('--progress-path',required=True)
    daily.add_argument('--budget-seconds',type=float,default=300)
    daily.add_argument('--batch-size',type=int,default=25)
    daily.add_argument('--max-documents',type=int)
    supplier=sub.add_parser('supplier-prices',help='Collect independently classified provider adjusted bars')
    supplier.add_argument('--manifest-id',required=True)
    supplier.add_argument('--adjustment',choices=['split','all'],default='split')
    supplier.add_argument('--budget-seconds',type=float,default=600)
    supplier.add_argument('--max-pages',type=int)
    supplier.add_argument('--progress-path',required=True)
    vwap=sub.add_parser('vwap-inputs',help='Collect full-universe matched trade inputs with SQL page checkpoints')
    vwap.add_argument('--manifest-id',required=True)
    vwap.add_argument('--progress-path',required=True)
    vwap.add_argument('--budget-seconds',type=float,default=600)
    vwap.add_argument('--max-pages',type=int)
    indices=sub.add_parser('index-history',help='Acquire official historical index records without inferring baselines')
    indices.add_argument('action',choices=['collect-downloads','project'])
    indices.add_argument('--manifest-id',required=True)
    indices.add_argument('--budget-seconds',type=float,default=60)
    indices.add_argument('--limit',type=int,default=20)
    query = sub.add_parser("query")
    query.add_argument("action", choices=["source-accounts", "fundamentals", "prices", "metrics", "smoke", "resolve"])
    query.add_argument("--release-id", required=True)
    query.add_argument("--as-of", required=True)
    query.add_argument("--history-mode", choices=["reconstructed_public", "observed_system"], required=True)
    query.add_argument("--entity-id", action="append", default=[])
    query.add_argument("--identifier", action="append", default=[], help="Security code or explicit alias (repeatable)")
    query.add_argument("--market", choices=["us", "kr", "both"], default="both")
    query.add_argument("--venue")
    query.add_argument("--namespace")
    query.add_argument("--session-date", type=date.fromisoformat)
    query.add_argument('--basis',choices=['as_traded','provider_adjusted'],default='as_traded')
    query.add_argument("--metric-id", action="append", default=[], help="Explicit registered financial metric (repeatable)")
    query.add_argument("--research-partial", action="store_true")
    maintenance = sub.add_parser("maintenance")
    maintenance.add_argument("action", choices=["backup", "verify-backup", "restore-drill", "repair-dart-inventory", "replay-kis-prices"])
    maintenance.add_argument("--backup-manifest")
    maintenance.add_argument("--audit-path")
    maintenance.add_argument("--progress-path")
    maintenance.add_argument("--budget-seconds", type=float)
    maintenance.add_argument("--requeue-recovered", action="store_true")
    return parser


def _dispatch(args, engine, settings):
    from .credentials import load_credentials
    if args.command=='coverage-eligibility':
        from .coverage.calendar_source import collect_alpaca_calendar,load_calendar_capture,VERSION
        from .coverage.eligibility_service import import_calendar_evidence,materialize_eligibility_overlay,eligibility_overlay_report
        from .db import transaction
        from sqlalchemy import text
        if args.action=='calendar-capture':
            return collect_alpaca_calendar(engine,load_credentials(sources=['alpaca']),settings.raw_root,
                start=args.start,end=args.end,live=args.live_account)
        with transaction(engine) as session:
            session.execute(text("SET LOCAL statement_timeout='30s'"))
            if args.action=='calendar-import':
                capture=load_calendar_capture(session,settings.raw_root,start=args.start,end=args.end)
                if capture is None:raise ValueError('committed calendar capture required')
                row=import_calendar_evidence(session,market='US',source='alpaca',calendar_version=VERSION,
                    returned_rows=capture['rows'],start=args.start,end=args.end,captured_at=capture['captured_at'],
                    source_refs={k:v for k,v in capture.items() if k in ('capture_id','raw_id','body_hash','source_url')},
                    complete_interval=True,authoritative=False)
                return {'status':'calendar_evidence_committed','evidence_id':row.id,'rows':len(row.rows),
                    'grade':'provider_declared','coverage_complete':False}
            if args.action=='report':
                if not args.overlay_id:raise ValueError('explicit overlay identity required')
                return eligibility_overlay_report(session,args.overlay_id,limit=args.batch_size,cursor=args.cursor)
            cutoff=datetime.fromisoformat(args.evidence_cutoff.replace('Z','+00:00')) if args.evidence_cutoff else None
            return materialize_eligibility_overlay(session,run_id=args.run_id,limit=args.batch_size,
                cursor=args.cursor,evidence_cutoff=cutoff,profile=args.profile)
    from .ingestion.http import ProviderHTTP
    from .providers import KISQuotes, OpenDART, SEC
    if args.command == "init-db":
        from alembic import command
        from alembic.config import Config
        command.upgrade(Config("alembic.ini"), "head")
        return {"status": "schema_ready"}
    if args.command=='vwap-inputs':
        from .prices.vwap_collection import collect_vwap
        return collect_vwap(engine,settings,manifest_id=args.manifest_id,progress_path=args.progress_path,
            budget_seconds=args.budget_seconds,max_pages=args.max_pages)
    if args.command=='index-history':
        from .indices.acquisition import collect_index_history,project_supplemental_history
        from .indices.requests import public_download_requests
        if args.action=='project':return project_supplemental_history(engine,manifest_id=args.manifest_id,
            limit=args.limit,budget_seconds=args.budget_seconds)
        return collect_index_history(engine,settings.raw_root,manifest_id=args.manifest_id,
            requests=public_download_requests(),max_requests=args.limit,budget_seconds=args.budget_seconds,
            job_key='russell_public_downloads_v1')
    if args.command == "status":
        from .ops.status import operating_status
        return operating_status(engine, args.market)
    if args.command == "resolve":
        from .catalog.aliases import resolve_alias
        cutoff = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else datetime.now(timezone.utc)
        return resolve_alias(engine, args.identifier, as_of=cutoff, history_mode=args.history_mode,
            market=None if args.market == "both" else args.market.upper(), venue=args.venue,
            namespace=args.namespace, session_date=args.session_date, manifest_id=args.manifest_id)
    if args.command == "universe" and args.action == "sync-aliases":
        from .catalog.aliases import seed_aliases
        return seed_aliases(engine, market=args.market)
    if args.command == "collect":
        from .ingestion.supervisor import run_collection
        return run_collection(engine, settings.raw_root, market=args.market, domain=args.domain, budget_seconds=args.budget_seconds, mode=args.mode,
            source_filter=args.source_filter)
    if args.command == "krx":
        from .prices.krx_quote_collection import plan_krx_quotes,run_krx_quote_collection
        if args.action == "collect":
            return run_krx_quote_collection(engine,settings.raw_root,budget_seconds=args.budget_seconds,services=args.services)
        from sqlalchemy import select
        from .db import transaction
        from .ingestion.models import TargetUniverseManifest
        with transaction(engine) as session:
            manifest = session.get(TargetUniverseManifest,args.manifest_id) if args.manifest_id else session.scalar(
                select(TargetUniverseManifest).order_by(TargetUniverseManifest.captured_at.desc()).limit(1))
            if manifest is None:
                raise ValueError('Target manifest required')
            kwargs = {'services':args.services} if args.services is not None else {}
            return plan_krx_quotes(session,manifest,args.start,args.end,**kwargs)
    if args.command in {"universe", "sources"}:
        http = ProviderHTTP(engine)
        credential_sources = [args.source] if args.command == "sources" else (
            ["kis"] if args.action == "sync-kis-us" else
            [source for source, market in (("opendart", "kr"), ("sec", "us")) if args.market in {market, "both"}])
        credentials = load_credentials(sources=credential_sources)
        try:
            if args.command == "sources":
                if args.source == "krx":
                    from .providers import KRXData
                    response,payload=KRXData(http,credentials).daily_quotes('kospi',date(2026,10,7))
                    return {"status":"access_verified","source":"krx","service":"kospi",
                        "rows":len(payload.get('OutBlock_1') or []),"history_coverage_qualified":False,"matched_vwap":False}
                if args.source == "alpaca":
                    from .prices.adapters.alpaca import AlpacaDailyRequest
                    from .providers import AlpacaData
                    response, payload = AlpacaData(http, credentials).daily_prices(
                        AlpacaDailyRequest(date(2016, 1, 1), date(2016, 1, 5), symbol="AAPL"))
                    return {"status": "access_verified", "source": "alpaca", "feed": "sip", "adjustment": "raw",
                            "rows": len(payload.get("bars") or []), "history_coverage_qualified": False, "matched_vwap": False}
                if args.source == "opendart":
                    response = OpenDART(http, credentials).fetch("corpCode.xml", json_response=False)
                    return {"status": "access_verified", "source": "opendart", "response_bytes": len(response.content), "history_coverage_qualified": False}
                if args.source == "sec":
                    response = SEC(http, credentials).fetch("https://www.sec.gov/files/company_tickers_exchange.json")
                    return {"status": "access_verified", "source": "sec", "response_bytes": len(response.content), "history_coverage_qualified": False}
                from .prices.adapters.kis import KISDailyRequest
                response, payload = KISQuotes(http, credentials).daily_prices(KISDailyRequest("005930", date(2026, 9, 1), date(2026, 10, 6)))
                return {"status": "access_verified", "source": "kis", "raw_option": "1", "rows": len(payload.get("output2", [])), "matched_vwap": False}
            if args.action == "sync":
                from .catalog.collection import sync_universe
                from .catalog.aliases import seed_aliases
                result = sync_universe(engine, http, settings.raw_root, market=args.market, output_start=args.start, output_end=args.end)
                if result.get("manifest_id"):
                    result["aliases"] = seed_aliases(engine, manifest_id=result["manifest_id"], market=args.market)
                return result
            if args.action == "sync-kis-us":
                if args.market == "kr":
                    raise ValueError("KIS overseas catalog requires the US target")
                from .catalog.kis_overseas import sync_kis_overseas_catalog
                return sync_kis_overseas_catalog(engine, http, settings.raw_root,
                    manifest_id=args.manifest_id)
            results = []
            if args.market in {"kr", "both"}:
                from .catalog.corp_codes import sync_corp_codes
                results.append(sync_corp_codes(engine, OpenDART(http, credentials)))
            if args.market in {"us", "both"}:
                from .catalog.sec_directory import sync_sec_directory
                results.append(sync_sec_directory(engine, SEC(http, credentials)))
            from .catalog.aliases import seed_aliases
            return {"status": "partial", "results": results, "aliases": seed_aliases(engine, market=args.market)}
        finally:
            http.close()
    if args.command == "validate":
        from .releases.validation import validate_dataset
        return validate_dataset(engine, settings.raw_root)
    if args.command == "normalize":
        if args.action=='financial-completion':
            from .fundamentals.completion_storage import materialize_financial_completion
            return {'status':'research_partial','coverage_complete':False,
                **asdict(materialize_financial_completion(engine,after_document_id=args.after_document_id,max_documents=args.max_documents))}
        from .fundamentals.materialize import materialize_canonical
        return {"status": "research_partial", "target_scope_preserved": True,
                "calculation_qualification_complete": False, **asdict(materialize_canonical(engine, batch_size=args.batch_size))}
    if args.command=='coverage':
        from .coverage.service import materialize_coverage,coverage_report
        from .db import transaction
        if args.action=='report':
            if not args.run_id:raise ValueError('coverage_run_id_required')
            with transaction(engine) as session:return coverage_report(session,args.run_id)
        return materialize_coverage(engine,manifest_id=args.manifest_id,run_id=args.run_id,
            batch_size=args.batch_size,max_batches=args.max_batches)
    if args.command == "derive":
        from .fundamentals.materialize import materialize_metrics
        cutoff = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        result = materialize_metrics(engine, as_of=cutoff, history_mode=args.history_mode,
                                     batch_size=args.batch_size, metric_ids=args.metric_id or None,
                                     query_version=getattr(args,'query_version','release_query_v5'))
        return {"status": "research_partial", "target_scope_preserved": True, **asdict(result)}
    if args.command == "release":
        from .releases.build import build_release
        scope=json.loads(Path(args.financial_scope).read_text(encoding='utf-8-sig')) if args.financial_scope else None
        price_scope=None
        if args.price_start is not None or args.price_end is not None:
            if args.price_start is None or args.price_end is None:raise ValueError('both_price_scope_dates_required')
            price_scope={'start':args.price_start,'end':args.price_end}
        return build_release(engine, args.manifest_id,query_version=args.query_version,financial_scope=scope,price_scope=price_scope)
    if args.command=='publication-daily':
        from .fundamentals.daily_publication_runner import backfill_daily_publications
        return backfill_daily_publications(engine,manifest_id=args.manifest_id,progress_path=args.progress_path,
            budget_seconds=args.budget_seconds,batch_size=args.batch_size,max_documents=args.max_documents)
    if args.command=='supplier-prices':
        from .prices.supplier_collection import collect_supplier_adjusted
        credentials=load_credentials(sources=['alpaca']);http=ProviderHTTP(engine)
        try:
            return collect_supplier_adjusted(engine,http,credentials,settings.raw_root,manifest_id=args.manifest_id,
                adjustment=args.adjustment,budget_seconds=args.budget_seconds,max_pages=args.max_pages,progress_path=args.progress_path)
        finally:http.close()
    if args.command=='publication':
        from .fundamentals.publication_runner import audit_publications,verify_publications
        if args.action=='audit':
            if not args.manifest_id:raise ValueError('original_target_manifest_required')
            watermark=datetime.fromisoformat(args.watermark.replace('Z','+00:00')) if args.watermark else None
            return audit_publications(engine,manifest_id=args.manifest_id,output_path=args.audit_path,watermark=watermark)
        if not args.progress_path:raise ValueError('explicit_publication_progress_path_required')
        return verify_publications(engine,audit_path=args.audit_path,progress_path=args.progress_path,
            budget_seconds=args.budget_seconds,batch_size=args.batch_size,retry_unavailable=args.retry_unavailable,
            archive_compressed_mb=args.archive_compressed_mb,archive_total_mb=args.archive_total_mb)
    if args.command == "query":
        from .releases.query import cli_query
        return cli_query(engine, args)
    if args.command == "maintenance":
        if args.action == 'replay-kis-prices':
            if not args.audit_path:
                raise ValueError('Explicit KIS validation audit required')
            from .ops.kis_repair import replay_kis_price_audit, requeue_recovered_kis_jobs
            result = replay_kis_price_audit(engine, settings.raw_root, args.audit_path,
                progress_path=args.progress_path, budget_seconds=args.budget_seconds)
            if args.requeue_recovered and result['status'] == 'complete':
                result = {**result, 'queue_recovery':requeue_recovered_kis_jobs(engine, args.audit_path, result)}
            return result
        if args.action == "repair-dart-inventory":
            from .fundamentals.dart_inventory_repair import repair_dart_inventory
            http = ProviderHTTP(engine)
            try:
                return {"status": "inventory_repaired", **repair_dart_inventory(engine, OpenDART(http, load_credentials(sources=["opendart"])))}
            finally:
                http.close()
        from .releases.backup import backup_dataset, verify_backup, restore_drill
        if args.action == "backup":
            return backup_dataset(engine, settings.raw_root)
        if args.action == "restore-drill":
            return restore_drill(engine, args.backup_manifest)
        return verify_backup(args.backup_manifest)
    raise ValueError("Unknown command")


def _emit_json(value):
    # Windows stdout may default to CP949 while subprocess readers expect UTF-8.
    # Use one JSON wire encoding so names from both markets remain intact.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False, default=str))


def main(argv=None) -> int:
    from .ingestion.http import ProviderError
    args = _parser().parse_args(argv)
    engine = None
    try:
        settings = Settings.from_env()
        engine = make_engine(settings.database_url)
        result = _dispatch(args, engine, settings)
        _emit_json(result)
        return 2 if result.get("status") == "blocked" else 0
    except (ValueError, ProviderError, SQLAlchemyError) as exc:
        reason = exc.code if isinstance(exc, ProviderError) else type(exc).__name__
        result={"status": "blocked", "reason": reason, "details_redacted": True}
        if isinstance(exc,SQLAlchemyError):
            from .ops.db_recovery import database_error_payload
            result['database_error']=database_error_payload(exc)
        _emit_json(result)
        return 2
    finally:
        if engine is not None:
            engine.dispose()
