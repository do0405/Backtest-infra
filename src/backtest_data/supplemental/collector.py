"""One resumable supplemental worker; existing collection queues are untouched."""
import argparse
from datetime import date,datetime,time,timedelta,timezone
from decimal import Decimal
import itertools,json,math
from pathlib import Path
import re,shutil
import hashlib
from zoneinfo import ZoneInfo
from sqlalchemy import select
from ..config import Settings
from ..credentials import load_credentials
from ..db import make_engine,transaction,utcnow
from ..catalog.models import SourceQualification
from ..catalog.universe import parse_us_directory
from ..ingestion.models import TargetUniverseManifest,RawObject
from ..ingestion.raw_store import RawStore
from .models import SupplementalDocument
from .models import SupplementalObservation
from .storage import register_document,store_rows,archive_file,exact_json,public_request
from .transport import SourceHTTP,LSEG_CATALOG
from . import issuer_products as products
from .official_catalogs import parse_edgar_delistings,parse_lseg,parse_fund_tickers,parse_spglobal_changes
from .alpaca_inputs import corporate_action_requests,normalize_corporate_action_page,decode_alpaca_json,trade_request,CapturedTradePage,aggregate_trade_pages

MANIFEST='b31ee12c-40aa-484a-9807-ed8fafc1ec0a'
SP_RELEASE='https://press.spglobal.com/2016-01-22-Federal-Realty-Investment-Trust-Set-to-Join-the-S-P-500-Curtiss-Wright-Pool-to-Join-S-P-MidCap-400-Cray-Asbury-Automotive-to-Join-S-P-SmallCap-600'
NPORT_URL='https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets'

class SupplementalCollector:
    def __init__(self,engine,settings,progress_path,manifest_id=MANIFEST,*,requested_window=None,min_free_bytes=50*1024**3):
        self.engine,self.settings,self.path=engine,settings,Path(progress_path)
        with transaction(engine) as session:
            manifest=session.get(TargetUniverseManifest,manifest_id)
            if manifest is None:raise ValueError('Original target manifest unavailable')
            self.scope=dict(manifest.scope);self.target_count=len(manifest.members)
        self.original_start=date.fromisoformat(self.scope['output_start']);self.original_end=date.fromisoformat(self.scope['output_end'])
        self.start,self.end=self.original_start,self.original_end
        if type(min_free_bytes) is not int or min_free_bytes<2*1024**3:raise ValueError('Explicit supplemental disk floor must be at least 2 GiB')
        self.min_free_bytes=min_free_bytes;self.requested_window=requested_window
        if requested_window is not None:
            if (not isinstance(requested_window,dict) or set(requested_window)!={'start','end'}
                    or any(type(value) is not date for value in requested_window.values())
                    or requested_window['start']>requested_window['end'] or requested_window['start']<self.original_start):
                raise ValueError('Explicit ordered supplemental refresh window required')
            captured=utcnow()
            completed=min(captured.astimezone(ZoneInfo(zone)).date()-timedelta(days=1)
                for zone in ('Asia/Seoul','America/New_York'))
            if requested_window['end']>completed:raise ValueError('Supplemental refresh requires completed NY/KR calendar days')
            self.start,self.end=requested_window['start'],requested_window['end']
        self.nport_quarters=[]
        for year in range(2019,self.end.year+1):
            for quarter in range(1,5):
                if (year,quarter)<(2019,4):continue
                next_quarter=date(year+1,1,1) if quarter==4 else date(year,quarter*3+1,1)
                if next_quarter-timedelta(days=1)<=self.end:self.nport_quarters.append((year,quarter))
        if requested_window is not None:self.nport_quarters=list(reversed(self.nport_quarters[-2:]))
        self.manifest_id=manifest_id;self.http=SourceHTTP(engine)
        self.nport_iterators={}
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'version':1,'completed':{},'catalogs':{},'errors':{},'inserted':0,'nport':{},'actions':{},'lseg':{},'coverage_complete':False}
        if self.state.get('manifest_id',manifest_id)!=manifest_id:raise ValueError('Supplemental progress manifest mismatch')
        if (self.state.get('output_start',self.start.isoformat()),self.state.get('output_end',self.end.isoformat()))!=(self.start.isoformat(),self.end.isoformat()):
            raise ValueError('Supplemental progress requested window mismatch')
        if (self.state.get('original_output_start',self.original_start.isoformat()),self.state.get('original_output_end',self.original_end.isoformat()))!=(self.original_start.isoformat(),self.original_end.isoformat()):
            raise ValueError('Supplemental original manifest scope mismatch')
        if self.state.get('listing_parser_version',0)<2:
            for key,error in list(self.state['errors'].items()):
                if key.startswith('listing:') and error.get('reason')=='ValueError':
                    self.state.setdefault('error_history',[]).append({'step':key,**error})
                    self.state['errors'].pop(key)
                    self.state.pop('alpha_deferred_until',None)
            self.state['listing_parser_version']=2
        self.state.update(manifest_id=manifest_id,target_count=self.target_count,output_start=self.start.isoformat(),output_end=self.end.isoformat(),coverage_complete=False)
        if requested_window is not None:self.state.update(original_output_start=self.original_start.isoformat(),original_output_end=self.original_end.isoformat(),requested_window={'start':self.start.isoformat(),'end':self.end.isoformat()})
        c=load_credentials(sources=['sec','alpaca','alpha_vantage']);self.sec_headers={'User-Agent':c.sec_user_agent}
        self.alpha_key=c.alpha_vantage_api_key
        self.alpaca_headers={'APCA-API-KEY-ID':c.alpaca_key.reveal(),'APCA-API-SECRET-KEY':c.alpaca_secret.reveal()} if c.alpaca_key and c.alpaca_secret else None

    def save(self,**values):
        self.state.update(values,checked_at=utcnow().isoformat())
        self.path.parent.mkdir(parents=True,exist_ok=True)
        pending=self.path.with_suffix('.pending.json');pending.write_text(json.dumps(exact_json(self.state),ensure_ascii=True,indent=2),encoding='utf-8');pending.replace(self.path)

    def ingest(self,key,source,request,parser):
        if key in self.state['completed']:return None
        self.save(current_step=key,status='running')
        response=self.http.fetch(source,request.get('method','GET'),request['url'],params=request.get('params'),json_body=request.get('json_body'),headers=self.alpaca_headers if source=='alpaca' else self.sec_headers if source=='sec' else None)
        scope={'manifest_id':self.manifest_id,'target_count':self.target_count,'output_start':self.start.isoformat(),'output_end':self.end.isoformat(),'request':request.get('params',request.get('json_body',{}))}
        if self.requested_window is not None:scope.update(original_output_start=self.original_start.isoformat(),original_output_end=self.original_end.isoformat())
        try: rows=list(parser(response.content))
        except Exception:
            with transaction(self.engine) as session:
                document=register_document(session,source=source,domain=key.split(':')[0],url=request['url'],request_scope=scope,captured_at=response.captured_at,raw_root=self.settings.raw_root,content=response.content)
                document_id=document.id
            self.state.setdefault('parse_failure_documents',{})[key]=document_id
            self.save()
            raise
        with transaction(self.engine) as session:
            document=register_document(session,source=source,domain=key.split(':')[0],url=request['url'],request_scope=scope,captured_at=response.captured_at,raw_root=self.settings.raw_root,content=response.content)
            result=store_rows(session,document,rows)
            session.add(SourceQualification(source=source,domain=key.split(':')[0],status='access_verified',
                evidence={'supplemental_document_id':document.id,'source_rows':result['source_rows'],'inserted':result['inserted'],'coverage_complete':False,'target_count':self.target_count}))
        self.state['completed'][key]=result;self.state['inserted']+=result['inserted'];self.state['errors'].pop(key,None)
        self.save(last_commit_at=utcnow().isoformat())
        return response,rows,result['document_id']

    def attempt(self,key,action):
        if key in self.state['completed']:return None
        previous=self.state['errors'].get(key,{})
        retry_at=previous.get('next_retry_at')
        if retry_at and datetime.fromisoformat(retry_at)>utcnow(): return None
        try:return action()
        except Exception as exc:
            from ..ingestion.http import ProviderError
            from sqlalchemy.exc import SQLAlchemyError
            if isinstance(exc,SQLAlchemyError):raise
            reason=exc.code if isinstance(exc,ProviderError) else type(exc).__name__
            wait=timedelta(days=1) if reason=='free_rolling_daily_quota_deferred' else timedelta(hours=1)
            self.state['errors'][key]={'reason':str(reason)[:160],'checked_at':utcnow().isoformat(),
                'next_retry_at':(utcnow()+wait).isoformat(),'coverage_complete':False}
            self.save();return None

    def bootstrap(self):
        self.save(phase='whole_catalog_and_first_historical_commits')
        today=utcnow().date()
        if not self.can_continue():return
        self.listing_step(initial=True)
        # Recover the already committed first catalog without recapture or loss.
        if 'catalog:kodex:1' in self.state['completed'] and not self.state.get('kodex_requests'):
            with transaction(self.engine) as session:
                doc=session.get(SupplementalDocument,self.state['completed']['catalog:kodex:1']['document_id'])
                raw=session.get(RawObject,doc.raw_id)
                self.state['kodex_requests']=products.kodex_catalog_requests(RawStore(self.settings.raw_root).read(raw.storage_key))
        self.restore_catalogs()
        first=self.attempt('catalog:kodex:1',lambda:self.ingest('catalog:kodex:1','kodex',products.kodex_catalog_requests()[0],products.parse_kodex_catalog))
        if first:
            self.state['kodex_requests']=products.kodex_catalog_requests(first[0].content)
            self.state['catalogs']['kodex']=first[1];self.save()
        for n,request in enumerate(self.state.get('kodex_requests',[])[1:],2):
            if not self.can_continue():return
            got=self.attempt(f'catalog:kodex:{n}',lambda n=n,request=request:self.ingest(f'catalog:kodex:{n}','kodex',request,products.parse_kodex_catalog))
            if got:self.state['catalogs'].setdefault('kodex',[]).extend(got[1]);self.save()
        for source,request,parser in (
            ('tiger',products.tiger_catalog_request(),lambda b:products.parse_tiger_catalog(b,observation_date=today)),
            ('ishares',products.ishares_catalog_request(),lambda b:products.parse_ishares_catalog(b,observation_date=today)),
            ('shinhan_issuance',products.shinhan_etn_issuance_request(),lambda b:products.parse_shinhan_etn_issuance(b,observation_date=today)),
            ('shinhan',products.shinhan_etn_catalog_request(),lambda b:products.parse_shinhan_etn_catalog(b,observation_date=today))):
            if not self.can_continue():return
            key=f'catalog:{source}';got=self.attempt(key,lambda:self.ingest(key,'shinhan' if source.startswith('shinhan') else source,request,parser))
            if got:self.state['catalogs'][source]=got[1];self.save()
        for source,request,parser,key in (
            ('sec',{'url':'https://www.sec.gov/files/company_tickers_mf.json'},lambda b:parse_fund_tickers(json.loads(b)),'fund_mapping:sec'),
            ('kodex',products.kodex_holdings_request('2ETF01',date(2016,1,4)),lambda b:products.parse_kodex_holdings(b,'2ETF01',requested_date=date(2016,1,4)),'holding_proof:kodex:2016'),
            ('tiger',products.tiger_holdings_request('KR7102110004',date(2016,1,4)),lambda b:products.parse_tiger_holdings(b,'KR7102110004',requested_date=date(2016,1,4)),'holding_proof:tiger:2016'),
            ('ishares',products.ishares_workbook_request('239726'),lambda b:products.parse_ishares_workbook(b,'239726'),'workbook_proof:ishares'),
            ('sec',{'url':'https://www.sec.gov/Archives/edgar/full-index/2016/QTR1/master.idx'},lambda b:parse_edgar_delistings(b,start=self.start,end=self.end),'edgar:2016:1'),
            ('lseg',{'method':'POST','url':LSEG_CATALOG,'json_body':{'content_type':'tech-notice, meth-review','family_mnemonic':'','page_number':0,'page_size':50,'qry':'','year':2016}},lambda b:parse_lseg(json.loads(b,parse_float=Decimal)),'index:2016:0'),
            ('spglobal',{'url':SP_RELEASE},lambda b:parse_spglobal_changes(b,announced_date=date(2016,1,22)),'index_members:spglobal:2016')):
            if self.requested_window is not None and (':2016' in key or key.startswith(('edgar:2016:','index:2016:'))):continue
            if not self.can_continue():return
            self.attempt(key,lambda key=key,source=source,request=request,parser=parser:self.ingest(key,source,request,parser))
        if not self.can_continue():return
        self.action_step()
        if self.requested_window is None:self.attempt('trade_input_session',self.trade_step)
        if not self.can_continue():return
        self.attempt('nport_input',self.nport_step)
        if self.requested_window is None and self.can_continue():self.attempt('nport_first_numeric_inputs',self.nport_first_numeric_inputs)
        self.save(phase='full_backfill_running',bootstrap_pass_complete=True)

    def trade_step(self):
        """Validate a whole-session tape before any all-symbol trade backfill.

        This reference traversal is explicitly separate from the unchanged
        all-US target. No single-session or regular-sale subset is declared
        full-market VWAP, and missing action coverage is never manufactured.
        """
        if self.requested_window is not None:return
        if not self.alpaca_headers:return
        progress=self.state.setdefault('trade_proof',{'pages':[],'complete':False})
        self.state['trade_backfill_scope']={'target_US_securities':13251,'output_start':self.start.isoformat(),
            'output_end':self.end.isoformat(),'status':'requires_verified_trade_policy_calendar_identity_and_action_inputs','coverage_complete':False}
        if progress['complete']:return
        local=ZoneInfo('America/New_York');session_date=date(2016,1,4)
        opening=datetime.combine(session_date,time(9,30),local);closing=datetime.combine(session_date,time(16),local)
        cutoff=datetime.combine(self.end+timedelta(days=1),time.min,local)
        request=trade_request('AAPL',session_date,session_open=opening,session_close=closing,cutoff=cutoff,limit=10000)
        if progress.get('next_page_token'):request=request.with_page_token(progress['next_page_token'])
        response=self.http.fetch('alpaca','GET',request.url,params=dict(request.params),headers=self.alpaca_headers)
        pages=[]
        with transaction(self.engine) as session:
            for item in progress['pages']:
                doc=session.get(SupplementalDocument,item['document_id']);raw=session.get(RawObject,doc.raw_id)
                old_request=trade_request('AAPL',session_date,session_open=opening,session_close=closing,cutoff=cutoff,limit=10000)
                if item.get('page_token'):old_request=old_request.with_page_token(item['page_token'])
                pages.append(CapturedTradePage(old_request,decode_alpaca_json(RawStore(self.settings.raw_root).read(raw.storage_key)),doc.content_hash,doc.captured_at))
        payload=decode_alpaca_json(response.content)
        pages.append(CapturedTradePage(request,payload,hashlib.sha256(response.content).hexdigest(),response.captured_at))
        candidate=aggregate_trade_pages(pages,symbol='AAPL',session=session_date,session_open=opening,session_close=closing,cutoff=cutoff)
        with transaction(self.engine) as session:
            doc=register_document(session,source='alpaca',domain='trade_input_validation',url=request.url,
                request_scope={'manifest_id':self.manifest_id,'request':dict(request.params),'reference_validation_only':True},
                captured_at=response.captured_at,raw_root=self.settings.raw_root,content=response.content)
            result=store_rows(session,doc,[candidate.as_staged_row()])
        progress['pages'].append({'document_id':result['document_id'],'page_token':dict(request.params).get('page_token')})
        progress.update(next_page_token=payload.get('next_page_token'),complete=payload.get('next_page_token') is None,
            source_trade_pages=len(pages),sum_price_volume=str(candidate.sum_price_volume),eligible_volume=str(candidate.eligible_volume),
            exact_full_market_vwap_verified=False,issues=list(candidate.issues))
        self.state['inserted']+=result['inserted'];self.save(last_commit_at=utcnow().isoformat())

    def action_step(self):
        if not self.alpaca_headers:return
        for year_request in corporate_action_requests(self.start,self.end):
            year=dict(year_request.params)['start'][:4];progress=self.state['actions'].get(year,{})
            if progress.get('complete'):continue
            request=year_request.with_page_token(progress['next_page_token']) if progress.get('next_page_token') else year_request
            page=progress.get('pages',0);key=f'actions:{year}:{page}'
            if self.retry_pending(key):continue
            if key in self.state['completed']:
                payload=decode_alpaca_json(self.committed_body(key));token=payload.get('next_page_token')
                self.state['actions'][year]={'pages':page+1,'next_page_token':token,'complete':token is None,'economic_coverage_verified':False}
                self.save();return
            got=self.attempt(key,lambda:self.ingest(key,'alpaca',{'url':request.url,'method':request.method,'params':dict(request.params)},lambda b:normalize_corporate_action_page(decode_alpaca_json(b),cutoff=self.end)))
            if got:
                payload=decode_alpaca_json(got[0].content);token=payload.get('next_page_token')
                self.state['actions'][year]={'pages':page+1,'next_page_token':token,'complete':token is None,'economic_coverage_verified':False}
                self.save()
            return

    def nport_step(self):
        from .nport import iter_nport_rows
        for year in (range(self.end.year,2018,-1) if self.requested_window is not None else range(2019,self.end.year+1)):
            for quarter in (range(4,0,-1) if self.requested_window is not None else range(1,5)):
                if (year,quarter) not in self.nport_quarters:continue
                name=f'{year}q{quarter}_nport';progress=self.state['nport'].get(name,{})
                if progress.get('complete'):continue
                if not self.can_continue():return
                self.save(current_step=f'nport:{name}')
                path=self.path.parent/'downloads'/f'{name}.zip'
                url=f'https://www.sec.gov/files/dera/data/form-n-port-data-sets/{name}.zip'
                if name not in self.nport_iterators:
                    capture=self.http.download('sec',url,path,headers=self.sec_headers)
                    if not progress.get('document_id'):
                        metadata=archive_file(path,self.settings.raw_root)
                        with transaction(self.engine) as session:
                            doc=register_document(session,source='sec_nport',domain='fund_portfolios',url=url,request_scope={'manifest_id':self.manifest_id,'output_start':self.start.isoformat(),'output_end':self.end.isoformat(),'quarter':name},captured_at=datetime.fromisoformat(capture['captured_at']),raw_root=self.settings.raw_root,raw_metadata=metadata)
                            document_id=doc.id
                        progress['document_id']=document_id;self.state['nport'][name]=progress;self.save()
                    self.nport_iterators[name]=iter_nport_rows(path,tables=('SUBMISSION','REGISTRANT','FUND_REPORTED_INFO','FUND_REPORTED_HOLDING'),resume_section=progress.get('section'),resume_offset=progress.get('offset',0))
                iterator=self.nport_iterators[name]
                try:
                    rows=list(itertools.islice(iterator,1000))
                    with transaction(self.engine) as session:
                        doc=session.get(SupplementalDocument,progress['document_id']);result=store_rows(session,doc,rows)
                except Exception:
                    # Reset to the last committed cursor after any failed batch.
                    self.nport_iterators.pop(name,None).close()
                    raise
                self.state['inserted']+=result['inserted'];progress['inserted']=progress.get('inserted',0)+result['inserted']
                if rows:progress.update(section=rows[-1]['source_section'],offset=rows[-1]['source_offset']+1)
                else:
                    progress['complete']=True
                    self.nport_iterators.pop(name,None)
                self.save(last_commit_at=utcnow().isoformat());return

    def edgar_step(self):
        for year in range(self.start.year,self.end.year+1):
            for quarter in range(1,5):
                if date(year,(quarter-1)*3+1,1)>self.end:continue
                key=f'edgar:{year}:{quarter}'
                if key in self.state['completed']:continue
                if self.retry_pending(key):continue
                self.attempt(key,lambda:self.ingest(key,'sec',{'url':f'https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx'},lambda b:parse_edgar_delistings(b,start=self.start,end=self.end)));return

    def index_step(self):
        for year in range(self.start.year,self.end.year+1):
            p=self.state['lseg'].get(str(year),{'next_page':0});page=p['next_page']
            if p.get('complete'):continue
            key=f'index:{year}:{page}'
            if self.retry_pending(key):continue
            if key in self.state['completed']:
                # Bootstrap first page, recover continuation from its declared count.
                with transaction(self.engine) as session:
                    doc=session.get(SupplementalDocument,self.state['completed'][key]['document_id']);raw=session.get(RawObject,doc.raw_id)
                    payload=json.loads(RawStore(self.settings.raw_root).read(raw.storage_key),parse_float=Decimal)
                total=int(payload['TotalCount']);self.state['lseg'][str(year)]={'next_page':page+1,'total':total,'complete':(page+1)*50>=total};self.save();return
            request={'method':'POST','url':LSEG_CATALOG,'json_body':{'content_type':'tech-notice, meth-review','family_mnemonic':'','page_number':page,'page_size':50,'qry':'','year':year}}
            got=self.attempt(key,lambda:self.ingest(key,'lseg',request,lambda b:[r for r in parse_lseg(json.loads(b,parse_float=Decimal)) if not r['announced_date'] or self.start<=r['announced_date']<=self.end]))
            if got:
                total=int(json.loads(got[0].content,parse_float=Decimal)['TotalCount'])
                self.state['lseg'][str(year)]={'next_page':page+1,'total':total,'complete':(page+1)*50>=total};self.save()
            elif key in self.state['errors']:p['blocked']=self.state['errors'][key];self.state['lseg'][str(year)]=p;self.save()
            return

    def distribution_step(self):
        for source in ('kodex','tiger','ishares'):
            for row in self.state['catalogs'].get(source,[]):
                entity=row['entity_key'];key=f'distribution:{source}:{entity}'
                if key in self.state['completed'] or self.retry_pending(key):continue
                if source=='ishares':request=products.ishares_workbook_request(entity);parser=lambda b:products.parse_ishares_workbook(b,entity)
                else:request=getattr(products,source+'_distributions_request')(entity);parser=lambda b:getattr(products,'parse_'+source+'_distributions')(b,entity)
                self.attempt(key,lambda:self.ingest(key,source,request,parser));return

    def holdings_step(self):
        """Traverse dated official PCF files; live catalogs do not prove old universe."""
        progress=self.state.setdefault('holdings',{'date_offset':0,'entity_offset':0,'complete':False})
        catalog=progress.setdefault('entities',sorted(set((source,row['entity_key']) for source in ('kodex','tiger') for row in self.state['catalogs'].get(source,[]))))
        if not catalog or progress['complete']:return
        total_dates=(self.end-self.start).days+1
        self.state['holdings_scope']={'calendar_dates':total_dates,'current_catalog_products':len(catalog),
            'requested_start':self.start.isoformat(),'requested_end':self.end.isoformat(),
            'closed_products_and_all_issuers_covered':False,'coverage_complete':False}
        day=self.start+timedelta(days=progress['date_offset'])
        source,entity=catalog[progress['entity_offset']]
        key=f'holding:{source}:{entity}:{day.isoformat()}'
        request=getattr(products,source+'_holdings_request')(entity,day)
        got=self.attempt(key,lambda:self.ingest(key,source,request,
            lambda b:getattr(products,'parse_'+source+'_holdings')(b,entity,requested_date=day)))
        if got or key in self.state['completed'] or key in self.state['errors']:
            # Failed days remain explicit holes; this cursor is request traversal only.
            progress['entity_offset']+=1
            if progress['entity_offset']>=len(catalog):
                progress['entity_offset']=0;progress['date_offset']+=1
            progress['complete']=progress['date_offset']>=total_dates
            self.save()

    def nport_first_numeric_inputs(self):
        """Front-load typed values without altering the full archive traversal."""
        from .nport import iter_nport_rows
        name='2019q4_nport';progress=self.state['nport'].get(name,{})
        if not progress.get('document_id') or progress.get('first_numeric_inputs_committed'):return
        path=self.path.parent/'downloads'/f'{name}.zip'
        for table in ('FUND_REPORTED_INFO','FUND_REPORTED_HOLDING'):
            if progress.get('numeric_'+table):continue
            if not self.can_continue():return
            rows=list(itertools.islice(iter_nport_rows(path,tables=(table,)),1000))
            with transaction(self.engine) as session:
                doc=session.get(SupplementalDocument,progress['document_id']);result=store_rows(session,doc,rows)
            progress['numeric_'+table]=result
            self.state['inserted']+=result['inserted'];self.save(last_commit_at=utcnow().isoformat())
        progress['first_numeric_inputs_committed']=True;self.save()

    def run(self,budget_seconds):
        deadline=__import__('time').monotonic()+budget_seconds
        self.deadline=deadline
        if self.state.get('error'):
            self.state.setdefault('error_history',[]).append(self.state.pop('error'))
        self.save(status='running')
        self.bootstrap()
        while self.can_continue() and self.state.get('bootstrap_pass_complete'):
            self.listing_step();self.action_step();self.edgar_step();self.index_step();self.distribution_step();self.holdings_step()
            self.attempt('nport_input',self.nport_step)
            if not self.state.get('trade_proof',{}).get('complete'):self.attempt('trade_input_session',self.trade_step)
        self.save(status=self.state.get('status') if self.state.get('status')=='blocked' else 'budget_deferred',phase='backfill_checkpoint_preserved')
        return {'status':self.state['status'],'inserted':self.state['inserted'],'completed_steps':len(self.state['completed']),
            'source_gap_count':len(self.state['errors']),'source_gaps':dict(list(self.state['errors'].items())[:50]),
            'coverage_complete':False,'target_count':self.target_count,'progress_path':str(self.path)}

    def can_continue(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(self.path.parent).free<self.min_free_bytes:
            self.save(status='blocked',reason='E_free_space_guard');return False
        return __import__('time').monotonic()<getattr(self,'deadline',float('inf'))

    def retry_pending(self,key):
        retry_at=self.state['errors'].get(key,{}).get('next_retry_at')
        return bool(retry_at and datetime.fromisoformat(retry_at)>utcnow())

    def restore_catalogs(self):
        """Rebuild reference traversal from committed rows after checkpoint loss."""
        catalogs={}
        with transaction(self.engine) as session:
            for key,result in self.state['completed'].items():
                if not key.startswith('catalog:'): continue
                source=key.split(':')[1]
                rows=session.execute(select(SupplementalObservation.entity_key,SupplementalObservation.entity_name).where(
                    SupplementalObservation.document_id==result['document_id'])).all()
                catalogs.setdefault(source,[]).extend({'entity_key':r[0],'entity_name':r[1]} for r in rows)
        self.state['catalogs']=catalogs
        self.save()

    def committed_body(self,key):
        with transaction(self.engine) as session:
            doc=session.get(SupplementalDocument,self.state['completed'][key]['document_id'])
            raw=session.get(RawObject,doc.raw_id)
            return RawStore(self.settings.raw_root).read(raw.storage_key)

    def listing_step(self,initial=False):
        from .alpha_vantage import listing_status_request,parse_listing_status
        if not self.alpha_key: return
        priority=[self.end,self.start] if self.requested_window is not None else [self.end,date(2016,1,4),self.start]
        dates=priority+[self.start+timedelta(days=n) for n in range((self.end-self.start).days+1)]
        dates=list(dict.fromkeys(dates))
        self.state['listing_scope']={'all_requested_calendar_dates':len(dates),'states':['active','delisted'],
            'request_target':len(dates)*2,'free_daily_limit':25,'coverage_complete':False,
            'ETN_and_ticker_reuse_verified':False}
        limit=4 if initial else 1
        for day in dates:
            for listing_state in ('active','delisted'):
                key=f'listing:{day.isoformat()}:{listing_state}'
                if key in self.state['completed']:continue
                if self.state.get('alpha_deferred_until') and datetime.fromisoformat(self.state['alpha_deferred_until'])>utcnow():return
                spec=listing_status_request(day,listing_state)
                request={'url':spec.url,'method':spec.method,'params':dict(spec.params)}
                request['params']['apikey']=self.alpha_key.reveal()
                got=self.attempt(key,lambda:self.ingest(key,'alpha_vantage',request,
                    lambda b:parse_listing_status(b,snapshot_date=day,state=listing_state)))
                if key in self.state['errors']:
                    self.state['alpha_deferred_until']=self.state['errors'][key]['next_retry_at'];self.save();return
                if got:
                    limit-=1
                    if not limit:return

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--budget-seconds',type=float,default=300)
    parser.add_argument('--progress-path',type=Path,default=Path('artifacts/supplemental/progress.json'))
    args=parser.parse_args()
    import msvcrt
    args.progress_path.parent.mkdir(parents=True,exist_ok=True)
    lock=(args.progress_path.parent/'supplemental.collector.lock').open('a+b')
    if lock.seek(0,2)==0:lock.write(b'0');lock.flush()
    lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        lock.close();print(json.dumps({'status':'duplicate_prevented','coverage_complete':False}));return 4
    collector=None;engine=None
    try:
        settings=Settings.from_env();engine=make_engine(settings.database_url)
        collector=SupplementalCollector(engine,settings,args.progress_path)
        print(json.dumps(collector.run(args.budget_seconds),default=str))
    except Exception as exc:
        from sqlalchemy.exc import SQLAlchemyError
        from ..ops.db_recovery import database_error_payload
        result={'status':'blocked','reason':type(exc).__name__,'details_redacted':True,'coverage_complete':False}
        if isinstance(exc,SQLAlchemyError):result['database_error']=database_error_payload(exc)
        if collector is not None:collector.save(status='blocked',error=result)
        print(json.dumps(result));return 2
    finally:
        if collector is not None:
            collector.http.close()
            for iterator in collector.nport_iterators.values():iterator.close()
        if engine is not None:engine.dispose()
        lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close()
    return 0

if __name__=='__main__':raise SystemExit(main())
