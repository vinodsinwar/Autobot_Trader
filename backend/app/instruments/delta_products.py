"""Delta Exchange India product sync (perpetual futures)."""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Instrument
from app.execution.delta.client import DeltaClient

log = get_logger(__name__)


def parse_product(p: dict[str, Any]) -> dict[str, Any] | None:
    if (p.get("state") or "live") != "live":
        return None
    symbol = (p.get("symbol") or "").upper()
    if not symbol or not p.get("id"):
        return None
    underlying = (p.get("underlying_asset") or {}).get("symbol", "").upper()
    return {
        "broker": "delta",
        "security_id": str(p["id"]),
        "exchange_segment": "DELTA",
        "symbol": symbol,
        "display_name": p.get("description", symbol),
        "instrument_type": "PERP",
        "underlying": underlying or symbol.replace("USD", ""),
        "expiry": "",
        "strike": None,
        "lot_size": float(p.get("contract_value") or 1),
        "tick_size": float(p.get("tick_size") or 0.5),
        "meta": {"contract_unit": p.get("contract_unit_currency", "")},
        "updated_at": datetime.now(UTC),
    }


async def sync_delta_instruments(session: AsyncSession, client: DeltaClient) -> int:
    products = await client.get_products("perpetual_futures")
    rows = [r for p in products if (r := parse_product(p)) is not None]
    await session.execute(delete(Instrument).where(Instrument.broker == "delta"))
    if rows:
        await session.execute(Instrument.__table__.insert(), rows)
    await session.commit()
    log.info("delta_products_synced", instruments=len(rows))
    return len(rows)
