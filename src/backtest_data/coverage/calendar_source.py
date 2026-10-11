"""Narrow read-only US calendar acquisition; broker declarations stay labeled."""
from datetime import date,time
import json
from urllib.parse import urlsplit
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from ..catalog.sources import ensure_contract
from ..db import new_id, transaction
from ..ingestion.http import ProviderHTTP
from ..ingestion.jobs import stable_hash
from ..ingestion.models import IngestionAttempt, RawObject
from ..ingestion.raw_store import RawStore
from .eligibility import normalize_calendar_rows

VERSION = 'alpaca_us_calendar_v1'
DOCUMENTATION = 'https://docs.alpaca.markets/us/reference/legacycalendar'


class CalendarHTTP(ProviderHTTP):
    def _validate(self, source, method, url):
        parts = urlsplit(url)
        if (source != 'alpaca' or method != 'GET' or parts.scheme != 'https' or
            parts.hostname not in {'paper-api.alpaca.markets','api.alpaca.markets'} or
            parts.port not in (None,443) or parts.username or parts.password or
            parts.path != '/v2/calendar' or parts.query or parts.fragment):
            raise ValueError('calendar transport permits only the documented read-only calendar')


def decode_calendar(content, *, start, end):
    if len(content) > 2*1024*1024:
        raise ValueError('calendar response exceeds bounded size')
    try:
        rows = json.loads(content)
        result = normalize_calendar_rows(rows,start=start,end=end,complete_interval=True)
        for row in result:
            if row['status'] != 'open' or time.fromisoformat(row['open']) >= time.fromisoformat(row['close']):
                raise ValueError('invalid session clock')
        return result
    except (ValueError,KeyError,TypeError,UnicodeError):
        raise ValueError('invalid complete calendar response') from None


def calendar_request(start,end):
    return {'start':start.isoformat(),'end':end.isoformat(),'date_type':'TRADING'}


def collect_alpaca_calendar(engine,credentials,raw_root,*,start,end,live=False):
    if start > end or (end-start).days > 20*366:
        raise ValueError('bounded calendar interval required')
    if not credentials.alpaca_key or not credentials.alpaca_secret:
        raise ValueError('Alpaca configuration required')
    url = ('https://api.alpaca.markets' if live else 'https://paper-api.alpaca.markets')+'/v2/calendar'
    http = CalendarHTTP(engine)
    try:
        response = http.request('alpaca','GET',url,params=calendar_request(start,end),headers={'APCA-API-KEY-ID':credentials.alpaca_key.reveal(),
            'APCA-API-SECRET-KEY':credentials.alpaca_secret.reveal()},max_bytes=2*1024*1024)
    finally:
        http.close()
    rows = decode_calendar(response.content,start=start,end=end)
    stored = RawStore(raw_root).put(response.content)
    with transaction(engine) as session:
        session.execute(text("SET LOCAL statement_timeout='20s'"))
        contract = ensure_contract(session,source='alpaca',domain='calendar',market='US',specification={
            'endpoint':url,'version':VERSION,'documentation':DOCUMENTATION,'complete_interval_contract':True,
            'grade':'provider_declared','authority':'broker','timezone':'America/New_York'})
        session.execute(insert(RawObject).values(id=new_id(),content_hash=stored.content_hash,
            storage_key=stored.storage_key,byte_count=stored.byte_count).on_conflict_do_nothing(index_elements=[RawObject.content_hash]))
        raw = session.scalar(select(RawObject).where(RawObject.content_hash==stored.content_hash))
        attempt = IngestionAttempt(id=new_id(),source_contract_id=contract.id,
            request_hash=stable_hash([VERSION,calendar_request(start,end)]),content_hash=stored.content_hash,status='received',
            captured_at=response.captured_at,evidence={'raw_id':raw.id,'start':start.isoformat(),'end':end.isoformat(),
                'row_count':len(rows),'complete_interval':True,'authoritative':False,'source_url':url})
        session.add(attempt)
        session.flush()
        receipt = {'status':'calendar_response_committed','capture_id':attempt.id,'raw_id':raw.id,
            'body_hash':stored.content_hash,'captured_at':response.captured_at.isoformat(),'HTTP_status':response.status_code,
            'row_count':len(rows),'start':start.isoformat(),'end':end.isoformat(),'grade':'provider_declared',
            'exchange_certified':False,'coverage_complete':False}
    return receipt


def load_calendar_capture(session,raw_root,*,start,end,as_of=None):
    from ..catalog.models import SourceContract
    query = select(IngestionAttempt).join(SourceContract,
        SourceContract.id==IngestionAttempt.source_contract_id).where(SourceContract.domain=='calendar',
        SourceContract.source=='alpaca',SourceContract.market=='US',IngestionAttempt.status=='received',
        IngestionAttempt.evidence['start'].astext<=start.isoformat(),
        IngestionAttempt.evidence['end'].astext>=end.isoformat())
    if as_of is not None:query=query.where(IngestionAttempt.captured_at<=as_of)
    attempt=session.scalar(query.order_by(IngestionAttempt.captured_at.desc(),IngestionAttempt.id.desc()).limit(1))
    if attempt is None:
        return None
    raw = session.scalar(select(RawObject).where(RawObject.content_hash==attempt.content_hash))
    if raw is None:
        raise ValueError('calendar raw reference missing')
    content = RawStore(raw_root).read(raw.storage_key)
    capture_start = date.fromisoformat(attempt.evidence['start'])
    capture_end = date.fromisoformat(attempt.evidence['end'])
    rows = decode_calendar(content,start=capture_start,end=capture_end)
    return {'rows':[r for r in rows if start.isoformat()<=r['date']<=end.isoformat()],
        'capture_id':attempt.id,'raw_id':raw.id,'body_hash':raw.content_hash,'captured_at':attempt.captured_at,
        'source_url':attempt.evidence['source_url'],'complete_interval':True,'authoritative':False}
