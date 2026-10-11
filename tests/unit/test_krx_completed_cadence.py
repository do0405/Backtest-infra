from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace as N

from backtest_data.remote import source_handlers as mod


SCOPE={'manifest_id':'whole','target_hash':'a'*64,'target_count':17758,
    'market_target_counts':{'KR':4507,'US':13251},'output_start':'2016-01-01','output_end':'2026-10-07'}
NOW=datetime(2026,10,12,0,tzinfo=timezone.utc)


def test_partial_backfill_keeps_existing_refresh_throughput():
    prior={'scope':SCOPE,'planned_through':'2016-01-31'}
    window=mod.krx_plan_window(SCOPE,as_of=NOW,prior=prior)
    assert window['refresh_windows'] and window['start']==date(2016,2,1)


def test_completed_original_pass_only_plans_once_per_eligible_provider_date():
    prior={'scope':SCOPE,'planned_through':'2026-10-07','last_refresh_ready_date':'2026-10-10',
        'refresh_planned_through':'2026-10-10'}
    before=mod.krx_plan_window(SCOPE,as_of=datetime(2026,10,11,23,59,tzinfo=timezone.utc),prior=prior,
        original_complete=True)
    assert before['refresh_windows']==[]
    after=mod.krx_plan_window(SCOPE,as_of=NOW,prior=prior,original_complete=True)
    assert after['refresh_windows'][-1]=={'kind':'rolling','start':date(2026,9,28),'end':date(2026,10,11)}
    assert after['refresh_anchor']==date(2026,10,12) and after['coverage_complete'] is False
    prior['last_refresh_ready_date']='2026-10-11'
    assert mod.krx_plan_window(SCOPE,as_of=NOW,prior=prior,original_complete=True)['refresh_windows']==[]


def test_due_gate_never_stops_existing_refresh_retries(monkeypatch):
    prior={'scope':SCOPE,'planned_through':'2026-10-07','last_refresh_ready_date':'2026-10-11',
        'refresh_planned_through':'2026-10-11'}
    calls=[]
    @contextmanager
    def txn(_):
        yield N(scalars=lambda _:N(yield_per=lambda _:[]))
    monkeypatch.setattr(mod,'transaction',txn)
    monkeypatch.setattr(mod,'_krx_original_complete',lambda *_:True)
    monkeypatch.setattr(mod,'plan_krx_quotes',lambda *_args,**_kwargs:calls.append('plan'))
    monkeypatch.setattr(mod,'run_krx_quote_collection',lambda *_args,**_kwargs:calls.append('collect') or {'status':'partial'})
    state,outcome=mod.krx_quote_step(N(),'raw',scope=SCOPE,previous=prior,budget_seconds=10,as_of=NOW)
    assert calls==['collect'] and state['last_refresh_ready_date']=='2026-10-11'
    assert state['coverage_complete'] is False and outcome['coverage_complete'] is False


def test_completed_cadence_rejects_a_future_ready_marker():
    import pytest
    prior={'scope':SCOPE,'planned_through':'2026-10-07','last_refresh_ready_date':'2026-10-13'}
    with pytest.raises(ValueError,match='future'):
        mod.krx_plan_window(SCOPE,as_of=NOW,prior=prior,original_complete=True)
