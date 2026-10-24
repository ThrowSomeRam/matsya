"""Scenario transforms: one primitive, a typed list of changes to the simulation engine's inputs.

Applied at run time to a copy of the inputs (scenarios.apply), never to stored tables or to
any fitted component, so overrides never update learned parameters. A list is stored in full
with the run (canonical JSON plus its sha256) so the run can be replayed exactly.

Types:
  rate_multiplier  weekly payment chance x factor for buyers / sectors / all buyers, or the
                   order rate x factor (purchases follow via the purchase ratio)
  date_shift       move fixed obligations (by kind or obligation_id) or every future supplier
                   payment by N days
  cash_event       one-off amount (signed: + inflow, - outflow) on a date, or on a day drawn
                   uniformly per path in [on, window_end]
  terms_change     buyer terms -> to_days (future weeks of open and future invoices); supplier
                   terms from_days -> to_days (supplier payments move by to_days - from_days)
  freeze           a fraction of the balance unusable for some days

Each type has one magnitude (MAGNITUDE) so the breaking-point search can scale it.
"""

import hashlib
import json
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class _T(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RateMultiplier(_T):
    type: Literal["rate_multiplier"] = "rate_multiplier"
    target: Literal["buyer", "sector", "all_buyers", "orders"]
    ids: list[str] = []  # buyer_ids or sectors; empty for all_buyers and orders
    factor: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def _ids(self):
        if (self.target in ("buyer", "sector")) != bool(self.ids):
            raise ValueError("ids are required for buyer/sector targets and not allowed otherwise")
        return self


class DateShift(_T):
    type: Literal["date_shift"] = "date_shift"
    target: Literal["obligation_kind", "obligation_id", "supplier_payments"]
    ids: list[str] = []  # obligation kinds (salary, rent, emi, ...) or obligation_ids
    days: int

    @model_validator(mode="after")
    def _ids(self):
        if (self.target != "supplier_payments") != bool(self.ids):
            raise ValueError("ids are required for obligation targets and not allowed for supplier_payments")
        if self.target == "obligation_kind" and "gst" in self.ids:
            raise ValueError("GST dates are statutory and its amount depends on the month; not shiftable")
        return self


class CashEvent(_T):
    type: Literal["cash_event"] = "cash_event"
    on: date
    amount_paise: int  # signed: + inflow, - outflow
    window_end: date | None = None  # day drawn uniformly per path in [on, window_end]
    label: str = ""

    @model_validator(mode="after")
    def _window(self):
        if self.window_end is not None and self.window_end < self.on:
            raise ValueError("window_end before on")
        return self


class TermsChange(_T):
    type: Literal["terms_change"] = "terms_change"
    party: Literal["buyer", "supplier"]
    ids: list[str] = []  # buyer_ids; empty = every buyer. Not used for suppliers (all of them)
    from_days: Annotated[int, Field(ge=0)] | None = None  # required for suppliers
    to_days: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _supplier(self):
        if self.party == "supplier" and (self.from_days is None or self.ids):
            raise ValueError("supplier terms need from_days and apply to every supplier")
        return self


class Freeze(_T):
    type: Literal["freeze"] = "freeze"
    start: date
    days: Annotated[int, Field(ge=1)]
    fraction: Annotated[float, Field(ge=0, le=1)]


Transform = Annotated[RateMultiplier | DateShift | CashEvent | TermsChange | Freeze, Field(discriminator="type")]
TRANSFORM_LIST = TypeAdapter(list[Transform])

# The one field a breaking-point search scales, per type.
MAGNITUDE = {"rate_multiplier": "factor", "date_shift": "days", "cash_event": "amount_paise",
             "terms_change": "to_days", "freeze": "fraction"}


def to_json(transforms: list) -> str:
    """Canonical JSON (sorted keys, no spaces): what is stored with the run and hashed."""
    return json.dumps(TRANSFORM_LIST.dump_python(list(transforms), mode="json"), sort_keys=True,
                      separators=(",", ":"))


def from_json(s: str) -> list:
    return TRANSFORM_LIST.validate_json(s)


def transforms_hash(transforms: list) -> str:
    return hashlib.sha256(to_json(transforms).encode()).hexdigest()


def with_magnitude(t, m: float):
    """A copy of transform t with its magnitude field set to m (ints rounded)."""
    field = MAGNITUDE[t.type]
    value = m if isinstance(getattr(t, field), float) else int(round(m))
    return type(t).model_validate({**t.model_dump(), field: value})
