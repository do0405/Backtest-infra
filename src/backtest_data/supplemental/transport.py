"""Bounded official data-only requests sharing existing provider call budgets."""
from datetime import datetime,timezone
import json,re,time
import hashlib
from pathlib import Path
from urllib.parse import urlsplit,unquote
import httpx
from ..db import transaction,utcnow
from ..ingestion.jobs import reserve_rate_slot, RateReservationDeferred
from ..ingestion.http import ProviderError,ProviderResponse

LSEG_CATALOG='https://www.lseg.com/content/lseg/en_us/ftse-russell/index-resources/notices/jcr:content/root/container/page_content_region/page-content-region/section/section-fw/index_announcements_.indexannouncement.json'
HOSTS={'sec':{'www.sec.gov','data.sec.gov'},'directory':{'www.nasdaqtrader.com'},
       'kodex':{'www.samsungfund.com'},'tiger':{'investments.miraeasset.com'},
       'ishares':{'www.ishares.com','www.blackrock.com'},'shinhan':{'open.shinhansec.com'},
       'lseg':{'www.lseg.com'},'spglobal':{'press.spglobal.com'},
       'nasdaq_index':{'ir.nasdaq.com'},'alpaca':{'data.alpaca.markets'},'alpha_vantage':{'www.alphavantage.co'}}
INTERVALS={'sec':0.25,'alpaca':0.334,'directory':0.2,'alpha_vantage':2.0}

def validate_request(source,method,url):
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname not in HOSTS.get(source,set()) or p.port not in (None,443) or p.username or p.password or p.query or p.fragment:
        raise ValueError('Official source URL outside explicit allowlist')
    if method!='GET' and not (source=='lseg' and method=='POST' and url==LSEG_CATALOG):
        raise ValueError('Only published data GET and LSEG read-only search POST allowed')
    if source=='alpaca' and not (p.path=='/v1/corporate-actions' or re.fullmatch(r'/v2/stocks/[A-Za-z0-9][A-Za-z0-9.\-^$]{0,119}/(?:trades|bars)',unquote(p.path))):
        raise ValueError('Only corporate action and historical market data allowed')
    if source=='alpha_vantage' and p.path!='/query':raise ValueError('Only listing query allowed')

class SourceHTTP:
    def __init__(self,engine):
        self.engine=engine;self.client=httpx.Client(timeout=httpx.Timeout(30,read=60),follow_redirects=False)

    def _reserve(self,source):
        with transaction(self.engine) as session:
            if source == 'alpha_vantage':
                from .quota import reserve_alpha_call
                reserve_alpha_call(session, utcnow())
            slot=reserve_rate_slot(session,f'{source}:shared_account',utcnow(),INTERVALS.get(source,1.0),max_delay_seconds=30)
        delay=(slot-utcnow()).total_seconds()
        if delay>0:time.sleep(delay)

    def fetch(self,source,method,url,*,params=None,json_body=None,headers=None,max_bytes=32*1024*1024):
        validate_request(source,method,url)
        if source=='alpha_vantage' and (params or {}).get('function')!='LISTING_STATUS':raise ValueError('Listing-only Alpha Vantage contract')
        try:
            self._reserve(source)
        except RateReservationDeferred:
            raise ProviderError(source, 'rate_budget_deferred', retryable=True) from None
        try:
            with self.client.stream(method,url,params=params,json=json_body,headers=headers) as response:
                if response.status_code!=200:raise ProviderError(source,f'HTTP_{response.status_code}')
                data=bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data)>max_bytes:raise ProviderError(source,'response_limit')
                return ProviderResponse(bytes(data),response.status_code,utcnow(),response.headers.get('content-type',''))
        except httpx.HTTPError:raise ProviderError(source,'transport_error') from None

    def download(self,source,url,path,*,headers=None,max_bytes=1024*1024*1024):
        validate_request(source,'GET',url)
        path=Path(path);receipt=path.with_suffix('.capture.json')
        if path.exists() and receipt.exists():
            cached=json.loads(receipt.read_text(encoding='utf-8'))
            if cached.get('url')!=url or cached.get('bytes')!=path.stat().st_size:raise ValueError('Cached download source mismatch')
            digest=hashlib.sha256()
            with path.open('rb') as data:
                for chunk in iter(lambda:data.read(1024*1024),b''):digest.update(chunk)
            if cached.get('sha256') and cached['sha256']!=digest.hexdigest():raise ValueError('Cached download integrity failure')
            if not cached.get('sha256'):
                cached.update(sha256=digest.hexdigest(),legacy_checksum_recorded=True)
                receipt.write_text(json.dumps(cached),encoding='utf-8')
            return cached
        try:
            self._reserve(source)
        except RateReservationDeferred:
            raise ProviderError(source, 'rate_budget_deferred', retryable=True) from None
        path.parent.mkdir(parents=True,exist_ok=True)
        pending=path.with_suffix('.partial')
        try:
            with self.client.stream('GET',url,headers=headers) as response:
                if response.status_code!=200:raise ProviderError(source,f'HTTP_{response.status_code}')
                length=response.headers.get('content-length')
                if length and int(length)>max_bytes:raise ProviderError(source,'response_limit')
                count=0;digest=hashlib.sha256()
                with pending.open('wb') as output:
                    for chunk in response.iter_bytes(1024*1024):
                        count+=len(chunk)
                        if count>max_bytes:raise ProviderError(source,'response_limit')
                        output.write(chunk)
                        digest.update(chunk)
            pending.replace(path)
            result={'url':url,'captured_at':utcnow().isoformat(),'bytes':count,'sha256':digest.hexdigest(),'path':str(path),'status_code':200}
            receipt.write_text(json.dumps(result),encoding='utf-8')
            return result
        except httpx.HTTPError:raise ProviderError(source,'transport_error') from None

    def close(self):self.client.close()
