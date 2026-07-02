"""Deterministic signal parser.

Strategy: instead of brittle whole-line templates, normalize the message and
extract fields independently (action, instrument, entry, targets, stop-loss).
This survives re-ordering and most formatting drift, runs in microseconds,
and is fully auditable. Per-channel `RulePack`s tune keyword synonyms,
symbol aliases and the default market.
"""
import re
import time
from dataclasses import dataclass, field

from app.parsing.schema import (
    Action,
    EntryType,
    InstrumentKind,
    OptionType,
    ParsedSignal,
    ParseResult,
    SignalKind,
)

_NUM = r"\d+(?:\.\d+)?"


@dataclass
class RulePack:
    name: str = "generic"
    # default instrument market when nothing in the message disambiguates
    default_instrument: InstrumentKind = InstrumentKind.EQUITY
    # map channel-specific slang → canonical symbol, e.g. {"BNF": "BANKNIFTY"}
    symbol_aliases: dict[str, str] = field(default_factory=dict)
    buy_words: tuple[str, ...] = ("BUY", "LONG", "BOUGHT", "ACCUMULATE")
    sell_words: tuple[str, ...] = ("SELL", "SHORT", "SOLD")
    exit_words: tuple[str, ...] = ("EXIT", "BOOK PROFIT", "BOOK FULL", "SQUARE OFF", "CLOSE POSITION")
    target_words: tuple[str, ...] = ("TGT", "TARGETS", "TARGET", "TP", "T1")
    sl_words: tuple[str, ...] = ("STOPLOSS", "STOP LOSS", "S/L", "SL", "STOP")
    above_words: tuple[str, ...] = ("ABOVE", "ABV", "CROSSING", "CROSS", "CROSSES")
    below_words: tuple[str, ...] = ("BELOW", "BLW", "BREAKS", "BREAKDOWN")
    entry_words: tuple[str, ...] = ("ENTRY", "BUY RANGE", "CMP", "NEAR", "AROUND", "@", "AT")

    def canonical_symbol(self, token: str) -> str:
        return self.symbol_aliases.get(token, token)


CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "MATIC", "DOT",
    "LTC", "LINK", "TRX", "SHIB", "PEPE", "SUI", "TON", "NEAR", "ARB", "OP",
}
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYNXT50"}

_KEYWORD_TOKENS = {
    "BUY", "SELL", "LONG", "SHORT", "BOUGHT", "SOLD", "ACCUMULATE", "EXIT",
    "TGT", "TARGET", "TARGETS", "TP", "SL", "STOPLOSS", "STOP", "LOSS",
    "ABOVE", "ABV", "BELOW", "BLW", "CROSSING", "CROSS", "CROSSES", "BREAKS",
    "ENTRY", "CMP", "NEAR", "AROUND", "AT", "FUT", "FUTURE", "FUTURES",
    "CE", "PE", "CALL", "PUT", "LOTS", "LOT", "QTY", "HERO", "ZERO", "SAFE",
    "TRADERS", "ONLY", "PAID", "JACKPOT", "SURE", "SHOT", "INTRADAY", "BTST",
    "WEEKLY", "MONTHLY", "EXPIRY", "T1", "T2", "T3", "AND", "TO", "RS",
}


def _normalize(text: str) -> str:
    text = text.upper()
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", " ", text)  # emoji
    text = text.replace("₹", " ").replace("**", " ").replace("__", " ")
    # commas act as list separators in signals ("TGT 170,190"); never merge numbers
    text = re.sub(r"[,;|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def looks_like_signal(text: str) -> bool:
    """Cheap heuristic gating the LLM fallback: needs a number and a trade-ish word."""
    t = _normalize(text)
    has_number = re.search(_NUM, t) is not None
    has_hint = re.search(
        r"\b(BUY|SELL|LONG|SHORT|TGT|TARGET|TP|SL|STOPLOSS|CE|PE|CALL|PUT|FUT|ENTRY|EXIT)\b", t
    ) is not None
    return has_number and has_hint


def _find_word(text: str, words: tuple[str, ...]) -> re.Match | None:
    for w in sorted(words, key=len, reverse=True):
        m = re.search(rf"(?<![A-Z0-9]){re.escape(w)}(?![A-Z])", text)
        if m:
            return m
    return None


def _extract_targets(text: str, pack: RulePack) -> tuple[list[float], str]:
    """Returns (targets, text-with-target-section-removed)."""
    words = "|".join(re.escape(w) for w in sorted(pack.target_words, key=len, reverse=True))
    m = re.search(
        rf"\b(?:{words})\s*[:=\-]?\s*({_NUM}(?:\s*(?:[/\-+]|AND|TO)?\s*{_NUM})*)",
        text,
    )
    if not m:
        return [], text
    nums = [float(n) for n in re.findall(_NUM, m.group(1))]
    cleaned = text[: m.start()] + " " + text[m.end():]
    return nums, cleaned


def _extract_sl(text: str, pack: RulePack) -> tuple[float | None, str]:
    words = "|".join(re.escape(w) for w in sorted(pack.sl_words, key=len, reverse=True))
    m = re.search(rf"\b(?:{words})\s*[:=\-]?\s*({_NUM})", text)
    if not m:
        return None, text
    cleaned = text[: m.start()] + " " + text[m.end():]
    return float(m.group(1)), cleaned


def _extract_option(text: str) -> tuple[float | None, OptionType | None, str]:
    m = re.search(rf"\b({_NUM})\s*(CE|PE|CALL|PUT)\b", text)
    if not m:
        return None, None, text
    ot = OptionType.CE if m.group(2) in ("CE", "CALL") else OptionType.PE
    cleaned = text[: m.start()] + " " + text[m.end():]
    return float(m.group(1)), ot, cleaned


def _extract_entry(text: str, pack: RulePack) -> tuple[EntryType, float | None, float | None, float | None, str]:
    """Returns (entry_type, price, range_low, range_high, cleaned_text)."""
    above = "|".join(re.escape(w) for w in pack.above_words)
    below = "|".join(re.escape(w) for w in pack.below_words)

    m = re.search(rf"\b(?:{above})\s*[:=]?\s*({_NUM})", text)
    if m:
        return EntryType.ABOVE, float(m.group(1)), None, None, text[: m.start()] + " " + text[m.end():]

    m = re.search(rf"\b(?:{below})\s*[:=]?\s*({_NUM})", text)
    if m:
        return EntryType.BELOW, float(m.group(1)), None, None, text[: m.start()] + " " + text[m.end():]

    entry_words = [w for w in pack.entry_words if w not in ("@",)]
    words = "|".join(re.escape(w) for w in sorted(entry_words, key=len, reverse=True))
    m = re.search(rf"(?:\B@|\b(?:{words})\b)\s*[:=]?\s*({_NUM})(?:\s*(?:-|TO)\s*({_NUM}))?", text)
    if m:
        cleaned = text[: m.start()] + " " + text[m.end():]
        if m.group(2):
            low, high = sorted((float(m.group(1)), float(m.group(2))))
            return EntryType.LIMIT, low, low, high, cleaned
        return EntryType.LIMIT, float(m.group(1)), None, None, cleaned

    return EntryType.MARKET, None, None, None, text


def _extract_symbol(text: str, pack: RulePack) -> str | None:
    # candidate tokens: alphabetic (with & or -) tokens that are not keywords
    for raw in re.findall(r"\b[A-Z][A-Z&\-]{1,19}\b", text):
        token = pack.canonical_symbol(raw)
        if raw not in _KEYWORD_TOKENS and token not in _KEYWORD_TOKENS:
            return token
    return None


def parse_with_rules(text: str, pack: RulePack | None = None) -> ParseResult:
    started = time.perf_counter()
    pack = pack or RulePack()

    def result(ok: bool, signal: ParsedSignal | None = None, error: str = "") -> ParseResult:
        return ParseResult(
            ok=ok,
            signal=signal,
            parser=f"rules:{pack.name}",
            error=error,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    t = _normalize(text)

    exited = _find_word(t, pack.exit_words) is not None
    buy = _find_word(t, pack.buy_words)
    sell = _find_word(t, pack.sell_words)
    if buy and sell:  # e.g. "BOOK PROFIT / SELL 50%" replies — ambiguous, refuse
        return result(False, error="ambiguous: both buy and sell words present")
    action = Action.BUY if buy else Action.SELL if sell else None

    targets, t = _extract_targets(t, pack)
    stop_loss, t = _extract_sl(t, pack)
    strike, option_type, t = _extract_option(t)
    entry_type, entry_price, range_low, range_high, t = _extract_entry(t, pack)
    is_future = re.search(r"\bFUT(?:URES?)?\b", t) is not None
    quantity = None
    qm = re.search(rf"\b({_NUM})\s*LOTS?\b", t)
    if qm:
        quantity = f"{qm.group(1)} lots"

    symbol = _extract_symbol(t, pack)
    if symbol is None:
        return result(False, error="no tradable symbol found")

    if exited and action is None:
        try:
            sig = ParsedSignal(
                kind=SignalKind.EXIT,
                action=Action.SELL,
                symbol=symbol,
                instrument=_infer_instrument(symbol, strike, is_future, pack),
                strike=strike,
                option_type=option_type,
                confidence=0.9,
                notes="exit signal",
            )
            return result(True, sig)
        except ValueError as exc:
            return result(False, error=str(exc))

    if action is None:
        return result(False, error="no action (buy/sell) found")

    instrument = _infer_instrument(symbol, strike, is_future, pack)
    confidence = 0.9
    if stop_loss is not None and targets:
        confidence = 0.95
    elif stop_loss is None and not targets and entry_type == EntryType.MARKET:
        confidence = 0.7  # bare "BUY X" — plausible but thin

    try:
        sig = ParsedSignal(
            action=action,
            symbol=symbol,
            instrument=instrument,
            strike=strike,
            option_type=option_type,
            entry_type=entry_type,
            entry_price=entry_price,
            entry_range_low=range_low,
            entry_range_high=range_high,
            targets=targets,
            stop_loss=stop_loss,
            quantity_hint=quantity,
            confidence=confidence,
        )
    except ValueError as exc:
        return result(False, error=f"inconsistent signal: {exc}")
    return result(True, sig)


def _infer_instrument(
    symbol: str, strike: float | None, is_future: bool, pack: RulePack
) -> InstrumentKind:
    base = re.sub(r"(USDT?|INR|PERP)$", "", symbol)
    if base in CRYPTO_BASES or pack.default_instrument in (
        InstrumentKind.CRYPTO_PERP,
        InstrumentKind.CRYPTO_SPOT,
    ):
        return InstrumentKind.CRYPTO_PERP
    if strike is not None:
        return InstrumentKind.OPTION
    if is_future or symbol in INDEX_SYMBOLS:
        return InstrumentKind.FUTURE
    return pack.default_instrument


# built-in packs, extensible per channel via Channel.config["rule_pack_overrides"]
BUILTIN_PACKS: dict[str, RulePack] = {
    "generic": RulePack(name="generic"),
    "crypto": RulePack(name="crypto", default_instrument=InstrumentKind.CRYPTO_PERP),
    "index-options": RulePack(
        name="index-options",
        symbol_aliases={"BNF": "BANKNIFTY", "NF": "NIFTY", "FN": "FINNIFTY"},
    ),
}


def get_pack(name: str, overrides: dict | None = None) -> RulePack:
    pack = BUILTIN_PACKS.get(name, BUILTIN_PACKS["generic"])
    if overrides:
        aliases = {**pack.symbol_aliases, **overrides.get("symbol_aliases", {})}
        pack = RulePack(
            name=pack.name,
            default_instrument=pack.default_instrument,
            symbol_aliases=aliases,
        )
    return pack
