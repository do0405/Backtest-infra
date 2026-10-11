from datetime import date


def date_only_available_at(published_date: date, sessions: list[dict]):
    candidates = [item for item in sessions if item["session"] > published_date]
    if not candidates:
        raise ValueError("Verified calendar has no next session; do not invent availability")
    return min(candidates, key=lambda item: item["session"])["open_at"]
