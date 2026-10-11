from datetime import datetime, timedelta, timezone

from backtest_data.ops.performance_status import fixed_pass_eta_hours


def test_remaining_pass_time_uses_observed_committed_cursor_rate():
    start=datetime(2026,10,11,tzinfo=timezone.utc)
    assert fixed_pass_eta_hours(100,120,200,start,start+timedelta(hours=1))==4


def test_stale_or_nonprogressing_samples_do_not_claim_completion_time():
    start=datetime(2026,10,11,tzinfo=timezone.utc)
    assert fixed_pass_eta_hours(100,100,200,start,start+timedelta(hours=1)) is None
    assert fixed_pass_eta_hours(100,120,200,start,start) is None
    assert fixed_pass_eta_hours(100,None,200,start,start+timedelta(hours=1)) is None


def test_completed_fixed_pass_is_zero_but_not_a_whole_dataset_certificate():
    start=datetime(2026,10,11,tzinfo=timezone.utc)
    assert fixed_pass_eta_hours(100,200,200,start,start+timedelta(hours=1))==0


def sample(count, total, observed, updated=None, binding="same"):
    return {"count": count, "target": total, "binding": binding,
            "observed_at": observed, "updated_at": updated or observed}


def test_bound_sample_uses_current_target_and_observation_interval():
    from backtest_data.ops.performance_status import bound_pass_eta_hours
    start=datetime(2026,10,11,tzinfo=timezone.utc)
    a=sample(100,300,start, start-timedelta(minutes=1))
    b=sample(120,300,start+timedelta(hours=1),start+timedelta(minutes=30))
    assert bound_pass_eta_hours(a,b,after=start-timedelta(minutes=2))==9


def test_bound_sample_refuses_changed_audit_or_pre_change_file():
    from backtest_data.ops.performance_status import bound_pass_eta_hours
    start=datetime(2026,10,11,tzinfo=timezone.utc)
    a=sample(100,300,start)
    b=sample(120,300,start+timedelta(hours=1))
    assert bound_pass_eta_hours(a,{**b,"binding":"other"},after=start) is None
    assert bound_pass_eta_hours(a,{**b,"target":400},after=start) is None
    assert bound_pass_eta_hours({**a,"updated_at":start-timedelta(hours=1)},b,after=start) is None
    assert bound_pass_eta_hours({**a,"binding":None},b,after=start) is None


def test_bound_sample_refuses_future_or_naive_updated_clock():
    from backtest_data.ops.performance_status import bound_pass_eta_hours
    start=datetime(2026,10,11,tzinfo=timezone.utc)
    a=sample(100,300,start)
    b=sample(120,300,start+timedelta(hours=1))
    assert bound_pass_eta_hours({**a,"updated_at":start+timedelta(seconds=1)},b,after=start) is None
    assert bound_pass_eta_hours({**a,"updated_at":start.replace(tzinfo=None)},b,after=start) is None
