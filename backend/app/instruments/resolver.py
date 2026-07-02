"""Resolve a ParsedSignal to a concrete tradable instrument."""
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Instrument
from app.parsing.schema import InstrumentKind, ParsedSignal


class ResolutionError(Exception):
    pass


_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"


def _expiry_from_hint(hint: str | None) -> str | None:
    """Turn '10JUL' / '10 JUL 2026' style hints into a YYYY-MM-DD prefix filter."""
    if not hint:
        return None
    m = re.search(rf"(\d{{1,2}})\s*({_MONTHS})\s*(\d{{4}})?", hint.upper())
    if not m:
        return None
    year = int(m.group(3) or datetime.now(UTC).year)
    month = datetime.strptime(m.group(2), "%b").month
    return f"{year:04d}-{month:02d}-{int(m.group(1)):02d}"


async def resolve(session: AsyncSession, signal: ParsedSignal, broker: str) -> Instrument:
    symbol = signal.symbol.upper()
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    if broker == "delta" or signal.instrument in (
        InstrumentKind.CRYPTO_PERP, InstrumentKind.CRYPTO_SPOT,
    ):
        candidates = [symbol]
        if not symbol.endswith(("USD", "USDT")):
            candidates += [f"{symbol}USD", f"{symbol}USDT"]
        for cand in candidates:
            inst = (
                await session.execute(
                    select(Instrument).where(
                        Instrument.broker == "delta", Instrument.symbol == cand
                    )
                )
            ).scalar_one_or_none()
            if inst:
                return inst
        raise ResolutionError(f"no Delta product for {symbol} (run product sync?)")

    if signal.instrument == InstrumentKind.OPTION:
        exact = _expiry_from_hint(signal.expiry_hint)
        q = (
            select(Instrument)
            .where(
                Instrument.broker == "dhan",
                Instrument.underlying == symbol,
                Instrument.instrument_type == signal.option_type.value,
                Instrument.strike == signal.strike,
                Instrument.expiry >= (exact or today),
            )
            .order_by(Instrument.expiry)
        )
        if exact:
            q = q.where(Instrument.expiry == exact)
        inst = (await session.execute(q.limit(1))).scalar_one_or_none()
        if inst is None:
            raise ResolutionError(
                f"no {symbol} {signal.strike:g} {signal.option_type.value} contract found"
                f"{' for expiry ' + exact if exact else ''} (scrip master stale?)"
            )
        return inst

    if signal.instrument == InstrumentKind.FUTURE:
        inst = (
            await session.execute(
                select(Instrument)
                .where(
                    Instrument.broker == "dhan",
                    Instrument.underlying == symbol,
                    Instrument.instrument_type == "FUT",
                    Instrument.expiry >= today,
                )
                .order_by(Instrument.expiry)
                .limit(1)
            )
        ).scalar_one_or_none()
        if inst is None:
            raise ResolutionError(f"no {symbol} future found")
        return inst

    # equity
    inst = (
        await session.execute(
            select(Instrument).where(
                Instrument.broker == "dhan",
                Instrument.instrument_type == "EQ",
                Instrument.symbol == symbol,
            )
        )
    ).scalar_one_or_none()
    if inst is None:
        raise ResolutionError(f"no equity instrument for {symbol}")
    return inst
