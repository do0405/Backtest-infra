from datetime import timedelta
import json

import pytest

from backtest_data.db import utcnow
from backtest_data.fundamentals.publication_runner import audit_publications,validate_audit
from backtest_data.fundamentals.public_version_batch import load_publication_batch,run_publication_batch
from test_public_pit_queries import setup,original
from datetime import date

pytestmark=pytest.mark.integration


def test_full_document_audit_and_resumable_sql_outcomes_preserve_source_clocks(db_session,tmp_path):
    issuer,target=setup(db_session)
    item=original(db_session,issuer,'100','0000010000-24-000001',date(2024,3,1))
    document,filing,facts,_,_=item
    old=(document.captured_at,document.available_at,facts[0].captured_at,facts[0].first_seen_at)
    watermark=utcnow()
    path=tmp_path/'audit.json'
    summary=audit_publications(db_session,manifest_id=target.id,watermark=watermark,output_path=path)
    audit=validate_audit(json.loads(path.read_text(encoding='utf-8')))
    assert summary['original_target']['security_count']==1
    assert document.id in [row['id'] for row in audit['documents']]
    targets=load_publication_batch(db_session,watermark=watermark,document_ids=[document.id],limit=1)
    assert len(targets[0].sql_facts)==1
    def unexpected_fetch(request):raise AssertionError('Missing metadata must fail before HTTP')
    first=run_publication_batch(db_session,targets=targets,watermark=watermark,fetch=unexpected_fetch)
    repeat=run_publication_batch(db_session,targets=targets,watermark=watermark,fetch=unexpected_fetch)
    assert first['unavailable_count']==1
    assert first['results'][0]['evidence_ids']==repeat['results'][0]['evidence_ids']
    assert first['next_cursor']==document.id
    assert (document.captured_at,document.available_at,facts[0].captured_at,facts[0].first_seen_at)==old
