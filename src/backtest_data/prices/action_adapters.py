"""Contract-driven action normalization without guessing undocumented vendor codes."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .actions import CorporateAction
from .normalize import decimal_value


@dataclass(frozen=True, slots=True)
class ActionFieldContract:
    """Verified canonical->provider fields and provider->canonical action codes.

    Vendor code meanings and split-ratio direction must be established in the
    referenced contract before using this mapper. Date precision is preserved
    as dates; public availability is supplied separately by the capture layer.
    """

    evidence_ref: str
    fields: tuple[tuple[str, str], ...]
    action_types: tuple[tuple[str, str], ...]
    date_format: str

    def __post_init__(self) -> None:
        names = [name for name, _ in self.fields]
        types = [code for code, _ in self.action_types]
        supported = {"event_id", "action_type", "effective_date", "ex_date", "pay_date", "announced_at",
                     "split_ratio", "cash_amount", "currency", "received_security_id", "cash_share_basis", "cancelled"}
        if not self.evidence_ref or not self.date_format or len(set(names)) != len(names) or len(set(types)) != len(types):
            raise ValueError("verified contract and unique canonical/action-code mappings are required")
        if not {"event_id", "action_type", "effective_date"} <= set(names) or not set(names) <= supported:
            raise ValueError("action contract is missing required fields or maps unsupported canonical fields")
        if any(not field for _, field in self.fields):
            raise ValueError("provider field names must be explicit")


def normalize_action_revision(row: Mapping[str, Any], *, contract: ActionFieldContract,
                              observation_id: str, security_id: str, revision: int,
                              available_at: datetime | None, captured_at: datetime,
                              source_contract_id: str, raw_ref: str) -> CorporateAction:
    """Map one action revision and preserve unknown event types as unsupported.

    Missing cash, ratio, ex/pay date, received security, or share basis stays
    unknown. Raw provenance preserves the original complete event; this mapper
    does not treat a bar's opaque action flags as sufficient event evidence.
    """
    values: dict[str, Any] = {}
    for canonical, provider in contract.fields:
        value = row.get(provider)
        if value is None or value == "":
            continue
        if canonical in {"split_ratio", "cash_amount"}:
            value = decimal_value(value)
        elif canonical in {"effective_date", "ex_date", "pay_date"}:
            value = datetime.strptime(str(value), contract.date_format).date()
        elif canonical == "announced_at":
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        elif canonical == "action_type":
            value = dict(contract.action_types).get(str(value), f"unsupported:{value}")
        elif canonical == "cancelled":
            if type(value) is not bool:
                raise ValueError("cancelled field needs an explicitly verified boolean mapping")
        else:
            value = str(value)
        values[canonical] = value
    if not {"event_id", "action_type", "effective_date"} <= values.keys():
        raise ValueError("action event identity/type/effective_date is missing")
    values.setdefault("ex_date", None)
    return CorporateAction(id=observation_id, security_id=security_id, revision=revision,
                            available_at=available_at, captured_at=captured_at,
                            source_contract_id=source_contract_id, raw_ref=raw_ref, **values)
