"""Resumable full document coverage with one bounded SQL page per transaction."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..config import Settings
from ..db import make_engine
from .completion import COMPLETION_POLICY
from .completion_storage import materialize_financial_completion

WORKER_LOCK_KEY = int.from_bytes(hashlib.sha256((COMPLETION_POLICY + ':worker').encode()).digest()[:8],
                                'big', signed=True)


def _write_checkpoint(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def run_completion_worker(database, *, checkpoint_path, batch_size=1, max_source_facts=5000,
                          max_batches=None, materializer=materialize_financial_completion):
    """Continue until no unaudited document remains; never truncate its facts.

    Existing SQL audit coverage is the membership checkpoint. The keyset cursor
    is an optimization, and wraps once when a page is empty so newly collected
    random UUIDs before the cursor are also included. Oversized documents have a
    durable SQL failure plus an explicit deferred ID in this checkpoint.
    """
    path = Path(checkpoint_path)
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'policy': COMPLETION_POLICY, 'status': 'running', 'after_document_id': '',
        'documents': 0, 'scanned': 0, 'mapped': 0, 'fiscal_qualified': 0,
        'created': 0, 'reused': 0, 'audited': 0, 'batches': 0, 'reasons': {}, 'deferred_document_ids': []}
    if state.get('policy') != COMPLETION_POLICY:
        raise ValueError('completion_checkpoint_policy_mismatch')
    if state.get('status') in {'complete', 'complete_with_deferred_documents'}:
        state['after_document_id'] = ''
    state['status'] = 'running'
    state['batch_size'], state['max_source_facts'] = batch_size, max_source_facts
    batches = 0
    while max_batches is None or batches < max_batches:
        summary = materializer(database, max_documents=batch_size,
            after_document_id=state['after_document_id'], max_source_facts=max_source_facts, only_pending=True)
        if not summary.documents:
            if state['after_document_id']:
                state['after_document_id'] = ''
                continue
            state['status'] = 'complete_with_deferred_documents' if state['deferred_document_ids'] else 'complete'
            state['updated_at'] = datetime.now(timezone.utc).isoformat()
            _write_checkpoint(path, state)
            return state
        for key in ('documents', 'scanned', 'mapped', 'fiscal_qualified', 'created', 'reused', 'audited'):
            state[key] += getattr(summary, key)
        state['reasons'] = dict(sorted((Counter(state['reasons']) + Counter(summary.reasons)).items()))
        if summary.reasons.get('completion_source_fact_limit'):
            if batch_size != 1:
                # IDs are exact only for a one-document page, the default.
                raise ValueError('deferred_document_checkpoint_requires_batch_size_one')
            state['deferred_document_ids'] = sorted(set(state['deferred_document_ids']) | {summary.next_after_document_id})
        elif batch_size == 1:
            state['deferred_document_ids'] = [identity for identity in state['deferred_document_ids']
                                              if identity != summary.next_after_document_id]
        state['after_document_id'] = summary.next_after_document_id
        state['batches'] += 1
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        _write_checkpoint(path, state)
        batches += 1
    return state


def run_guarded_completion_worker(database, **kwargs):
    """Hold a dedicated session advisory lock while bounded transactions commit."""
    with database.connect() as guard:
        acquired = guard.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': WORKER_LOCK_KEY})
        guard.commit()
        if not acquired:
            return {'status': 'blocked', 'reason': 'financial_completion_worker_already_running'}
        try:
            return run_completion_worker(database, **kwargs)
        finally:
            if not guard.invalidated:
                guard.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': WORKER_LOCK_KEY})
                guard.commit()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--batch-size', type=int, default=1, choices=(1,))
    parser.add_argument('--max-source-facts', type=int, default=50000)
    parser.add_argument('--max-batches', type=int)
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = make_engine(Settings.from_env().database_url)
        result = run_guarded_completion_worker(engine, checkpoint_path=args.checkpoint, batch_size=args.batch_size,
            max_source_facts=args.max_source_facts, max_batches=args.max_batches)
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
