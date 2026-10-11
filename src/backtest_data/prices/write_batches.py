"""Small price-page writes keep lease checks between bounded WAL bursts."""


# A 100-row price/decision write can exceed the five-minute lease on the
# collector's USB database storage. Bound each statement/issue flush to ten.
PRICE_WRITE_BATCH_SIZE = 10


def flush_price_evidence(session, fence):
    """Flush in the caller's page transaction, checking current ownership."""
    if fence is not None:
        # The heartbeat must run before the pending evidence is flushed,
        # rather than triggering that flush through ORM autoflush itself.
        with session.no_autoflush:
            fence()
    session.flush()
    if fence is not None:
        fence()
