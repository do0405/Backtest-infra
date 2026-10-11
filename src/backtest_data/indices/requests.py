"""Immutable public requests, separate from any live collector configuration."""
from dataclasses import dataclass
from datetime import date
import json
from urllib.parse import urlsplit

START = date(2016,1,1)
END = date(2026,10,7)
LSEG_CATALOG = 'https://www.lseg.com/content/lseg/en_us/ftse-russell/index-resources/notices/jcr:content/root/container/page_content_region/page-content-region/section/section-fw/index_announcements_.indexannouncement.json'
HOSTS = {'www.lseg.com','content.ftserussell.com','ir.nasdaq.com','press.spglobal.com','index.krx.co.kr'}
SOURCE_HOSTS={'lseg':{'www.lseg.com','content.ftserussell.com'},'nasdaq_index':{'ir.nasdaq.com'},
              'spglobal':{'press.spglobal.com'},'krx_index':{'index.krx.co.kr'}}

def original_scope():
    return {'start':START.isoformat(),'end':END.isoformat(),'target_count':17758,'market_counts':{'KR':4507,'US':13251}}

@dataclass(frozen=True)
class IndexRequest:
    source: str
    url: str
    parser: str
    method: str = 'GET'
    params: tuple[tuple[str,str], ...] = ()
    json_body: str | None = None
    context: tuple[tuple[str,str], ...] = ()

    def __post_init__(self):
        parts = urlsplit(self.url)
        if parts.scheme != 'https' or parts.hostname not in SOURCE_HOSTS.get(self.source,set()) or parts.port not in (None,443) or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('Allowlisted public official URL required')
        if self.method not in {'GET','POST'} or (self.method == 'POST' and self.url != LSEG_CATALOG):
            raise ValueError('Only published public catalog POST is permitted')
        if self.method == 'GET' and self.json_body is not None: raise ValueError('GET cannot contain POST body')
        if any(any(s in k.lower() for s in ('auth','secret','password','token','key')) for k,_ in self.params):
            raise ValueError('No credentials in official public requests')
        if not isinstance(self.params,tuple) or not isinstance(self.context,tuple): raise ValueError('Frozen request tuples required')
        if any(not isinstance(pair,tuple) or len(pair)!=2 or not all(isinstance(value,str) for value in pair) for pair in self.params+self.context):
            raise ValueError('Immutable public string pairs required')
        if self.method=='POST':
            try: payload=json.loads(self.json_body)
            except (TypeError,ValueError): raise ValueError('Published catalog payload required') from None
            if not isinstance(payload,dict) or set(payload)!={'content_type','family_mnemonic','page_number','page_size','qry','year'}:
                raise ValueError('Only published catalog search fields permitted')
            if payload['content_type']!='tech-notice, meth-review' or payload['family_mnemonic']!='' or payload['qry']!='':
                raise ValueError('Full-family historical catalog scope required')
            if any(type(payload[k]) is not int for k in ('page_number','page_size','year')) or payload['page_number']<0 or not 1<=payload['page_size']<=100 or not START.year<=payload['year']<=END.year:
                raise ValueError('Bounded historical catalog payload required')

    def payload(self): return json.loads(self.json_body) if self.json_body is not None else None
    def serializable(self):
        return {'source':self.source,'url':self.url,'parser':self.parser,'method':self.method,
                'params':[list(p) for p in self.params],'json_body':self.json_body,'context':[list(p) for p in self.context]}

    @classmethod
    def restore(cls,value):
        return cls(**{**value,'params':tuple(tuple(p) for p in value.get('params',[])),
                      'context':tuple(tuple(p) for p in value.get('context',[]))})

def lseg_catalog_request(year,page=0,page_size=50):
    if not START.year <= year <= END.year or page < 0 or not 1 <= page_size <= 100:
        raise ValueError('Original historical catalog window required')
    body={'content_type':'tech-notice, meth-review','family_mnemonic':'','page_number':page,'page_size':page_size,'qry':'','year':year}
    return IndexRequest('lseg',LSEG_CATALOG,'lseg_catalog','POST',json_body=json.dumps(body,sort_keys=True))

def nasdaq_catalog_request(year,page=0):
    if not START.year <= year <= END.year or page < 0: raise ValueError('Original historical year required')
    return IndexRequest('nasdaq_index','https://ir.nasdaq.com/search','nasdaq_catalog',params=(('f[0]','type:nir_news'),('f[1]',f'year:{year}'),('query','Nasdaq-100'),('page',str(page))),context=(('year',str(year)),('page',str(page))))

def public_download_requests():
    """Official discovered downloads; finite inventory, not full historical coverage."""
    result=[]
    for year,day in [(2025,'20250627'),(2026,'20260626')]:
        for action,filename in [('add','additions'),('delete','deletions')]:
            evidence='https://www.lseg.com/en/media-centre/press-releases/ftse-russell/2025/ftse-russell-begins-37th-annual-russell-reconstitution' if year==2025 else 'https://www.lseg.com/en/ftse-russell/russell-reconstitution'
            result.append(IndexRequest('lseg',f'https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/ru3000-{filename}-{day}.pdf','russell_pdf',context=(('index_key','FTSE_RUSSELL:RUSSELL_3000'),('action',action),('effective_date',f'{day[:4]}-{day[4:6]}-{day[6:]}'),('effective_phase','after_close'),('date_evidence_url',evidence),('published_date',''))))
    for action,filename in [('add','additions'),('delete','deletions')]:
        result.append(IndexRequest('lseg',f'https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/rmicro-{filename}-20260626.pdf','russell_pdf',context=(('index_key','FTSE_RUSSELL:RUSSELL_MICROCAP'),('action',action),('effective_date','2026-06-26'),('effective_phase','after_close'),('date_evidence_url','https://www.lseg.com/en/ftse-russell/russell-reconstitution'),('published_date',''))))
    for suffix in ('r1000','r2000','r3000','rmicro'):
        result.append(IndexRequest('lseg',f'https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/final-ipo-additions-3-qtr-{suffix}.pdf','russell_ipo_pdf'))
    return result

def default_requests():
    # Downloads are prioritized; all historical catalog years remain queued.
    result=public_download_requests()
    result += [lseg_catalog_request(year) for year in range(START.year,END.year+1)]
    result += [nasdaq_catalog_request(year) for year in range(START.year,END.year+1)]
    for year,slug in [(2016,'5'),(2017,'6'),(2018,'7'),(2019,'8'),(2020,'9')]:
        url='https://ir.nasdaq.com/node/95636' if year==2017 else f'https://ir.nasdaq.com/news-releases/news-release-details/annual-changes-nasdaq-100-index-{slug}'
        result.append(IndexRequest('nasdaq_index',url,'nasdaq_html'))
    result.append(IndexRequest('nasdaq_index','https://ir.nasdaq.com/node/104321','nasdaq_html'))
    return result
