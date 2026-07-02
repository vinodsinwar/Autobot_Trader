"""Position sizing: channel sizing config + instrument → order quantity."""
import math
from dataclasses import dataclass
from typing import Any

from app.db.models import Instrument
from app.parsing.schema import ParsedSignal


class SizingError(Exception):
    pass


@dataclass(slots=True)
class SizedOrder:
    qty: float          # exchange units (F&O: units = lots*lot_size; Delta: contracts)
    notional: float | None


def size_order(
    parsed: ParsedSignal,
    instrument: Instrument,
    sizing: dict[str, Any] | None,
    max_capital_per_trade: float | None = None,
) -> SizedOrder:
    """sizing = {"mode": "lots"|"units"|"capital", "value": n}. Default: 1 lot."""
    sizing = sizing or {}
    mode = sizing.get("mode", "lots")
    value = float(sizing.get("value", 1))
    lot = float(instrument.lot_size or 1)
    price = parsed.entry_price or parsed.entry_range_high

    if mode == "lots":
        qty = value * lot if instrument.instrument_type in ("CE", "PE", "FUT") else value
        if instrument.instrument_type == "PERP":
            qty = value  # value = number of contracts
    elif mode == "units":
        qty = value
    elif mode == "capital":
        if not price:
            raise SizingError("capital sizing needs an entry price (market orders excluded)")
        if instrument.instrument_type in ("CE", "PE", "FUT"):
            lots = math.floor(value / (price * lot))
            qty = lots * lot
        elif instrument.instrument_type == "PERP":
            # Delta contract notional = contract_value(underlying units) * price
            qty = math.floor(value / (float(instrument.lot_size) * price))
        else:
            qty = math.floor(value / price)
    else:
        raise SizingError(f"unknown sizing mode {mode!r}")

    if qty <= 0:
        raise SizingError(f"sizing produced qty {qty} (mode={mode}, value={value}, price={price})")

    notional = qty * price if price else None
    if instrument.instrument_type == "PERP" and price:
        notional = qty * float(instrument.lot_size) * price
    if max_capital_per_trade and notional and notional > float(max_capital_per_trade):
        raise SizingError(
            f"notional {notional:.0f} exceeds max capital per trade {max_capital_per_trade:.0f}"
        )
    return SizedOrder(qty=qty, notional=notional)
