"""The one schema every signal must satisfy before it can touch money.

Both the deterministic rule engine and the LLM fallback must produce a
`ParsedSignal` that passes validation; anything else is PARSE_FAILED.
"""
import enum
import hashlib
import json

from pydantic import BaseModel, Field, model_validator


class Action(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class InstrumentKind(enum.StrEnum):
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    EQUITY = "EQUITY"
    CRYPTO_PERP = "CRYPTO_PERP"
    CRYPTO_SPOT = "CRYPTO_SPOT"


class OptionType(enum.StrEnum):
    CE = "CE"
    PE = "PE"


class EntryType(enum.StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    ABOVE = "ABOVE"   # buy when price crosses above (stop-limit entry)
    BELOW = "BELOW"   # enter when price crosses below


class SignalKind(enum.StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    MODIFY = "MODIFY"


class ParsedSignal(BaseModel):
    kind: SignalKind = SignalKind.ENTRY
    action: Action
    symbol: str = Field(min_length=1, description="Underlying/base symbol, e.g. NIFTY, RELIANCE, BTCUSD")
    instrument: InstrumentKind
    strike: float | None = None
    option_type: OptionType | None = None
    expiry_hint: str | None = Field(default=None, description="e.g. 'weekly', '28NOV', 'monthly'")

    entry_type: EntryType = EntryType.MARKET
    entry_price: float | None = None
    entry_range_low: float | None = None
    entry_range_high: float | None = None

    targets: list[float] = Field(default_factory=list)
    stop_loss: float | None = None

    quantity_hint: str | None = Field(default=None, description="e.g. '2 lots', '0.1 BTC'")
    notes: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _self_consistent(self) -> "ParsedSignal":
        if self.instrument == InstrumentKind.OPTION:
            if self.strike is None or self.option_type is None:
                raise ValueError("option signal requires strike and option_type")
        ref = self.entry_price or self.entry_range_low
        if self.kind == SignalKind.ENTRY and ref is not None:
            # For a BUY, stop must sit below entry and targets above (mirror for SELL).
            # A violated relation almost always means a mis-parsed/mis-heard number.
            if self.stop_loss is not None:
                if self.action == Action.BUY and self.stop_loss >= ref:
                    raise ValueError(f"BUY signal with stop_loss {self.stop_loss} >= entry {ref}")
                if self.action == Action.SELL and self.stop_loss <= ref:
                    raise ValueError(f"SELL signal with stop_loss {self.stop_loss} <= entry {ref}")
            for t in self.targets:
                if self.action == Action.BUY and t <= ref:
                    raise ValueError(f"BUY signal with target {t} <= entry {ref}")
                if self.action == Action.SELL and t >= ref:
                    raise ValueError(f"SELL signal with target {t} >= entry {ref}")
        return self

    def dedup_hash(self) -> str:
        """Stable hash over the trade-relevant fields, used for duplicate suppression."""
        core = {
            "kind": self.kind.value,
            "action": self.action.value,
            "symbol": self.symbol.upper(),
            "instrument": self.instrument.value,
            "strike": self.strike,
            "option_type": self.option_type.value if self.option_type else None,
            "entry_price": self.entry_price,
            "targets": sorted(self.targets),
            "stop_loss": self.stop_loss,
        }
        return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()

    def human_summary(self) -> str:
        inst = self.symbol.upper()
        if self.instrument == InstrumentKind.OPTION:
            inst = f"{inst} {self.strike:g} {self.option_type.value}"
        elif self.instrument == InstrumentKind.FUTURE:
            inst = f"{inst} FUT"
        entry = {
            EntryType.MARKET: "at market",
            EntryType.LIMIT: f"@ {self.entry_price}",
            EntryType.ABOVE: f"above {self.entry_price}",
            EntryType.BELOW: f"below {self.entry_price}",
        }[self.entry_type]
        parts = [f"{self.action.value} {inst} {entry}"]
        if self.targets:
            parts.append("TGT " + "/".join(f"{t:g}" for t in self.targets))
        if self.stop_loss is not None:
            parts.append(f"SL {self.stop_loss:g}")
        return " ".join(parts)


class ParseResult(BaseModel):
    ok: bool
    signal: ParsedSignal | None = None
    parser: str = ""            # rules:<pack> | llm:<model> | relay
    error: str = ""
    latency_ms: float = 0.0
