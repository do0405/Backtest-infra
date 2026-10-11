"""Official source metadata: no current ticker or filing-time substitution."""
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
import re

def parse_edgar_delistings(content,*,start,end):
    lines=content.decode('utf-8-sig').splitlines()
    header=next((i for i,line in enumerate(lines) if line=='CIK|Company Name|Form Type|Date Filed|Filename'),None)
    if header is None:raise ValueError('EDGAR master schema changed')
    result=[]
    for line in lines[header+1:]:
        if not line or line.startswith('-'):continue
        parts=line.split('|')
        if len(parts)!=5:raise ValueError('EDGAR master record changed')
        cik,name,form,day,path=parts
        if form not in {'25','25/A','25-NSE','25-NSE/A'}:continue
        filed=date.fromisoformat(day)
        if not start<=filed<=end:continue
        result.append({'record_kind':'listing_event','entity_key':f'cik:{int(cik):010d}',
            'row_key':path,'entity_name':name,'filing_date':filed,'date_basis':'filing_day_not_last_trading_day',
            'fields':{'cik':cik,'form':form,'filing_date':day,'accession_path':path,
            'delisting_effective_date_verified':False,'source':'SEC quarterly master index'},
            'issues':['security_class_and_last_trade_unverified','public_dissemination_time_unverified']})
    return result

def parse_lseg(payload):
    if 'ErrorCode' in payload or not isinstance(payload.get('Data'),list) or 'TotalCount' not in payload:
        raise ValueError('LSEG source envelope is not a valid catalog')
    rows=[]
    for raw in payload['Data']:
        rows.append({'record_kind':'index_announcement','entity_key':str(raw.get('FamilyMnemonic') or 'unresolved_family'),
            'row_key':str(raw['id']),'entity_name':raw.get('title'),
            'announced_date':date.fromisoformat(raw['AnnouncedDate'][:10]) if raw.get('AnnouncedDate') else None,
            'effective_date':date.fromisoformat(raw['ElectiveDate'][:10]) if raw.get('ElectiveDate') else None,
            'published_date':date.fromisoformat(raw['AnnouncedDate'][:10]) if raw.get('AnnouncedDate') else None,
            'date_basis':'source_declared_announcement_and_effective_day','fields':raw,
            'issues':['full_notice_requires_portal','constituent_weights_unavailable','effective_intraday_phase_unknown']})
    return rows

def parse_fund_tickers(payload):
    required={'cik','seriesId','classId','symbol'}
    names=payload.get('fields',[])
    if not required.issubset(names) or not isinstance(payload.get('data'),list):raise ValueError('SEC fund ticker schema changed')
    rows=[]
    for values in payload['data']:
        fields=dict(zip(names,values,strict=True))
        rows.append({'record_kind':'fund_mapping','entity_key':str(fields['seriesId']),
            'row_key':f"{fields['classId']}:{fields['symbol']}",'member_identifier':str(fields['symbol']),
            'fields':fields,'date_basis':'current_snapshot_capture_only',
            'issues':['historical_ticker_mapping_unverified','ETF_mutual_fund_classification_unknown']})
    return rows

class Tables(HTMLParser):
    def __init__(self):super().__init__();self.tables=[];self.table=None;self.row=None;self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=='table':self.table=[]
        elif tag=='tr' and self.table is not None:self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(' '.join(' '.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:
            self.table.append(self.row);self.row=None
        elif tag=='table' and self.table is not None:
            self.tables.append(self.table);self.table=None

def parse_spglobal_changes(content,*,announced_date):
    parser=Tables();parser.feed(content.decode('utf-8',errors='strict'))
    result=[]
    for n,table in enumerate(parser.tables):
        heading=' '.join(x for row in table[:2] for x in row)
        matched=re.search(r'S&P\s+(500|MidCap\s+400|SmallCap\s+600)\s+INDEX\s*[^A-Za-z0-9]+\s*(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',heading,re.I)
        if not matched:continue
        from datetime import datetime
        effective=datetime.strptime(' '.join(matched.groups()[1:]),'%B %d %Y').date()
        index='S&P '+matched.group(1)
        action=None
        for j,cells in enumerate(table):
            if not cells:continue
            if cells[0] in ('ADDED','DELETED'):action=cells[0];cells=cells[1:]
            if action and cells and cells[0] not in ('COMPANY','ADDED','DELETED'):
                result.append({'record_kind':'index_member','entity_key':index,'row_key':f'{n}:{j}',
                    'member_name':cells[0],'announced_date':announced_date,'published_date':announced_date,
                    'effective_date':effective,'date_basis':'official_release_day_effective_after_close',
                    'fields':{'action':action,'cells':cells,'effective_phase':'after_close'},
                    'issues':['source_member_identifier_unresolved','weight_unavailable']})
    if not result:raise ValueError('No recognized official index change tables')
    return result
