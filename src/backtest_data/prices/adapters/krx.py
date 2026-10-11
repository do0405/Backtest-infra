"""KRX ingestion is gated on an approved machine-readable source contract."""


def normalize_krx_bar(row, **contract):
    """Refuse an invented endpoint/schema until the source investigation is verified."""
    raise NotImplementedError("KRX machine-readable endpoint and field contract have not been verified")
