"""Bounded read-only SQL snapshots; no collection, fallback source or release invention."""
from contextlib import contextmanager
from datetime import date,datetime,time,timedelta,timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from backtest_data.catalog.models import SourceContract
from backtest_data.coverage.calendar_source import load_calendar_capture
from backtest_data.coverage.policy import provisional_weekdays
from backtest_data.prices.provider_basis import contract_price_basis,supplier_daily_visible
from .contracts import AnalysisDataset,Bar,SeriesIdentity,canonical_hash,aware

DEFAULT_RUN='1b5527a5-b914-4154-ad4c-4969d7f65755'
FIELDS=('security_id','source_contract_id','basis','feed','venue','trade_scope','currency','quantity_unit')


def identity_for(row,contract):
    definition=contract_price_basis(contract)
    if row['basis']!=definition['basis']:raise ValueError('row and supplier basis conflict')
    return SeriesIdentity(row['security_id'],row['source_contract_id'],contract.market,contract.source,
        row['currency'],row['venue'],row['feed'],row['trade_scope'],row['basis'],
        tuple(definition['adjustments']),definition['volume_basis'],row['quantity_unit'])


class SQLProjector:
    def __init__(self,engine,raw_root,*,run_id=DEFAULT_RUN,origin='actual_source'):
        self.engine,self.raw_root,self.run_id,self.origin=engine,raw_root,run_id,origin
    @contextmanager
    def _read(self):
        with self.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                connection.execute(text('SET TRANSACTION READ ONLY'))
                connection.execute(text("SET LOCAL statement_timeout='20s'"))
                with Session(bind=connection,autoflush=False) as session:
                    yield session
    def search(self,query,market,limit):
        with self._read() as session:
            rows=session.execute(text("SELECT s.id AS security_id,s.name,s.market,s.source_identifier AS identifier "
                "FROM security s WHERE (:market='both' OR s.market=:market) AND "
                "(s.id=:identity OR s.source_identifier ILIKE :query OR s.name ILIKE :query) AND EXISTS "
                "(SELECT 1 FROM coverage_member m WHERE m.run_id=:run AND m.security_id=s.id) "
                "ORDER BY s.market,s.source_identifier,s.id LIMIT :limit"),
                {'market':market,'identity':query,'query':'%'+query.replace('%','\\%').replace('_','\\_')+'%',
                 'run':self.run_id,'limit':limit}).mappings().all()
        return [{**dict(r),'historical_identity':'unknown'} for r in rows]
    def series(self,security_id,start,end):
        if start>end or (end-start).days>20*366:raise ValueError('bounded ordered date window required')
        with self._read() as session:
            member=session.execute(text('SELECT 1 FROM coverage_member WHERE run_id=:run AND security_id=:id LIMIT 1'),
                {'run':self.run_id,'id':security_id}).first()
            if not member:raise ValueError('security outside original target')
            rows=session.execute(text('SELECT DISTINCT security_id,source_contract_id,basis,feed,venue,trade_scope,currency,quantity_unit '
                'FROM price_observation WHERE security_id=:id AND session BETWEEN :start AND :end LIMIT 101'),
                {'id':security_id,'start':start,'end':end}).mappings().all()
            if len(rows)>100:raise ValueError('explicit narrower source window required')
            result=[]
            for row in rows:
                contract=session.get(SourceContract,row['source_contract_id'])
                try:identity=identity_for(row,contract)
                except ValueError:continue
                from .dataset_store import wire
                result.append({**wire(identity),'series_key':identity.key})
        return sorted(result,key=lambda r:r['series_key'])
    def dataset(self,selections,start,end,cutoff):
        with self._read() as session:
            snapshot=session.execute(text('SELECT transaction_timestamp()')).scalar_one()
            return self.in_session(session,selections,start,end,cutoff,snapshot)
    def in_session(self,session,selections,start,end,cutoff,snapshot):
        cutoff=aware(cutoff)
        if cutoff>snapshot or start>end or (end-start).days>20*366 or not 1<=len(selections)<=20:
            raise ValueError('bounded past snapshot and selection required')
        target=session.execute(text('SELECT manifest_id,manifest_hash,target_count,output_start,output_end '
            'FROM coverage_run WHERE id=:run'),{'run':self.run_id}).mappings().one()
        if not target['output_start']<=start<=end<=target['output_end']:
            raise ValueError('date window outside original collection range')
        bars=[];gaps={'historical_universe_unverified','subset_selection_bias'};markets=set()
        for selection in selections:
            before_count=len(bars)
            contract=session.get(SourceContract,selection['source_contract_id'])
            if contract is None or contract.domain!='prices':raise ValueError('price contract required')
            identity=identity_for(selection,contract);markets.add(identity.market)
            if not session.execute(text('SELECT 1 FROM coverage_member WHERE run_id=:run AND security_id=:id LIMIT 1'),
                {'run':self.run_id,'id':identity.security_id}).first():raise ValueError('security outside original target')
            if selection.get('series_key')!=identity.key:raise ValueError('explicit full series identity changed')
            params={name:selection[name] for name in FIELDS}
            params.update(start=start,end=end,snapshot=snapshot)
            where=' AND '.join('p.'+name+'=:'+name for name in FIELDS)
            # The newest observation is chosen BEFORE adoption filtering. A new
            # quarantined revision cannot silently fall back to an older value.
            query=text('WITH newest AS MATERIALIZED (SELECT DISTINCT ON (p.session) p.* FROM price_observation p WHERE '+where+
                ' AND p.session BETWEEN :start AND :end AND p.captured_at<=:snapshot '
                'ORDER BY p.session,p.captured_at DESC,p.id DESC LIMIT 6001) '
                'SELECT p.*,d.id AS decision_id,d.status AS decision_status,d.evidence_grade FROM newest p '
                'LEFT JOIN LATERAL (SELECT id,status,evidence_grade FROM selection_decision '
                "WHERE record_domain='price_observation' AND record_id=p.id AND recorded_at<=:snapshot "
                'ORDER BY recorded_at DESC,id DESC LIMIT 1) d ON true ORDER BY p.session')
            rows=session.execute(query,params).mappings().all()
            if len(rows)>6000:raise ValueError('source window exceeds bar bound')
            for row in rows:
                if not supplier_daily_visible(row['session'],identity.market,cutoff):continue
                if row['decision_status']!='accepted':
                    gaps.add('latest_source_revision_unavailable');continue
                if row['evidence_grade'] not in {'contract_verified','supplier_declared'}:
                    gaps.add('source_grade_unqualified');continue
                available=datetime.combine(row['session']+timedelta(days=1),time.min,
                    ZoneInfo('Asia/Seoul' if identity.market=='KR' else 'America/New_York')).astimezone(timezone.utc)
                try:
                    bar=Bar(row['id'],identity,row['session'],row['open'],row['high'],row['low'],row['close'],row['volume'],
                        row['captured_at'],available,canonical_hash({k:row[k] for k in
                        ('id','signature','raw_id','session','open','high','low','close','volume','notional','decision_id')}),
                        row['notional'],row['raw_id'],row['decision_id'])
                except ValueError:gaps.add('invalid_latest_price_structure');continue
                bars.append(bar)
            if len(bars)==before_count:raise ValueError('selected series has no eligible bars')
            if identity.basis=='provider_adjusted':gaps.add('historical_adjustment_reference_unverified')
        if len(markets)!=1:raise ValueError('single market dataset required')
        if not bars:raise ValueError('no eligible bars in selected bounded snapshot')
        if len(bars)>100000:raise ValueError('aggregate bar bound exceeded')
        grid_end=min(end,cutoff.astimezone(ZoneInfo('America/New_York' if markets=={'US'} else 'Asia/Seoul')).date()-timedelta(days=1))
        calendar=load_calendar_capture(session,self.raw_root,start=start,end=grid_end,as_of=snapshot) if markets=={'US'} else None
        sessions=([date.fromisoformat(r['date']) for r in calendar['rows']] if calendar else provisional_weekdays(start,grid_end))
        grade='provider_declared_alpaca_calendar' if calendar else 'provisional_weekdays'
        if not calendar:gaps.add('market_calendar_unverified')
        outside=[b for b in bars if b.session not in set(sessions)]
        if outside:gaps.add('source_quote_calendar_conflict')
        bars=[b for b in bars if b.session in set(sessions)]
        if {s['series_key'] for s in selections}-{b.series.key for b in bars}:
            raise ValueError('selected series has no eligible bars on the declared calendar')
        for identity in {b.series for b in bars}:
            if set(sessions)-{b.session for b in bars if b.series==identity}:gaps.add('missing_selected_series_sessions')
        return AnalysisDataset.create(bars=bars,sessions=sessions,cutoff=cutoff,manifest_id=target['manifest_id'],
            manifest_hash=target['manifest_hash'],original_target_count=target['target_count'],source_snapshot_at=snapshot,
            quality_gaps=gaps,origin=self.origin,calendar_grade=grade,calendar_evidence_refs=(
                'capture:'+calendar['capture_id'],'raw:'+calendar['raw_id'],'body:'+calendar['body_hash']) if calendar else ())
    def publications(self,security_id,cutoff):
        # This is a metadata/quality view, not a replacement financial ratio query.
        with self._read() as session:
            rows=session.execute(text('SELECT e.id,e.document_id,e.source_fact_id,e.source,e.source_version_ref,'
                'e.status,e.reason,e.public_day,e.effective_available_at,e.calendar_basis,e.policy_version '
                'FROM financial_daily_publication_evidence e JOIN security s ON s.issuer_id=e.issuer_id '
                'WHERE s.id=:id AND e.policy_version=:policy AND '
                '(e.effective_available_at<=:cutoff OR e.status=\'unavailable\') '
                'ORDER BY e.recorded_at DESC,e.id DESC LIMIT 50'),{'id':security_id,'cutoff':cutoff,
                'policy':'financial_daily_metadata_pit_v1'}).mappings().all()
        from .dataset_store import wire
        return [wire(dict(r)) for r in rows]
