"""Optional source stage: exact-hash SEC metadata, never filing body archival."""
import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from sqlalchemy import or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from ..config import Settings
from ..credentials import load_credentials
from ..db import make_engine, utcnow
from ..ingestion.http import ProviderError, ProviderHTTP
from ..providers import SEC
from ..releases.models import ValidationIssue
from .completion import METADATA_POLICY
from .completion_storage import _annotation, _lock_document, _write_session, supplement_sec_metadata
from .completion_worker import _write_checkpoint
from .models import Filing, FinancialSourceDocument

METADATA_LOCK_KEY = int.from_bytes(hashlib.sha256((METADATA_POLICY + ':worker').encode()).digest()[:8],
                                  'big', signed=True)


@dataclass
class MetadataSummary:
    processed: int = 0
    reported: int = 0
    unavailable: int = 0
    source_blocked: bool = False
    next_after_document_id: str | None = None
    reasons: dict[str, int] = field(default_factory=dict)


def provider_failure(exc):
    return 'sec_source_' + exc.code, bool(exc.retryable or exc.code in {
        '401', '403', 'requires_identifying_user_agent'})


def supplement_sec_metadata_batch(database, *, sec, document_ids=None, max_documents=1,
                                  after_document_id='', retry_source_failures=False):
    """Fetch a bounded pending page before acquiring any document write locks."""
    if type(max_documents) is not int or not 1 <= max_documents <= 100:
        raise ValueError('bounded_metadata_document_limit_required')
    if document_ids is not None and len(document_ids) > max_documents:
        raise ValueError('explicit_metadata_documents_exceed_bound')
    assertion = aliased(ValidationIssue)
    covered = select(assertion.id).where(assertion.record_domain == 'financial_source_document',
        assertion.record_id == FinancialSourceDocument.id, assertion.rule == METADATA_POLICY,
        assertion.evidence['document_hash'].astext == FinancialSourceDocument.content_hash)
    if retry_source_failures:
        covered = covered.where(or_(assertion.evidence['source_reason'].astext.is_(None),
            ~assertion.evidence['source_reason'].astext.like('sec_source_%')))
    query = select(FinancialSourceDocument.id, FinancialSourceDocument.source_url).join(
        Filing, Filing.id == FinancialSourceDocument.filing_id).where(
        Filing.source == 'sec', FinancialSourceDocument.id > after_document_id,
        FinancialSourceDocument.extraction_complete.is_(True), ~covered.exists())
    if document_ids is not None:
        query = query.where(FinancialSourceDocument.id.in_(tuple(document_ids)))
    query = query.order_by(FinancialSourceDocument.id).limit(max_documents)
    if isinstance(database, Session):
        documents = database.execute(query).all()
    else:
        with Session(database) as reader:
            documents = reader.execute(query).all()
    report = MetadataSummary()
    reasons = Counter()
    for document_id, url in documents:
        report.processed += 1
        report.next_after_document_id = document_id
        reason = None
        response = None
        try:
            # ProviderHTTP enforces the existing shared SEC rate/allowlist budget.
            response = sec.fetch(url)
            with _write_session(database) as session:
                row = supplement_sec_metadata(session, document_id=document_id,
                    content=response.content, captured_at=response.captured_at)
                metadata = row.evidence['metadata']
            if metadata['status'] == 'reported':
                report.reported += 1
            else:
                reason = metadata.get('reason', 'unqualified_fiscal_metadata')
        except ProviderError as exc:
            reason, report.source_blocked = provider_failure(exc)
            # Stop on the first transport/provider block. Do not repeat it for
            # every issuer. The checkpoint requires an explicit later retry.
        except ValueError as exc:
            safe_reasons = {'supplement_document_hash_mismatch', 'supplement_numeric_instance_required',
                            'supplement_recording_precedes_capture'}
            reason = str(exc) if str(exc) in safe_reasons else 'sec_fiscal_context_unavailable'
        if reason:
            report.unavailable += 1
            reasons[reason] += 1
            with _write_session(database) as session:
                document = _lock_document(session, document_id)
                _annotation(session, document_id=document_id, rule=METADATA_POLICY,
                    evidence={'document_hash': document.content_hash,
                        'metadata': {'policy': METADATA_POLICY, 'document_hash': document.content_hash,
                                     'status': 'unavailable', 'reason': reason},
                        'source_reason': reason, 'source': 'sec_metadata_refetch',
                        'captured_at': response.captured_at if response is not None else utcnow(),
                        'historical_public_availability_qualified': False}, created_at=utcnow())
        if report.source_blocked:
            break
    report.reasons = dict(sorted(reasons.items()))
    return report


def run_sec_metadata_worker(database, *, sec, checkpoint_path, max_batches=None, retry_source_block=False):
    path = Path(checkpoint_path)
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'policy': METADATA_POLICY, 'status': 'running', 'after_document_id': '',
        'processed': 0, 'reported': 0, 'unavailable': 0, 'reasons': {}}
    if state.get('policy') != METADATA_POLICY:
        raise ValueError('metadata_checkpoint_policy_mismatch')
    if state.get('status') == 'blocked' and not retry_source_block:
        return state
    if state.get('status') in {'complete', 'blocked'}:
        state['after_document_id'] = ''
    state['status'] = 'running'
    batches = 0
    while max_batches is None or batches < max_batches:
        summary = supplement_sec_metadata_batch(database, sec=sec,
            max_documents=1, after_document_id=state['after_document_id'],
            retry_source_failures=retry_source_block)
        if not summary.processed:
            if state['after_document_id']:
                state['after_document_id'] = ''
                continue
            state['status'] = 'complete'
            _write_checkpoint(path, state)
            return state
        for key in ('processed', 'reported', 'unavailable'):
            state[key] += getattr(summary, key)
        state['reasons'] = dict(sorted((Counter(state['reasons']) + Counter(summary.reasons)).items()))
        state['after_document_id'] = summary.next_after_document_id
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        if summary.source_blocked:
            state.update(status='blocked', reason=next(iter(summary.reasons)), details_redacted=True)
        _write_checkpoint(path, state)
        if summary.source_blocked:
            return state
        batches += 1
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--max-batches', type=int)
    parser.add_argument('--retry-source-block', action='store_true')
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = make_engine(Settings.from_env().database_url)
        with engine.connect() as guard:
            acquired = guard.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': METADATA_LOCK_KEY})
            guard.commit()
            if not acquired:
                result = {'status': 'blocked', 'reason': 'sec_metadata_worker_already_running'}
            else:
                try:
                    transport = ProviderHTTP(engine)
                    result = run_sec_metadata_worker(engine, sec=SEC(transport, load_credentials(sources=["sec"])),
                        checkpoint_path=args.checkpoint, max_batches=args.max_batches,
                        retry_source_block=args.retry_source_block)
                finally:
                    if not guard.invalidated:
                        guard.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': METADATA_LOCK_KEY})
                        guard.commit()
        print(json.dumps(result, sort_keys=True))
        return 2 if result.get('status') == 'blocked' else 0
    except (ValueError, SQLAlchemyError) as exc:
        result = {'status': 'blocked', 'reason': type(exc).__name__, 'details_redacted': True}
        if isinstance(exc, SQLAlchemyError):
            from ..ops.db_recovery import database_error_payload
            result['database_error'] = database_error_payload(exc)
        print(json.dumps(result, sort_keys=True))
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
