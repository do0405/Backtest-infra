"""Exact official-source parsers. Unknown layouts fail rather than drop rows."""
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import io
import re

INDUSTRIES = ('Basic Materials', 'Consumer Discretionary', 'Consumer Staples', 'Energy',
              'Financials', 'Health Care', 'Industrials', 'Real Estate', 'Technology',
              'Telecommunications', 'Utilities')
SYMBOL = re.compile(r'[A-Z0-9][A-Z0-9.\-/]{0,19}')

def _base(index_key, kind, row_key):
    return dict(index_key=index_key, record_kind=kind, row_key=row_key, member_identifier=None,
                member_name=None, action=None, announced_date=None, published_date=None,
                effective_date=None, effective_phase=None, snapshot_date=None, weight_date=None,
                weight=None, weight_unit=None, fields={}, issues=[])

def _change(index_key, action, symbol, name, *, effective_date, published_date, effective_phase):
    row = _base(index_key, 'change', f'{action}:{symbol}')
    row.update(action=action, member_identifier=symbol, member_name=name,
               effective_date=effective_date, published_date=published_date,
               effective_phase=effective_phase,
               issues=['baseline_unavailable', 'weight_unavailable', 'historical_security_identity_unresolved'])
    if published_date is None: row['issues'].append('publication_unavailable')
    return row

def parse_russell_pages(pages, *, index_key, action, effective_date, published_date, effective_phase):
    if index_key not in {'FTSE_RUSSELL:RUSSELL_3000', 'FTSE_RUSSELL:RUSSELL_MICROCAP'} or action not in {'add', 'delete'}:
        raise ValueError('Declared Russell change list required')
    if type(effective_date) is not date or effective_phase not in {'after_close', 'before_open'}:
        raise ValueError('Source effective day and phase required')
    rows, seen = [], set()
    for page_number, page in enumerate(pages, 1):
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        if 'Company Symbol Industry' in lines:
            offset, vertical = lines.index('Company Symbol Industry') + 1, False
        elif any(lines[i:i+3] == ['Company', 'Symbol', 'Industry'] for i in range(len(lines)-2)):
            offset = next(i+3 for i in range(len(lines)-2) if lines[i:i+3] == ['Company', 'Symbol', 'Industry'])
            vertical = True
        else:
            # Official copyright/disclaimer final pages contain no table.
            if rows and any(x in page for x in ('All rights reserved', 'No part of this information', 'For more information about our indexes')):
                continue
            raise ValueError('Russell table header unavailable')
        body = lines[offset:]
        for i, line in enumerate(body):
            if line.startswith(('©', 'For more information', 'Russell US Indexes', 'lseg.com/', 'CORPORATE', 'FTSE Russell', '| Final index')):
                body = body[:i]
                break
        triples = []
        if vertical:
            if len(body) % 3: raise ValueError('Incomplete Russell table row')
            triples = [body[i:i+3] for i in range(0, len(body), 3)]
        else:
            for line in body:
                industry = next((sector for sector in INDUSTRIES if line.endswith(' '+sector)), None)
                if industry is None: raise ValueError('Unrecognized Russell table row')
                prefix = line[:-len(industry)].strip()
                try: name, symbol = prefix.rsplit(None, 1)
                except ValueError: raise ValueError('Incomplete Russell table row') from None
                triples.append((name, symbol, industry))
        for name, symbol, industry in triples:
            if not name or not SYMBOL.fullmatch(symbol) or industry not in INDUSTRIES or symbol in seen:
                raise ValueError('Invalid or duplicate Russell member row')
            seen.add(symbol)
            row = _change(index_key, action, symbol, name, effective_date=effective_date,
                          published_date=published_date, effective_phase=effective_phase)
            row['fields'] = {'Company': name, 'Symbol': symbol, 'Industry': industry,
                             'pdf_page': page_number, 'list_scope': 'change_list_not_complete_index'}
            rows.append(row)
    if not rows: raise ValueError('Empty Russell change list')
    return rows

def pdf_pages(content, *, layout=False):
    if not content.startswith(b'%PDF'): raise ValueError('Official response is not a PDF')
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError('PDF parser dependency unavailable') from None
    reader = PdfReader(io.BytesIO(content))
    if len(reader.pages) > 100: raise ValueError('PDF page limit exceeded')
    return [(page.extract_text(extraction_mode='layout') if layout else page.extract_text()) or '' for page in reader.pages]

def parse_russell_ipo_pages(pages):
    """Layout text keeps company names such as 'ITG Inc' next to their ticker."""
    text='\n'.join(pages)
    index=re.search(r'Russell\s+(1000|2000|3000|Microcap)[®\s]*Index',text)
    effective=re.search(r'Effective\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})',text)
    if not index or not effective or 'Final List of IPO Additions' not in text:
        raise ValueError('Official final IPO list identity and effective date required')
    key='FTSE_RUSSELL:RUSSELL_'+index.group(1).upper()
    day=datetime.strptime(effective.group(1).replace(',',''),'%B %d %Y').date()
    as_at=re.search(r'Data as at:\s*(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})',text)
    source_as_at=datetime.strptime(as_at.group(1),'%d %B %Y').date() if as_at else None
    result=[];seen=set()
    for page_number,page in enumerate(pages,1):
        lines=[line.strip() for line in page.splitlines() if line.strip()]
        header=next((i for i,line in enumerate(lines) if re.fullmatch(r'Ticker\s+Company Name',line)),None)
        if header is None:
            if result and 'All rights reserved' in page: continue
            raise ValueError('IPO table layout unavailable')
        for line in lines[header+1:]:
            if line.startswith(('Please see disclaimer','©')) or re.match(r'Russell .+Index Initial Public Offering',line): break
            parts=line.split(None,1)
            if len(parts)!=2 or not SYMBOL.fullmatch(parts[0]) or parts[0] in seen:
                raise ValueError('Invalid or duplicate IPO table row')
            symbol,name=parts;seen.add(symbol)
            row=_change(key,'add',symbol,name,effective_date=day,published_date=None,effective_phase='unknown')
            row['issues'].append('effective_intraday_phase_unknown')
            row['fields']={'Ticker':symbol,'Company Name':name,'pdf_page':page_number,
                'data_as_at':source_as_at.isoformat() if source_as_at else None,
                'list_scope':'quarterly_ipo_additions_not_complete_index'}
            result.append(row)
    if not result: raise ValueError('Empty IPO change list')
    return result

class _Text(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}: self.skip += 1
        elif tag in {'p','div','h1','h2','br'}: self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in {'script','style'}: self.skip = max(0,self.skip-1)
        elif tag in {'p','div'}: self.parts.append('\n')
    def handle_data(self, text):
        if not self.skip: self.parts.append(text)

def html_text(content):
    parser = _Text(); parser.feed(content.decode('utf-8-sig')); return ' '.join(parser.parts)

def parse_nasdaq_notice(text):
    if 'Nasdaq-100' not in text: raise ValueError('Not a Nasdaq-100 change release')
    month = r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    effective = re.search(r'prior to market open on\s+(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday),?\s+)?'+month+r'\s+(\d{1,2}),?\s+(\d{4})',text,re.I)
    publication = re.search(r'Annual Changes[^\n]*\n\s*'+month+r'\s+(\d{1,2}),?\s+(\d{4})',text,re.I)
    if not publication:
        # Source dateline is independent of the effective-date sentence.
        publication = re.search(r'NEW YORK,\s+([A-Z][a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\s+\(GLOBE NEWSWIRE\)',text)
    if not effective or not publication: raise ValueError('Exact release and effective dates unavailable')
    def parsed(match):
        value = ' '.join(match.groups())
        try: return datetime.strptime(value,'%B %d %Y').date()
        except ValueError: return datetime.strptime(value,'%b %d %Y').date()
    published, day = parsed(publication), parsed(effective)
    result = []
    for action, verb in [('add','added'),('delete','removed')]:
        match = re.search(r'The following[^:]{0,160}\b'+verb+r'[^:]{0,100}:\s*(.*?)(?=\n\s*\n|The following|About Nasdaq|$)',text,re.I|re.S)
        if not match: raise ValueError('Recognized add and delete paragraphs required')
        paragraph = match.group(1).strip()
        tokens = list(re.finditer(r'([^():;]+?)\s*\(Nasdaq:\s*([A-Z0-9./\-]+)\)',paragraph,re.I))
        if not tokens: raise ValueError('No source security symbols in change paragraph')
        for token in tokens:
            name = token.group(1).strip(' ,\n')
            name = re.sub(r'^and\s+', '', name, flags=re.I)
            for symbol in token.group(2).upper().split('/'):
                if not SYMBOL.fullmatch(symbol): raise ValueError('Invalid source symbol')
                row = _change('NASDAQ:NDX',action,symbol,name,effective_date=day,published_date=published,effective_phase='before_open')
                row['announced_date'] = published
                row['fields'] = {'source_paragraph':paragraph,'source_symbol_expression':token.group(2),
                                 'release_date_basis':'official_release_header_or_dateline'}
                result.append(row)
    if len({row['row_key'] for row in result}) != len(result): raise ValueError('Duplicate change symbol')
    return result

def parse_lseg_catalog(payload):
    from ..supplemental.official_catalogs import parse_lseg
    source_rows = parse_lseg(payload)
    total = payload['TotalCount']
    if isinstance(total,bool) or Decimal(str(total)) != int(total) or int(total) < len(source_rows):
        raise ValueError('Invalid source catalog count')
    rows = []
    for source in source_rows:
        row = _base('FTSE_RUSSELL:FAMILY:'+source['entity_key'], 'announcement',source['row_key'])
        row.update(announced_date=source['announced_date'],published_date=source['published_date'],
                   effective_date=source['effective_date'],fields=source['fields'],issues=source['issues']+['index_identity_unresolved','baseline_unavailable'])
        rows.append(row)
    return rows,int(total)

def parse_snapshot_csv(content, *, index_key, snapshot_date, weight_date, published_date, weight_unit, expected_member_count):
    if not index_key or type(snapshot_date) is not date or expected_member_count <= 0:
        raise ValueError('Explicit official snapshot scope/count required')
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig'),newline=''))
    if reader.fieldnames != ['Symbol','Company','Weight']: raise ValueError('Snapshot schema changed')
    rows, seen = [], set()
    for source in reader:
        if None in source or any(v is None for v in source.values()): raise ValueError('Malformed snapshot row')
        symbol, name = source['Symbol'], source['Company']
        if not SYMBOL.fullmatch(symbol) or not name.strip() or symbol in seen: raise ValueError('Invalid snapshot identity')
        seen.add(symbol)
        row = _base(index_key,'snapshot_member',symbol)
        weight = None
        if source['Weight']:
            if type(weight_date) is not date or weight_unit not in {'percent','fraction'}: raise ValueError('Exact weight day/unit required')
            try: weight = Decimal(source['Weight'])
            except InvalidOperation: raise ValueError('Invalid exact weight') from None
            if not weight.is_finite() or weight < 0: raise ValueError('Invalid exact weight')
        row.update(member_identifier=symbol,member_name=name,snapshot_date=snapshot_date,
                   published_date=published_date,weight_date=weight_date,weight=weight,weight_unit=weight_unit)
        row['fields'] = {'raw_row':source,'source_snapshot_complete':True,'declared_member_count':expected_member_count}
        row['issues'] = ['historical_security_identity_unresolved']
        if published_date is None: row['issues'].append('publication_unavailable')
        if weight is None: row['issues'].append('weight_unavailable')
        rows.append(row)
    if len(rows) != expected_member_count: raise ValueError('Snapshot member count differs from source declaration')
    return rows
