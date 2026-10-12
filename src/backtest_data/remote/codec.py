"""Exact typed values and fail-closed privacy checks for portable payloads."""
from datetime import date, datetime, timezone
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
import json
import re
from urllib.parse import urlsplit, parse_qsl

PRIVATE_KEY = re.compile(r"(?:^|_)(?:api_key|apikey|secret|password|auth|authorization|authentication|token|credential|credentials|database_url|configuration|config)(?:_|$)|^lease(?:_(?:owner|until|expiry|id))?$", re.I)


def private_field(key):
    key = str(key)
    # Retain only short schema field names, never arbitrarily large input keys.
    return _private_field(key) if len(key) <= 256 else _classify_private_field(key)


@lru_cache(maxsize=4096)
def _private_field(key):
    return _classify_private_field(key)


def _classify_private_field(key):
    # SEC taxonomy names such as OperatingLeaseCost are public financial data.
    normalized=re.sub(r'(?<=[a-z0-9])(?=[A-Z])','_',key).replace('-','_').lower()
    return bool(PRIVATE_KEY.search(normalized))


@dataclass(frozen=True, repr=False)
class PaginationCursor:
    """An explicitly typed API pagination cursor, never an authentication token."""
    value: str

    def __post_init__(self):
        if not isinstance(self.value,str) or not self.value or len(self.value.encode('utf-8'))>4096 or any(ord(c)<33 or ord(c)>126 for c in self.value) or self.value.startswith(('Bearer','http:','https:')):
            raise ValueError('bounded opaque pagination cursor required')


def _private(mapping):
    return any(private_field(k) and not (k in {'next_page_token','page_token'} and isinstance(v,PaginationCursor)) for k,v in mapping.items())


def _validate_string(value):
    if value.startswith(('https://', 'http://')):
        url = urlsplit(value)
        if url.username or url.password or any(private_field(k) for k, _ in parse_qsl(url.query)):
            raise ValueError('private URL is forbidden in payload')


def reject_constant(_):
    raise ValueError('nonfinite JSON')


def pack(value):
    if isinstance(value, PaginationCursor): return {"$type":"pagination_cursor","value":value.value}
    if isinstance(value, float):
        raise ValueError("binary float is forbidden in exact payload")
    if isinstance(value, Decimal):
        if not value.is_finite(): raise ValueError("nonfinite Decimal")
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None: raise ValueError("aware UTC clock required")
        return {"$type": "datetime", "value": value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, date): return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, dict):
        if "$type" in value: raise ValueError("reserved typed payload key")
        if _private(value): raise ValueError("private field is forbidden in payload")
        return {str(k): pack(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)): return [pack(v) for v in value]
    if isinstance(value, str): _validate_string(value)
    if value is None or isinstance(value, (str, bool, int)): return value
    raise ValueError("unsupported portable value type")


def unpack(value):
    if isinstance(value, dict):
        if "$type" in value:
            if set(value) != {"$type", "value"}: raise ValueError("invalid typed payload")
            kind, raw = value["$type"], value["value"]
            if kind == "decimal":
                result = Decimal(raw)
                if not result.is_finite(): raise ValueError("nonfinite Decimal")
                return result
            if kind == "date": return date.fromisoformat(raw)
            if kind == "pagination_cursor": return PaginationCursor(raw)
            if kind == "datetime":
                result = datetime.fromisoformat(raw)
                if result.tzinfo is None or result.utcoffset().total_seconds() != 0: raise ValueError("aware UTC clock required")
                return result
            raise ValueError("unknown portable type")
        decoded={k:unpack(v) for k,v in value.items()}
        if _private(decoded): raise ValueError("private field is forbidden in payload")
        return decoded
    if isinstance(value, list): return [unpack(v) for v in value]
    if isinstance(value, float): raise ValueError("binary float is forbidden")
    if isinstance(value, str): _validate_string(value)
    return value


def dumps(value):
    return json.dumps(pack(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def loads(value):
    # Unpack validates keys, typed values and decoded URLs as it visits them.
    return unpack(json.loads(value, parse_constant=reject_constant))
