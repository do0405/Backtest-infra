"""Estimate only a fixed audit pass, never full historical data eligibility."""

def fixed_pass_eta_hours(previous,current,target,started_at,checked_at):
    if any(type(v) is not int for v in (previous,current,target)):
        return None
    if not 0<=previous<=current<=target or started_at is None or checked_at is None:
        return None
    if started_at.tzinfo is None or checked_at.tzinfo is None:
        return None
    elapsed=(checked_at-started_at).total_seconds()/3600
    if elapsed<=0:return None
    if current==target:return 0.0
    progress=current-previous
    if progress<=0:return None
    return (target-current)*elapsed/progress


def bound_pass_eta_hours(previous, current, *, after):
    """Use matching fixed targets and fresh post-change observations only."""
    if not previous.get("binding") or previous.get("binding") != current.get("binding"):
        return None
    if previous.get("target") != current.get("target"):
        return None
    clocks = (after, previous.get("observed_at"), current.get("observed_at"),
              previous.get("updated_at"), current.get("updated_at"))
    if any(value is None or value.tzinfo is None for value in clocks):
        return None
    floor, start, end, updated_start, updated_end = clocks
    if not floor <= updated_start <= start < end or not floor <= updated_end <= end:
        return None
    if updated_end < updated_start:
        return None
    return fixed_pass_eta_hours(previous.get("count"), current.get("count"),
                                current.get("target"), start, end)
