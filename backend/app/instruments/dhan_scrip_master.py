"""Dhan scrip-master sync: symbol → securityId resolution data.

Dhan publishes a daily CSV of every tradable instrument. We ingest the
segments this system trades (NSE/BSE F&O + NSE equity) into the
`instruments` table. Column names differ between the compact and detailed
master files, so lookups go through tolerant aliases.
"""
import csv
import io
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Instrument

log = get_logger(__name__)

SCRIP_MASTER_URL = "https://images.dhan.co/api-scrip/api-scrip-master.csv"

_ALIASES = {
    "exchange": ["SEM_EXM_EXCH_ID", "EXCH_ID"],
    "segment": ["SEM_SEGMENT", "SEGMENT"],
    "security_id": ["SEM_SMST_SECURITY_ID", "SECURITY_ID"],
    "instrument": ["SEM_INSTRUMENT_NAME", "INSTRUMENT", "INSTRUMENT_TYPE"],
    "expiry": ["SEM_EXPIRY_DATE", "SM_EXPIRY_DATE", "EXPIRY_DATE"],
    "strike": ["SEM_STRIKE_PRICE", "STRIKE_PRICE"],
    "option_type": ["SEM_OPTION_TYPE", "OPTION_TYPE"],
    "tick_size": ["SEM_TICK_SIZE", "TICK_SIZE"],
    "trading_symbol": ["SEM_TRADING_SYMBOL", "SYMBOL_NAME", "TRADING_SYMBOL"],
    "lot_size": ["SEM_LOT_UNITS", "LOT_SIZE", "LOT_UNITS"],
    "display_name": ["SEM_CUSTOM_SYMBOL", "DISPLAY_NAME", "SM_SYMBOL_NAME"],
    "underlying": ["SM_SYMBOL_NAME", "UNDERLYING_SYMBOL", "SEM_TRADING_SYMBOL"],
    "series": ["SEM_SERIES", "SERIES"],
}

# instrument codes we ingest → normalized type
_TYPE_MAP = {
    "OPTIDX": "OPT", "OPTSTK": "OPT", "OPTCUR": None, "OPTFUT": None,
    "FUTIDX": "FUT", "FUTSTK": "FUT", "FUTCUR": None, "FUTCOM": None,
    "EQUITY": "EQ", "EQ": "EQ",
    "INDEX": None,
}


def _get(row: dict[str, Any], field: str) -> str:
    for alias in _ALIASES[field]:
        v = row.get(alias)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _norm_expiry(value: str) -> str:
    """Normalize expiry to YYYY-MM-DD (string-sortable)."""
    if not value:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.split(".")[0], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value


def parse_scrip_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """CSV row → instruments-table dict, or None if out of scope."""
    exchange = _get(row, "exchange").upper()
    if exchange not in ("NSE", "BSE"):
        return None
    inst_code = _get(row, "instrument").upper()
    norm_type = _TYPE_MAP.get(inst_code)
    if norm_type is None:
        return None
    if norm_type == "EQ" and _get(row, "series").upper() not in ("", "EQ", "A", "B"):
        return None

    segment = f"{exchange}_FNO" if norm_type in ("OPT", "FUT") else f"{exchange}_EQ"
    option_type = _get(row, "option_type").upper()
    if norm_type == "OPT":
        if option_type not in ("CE", "PE"):
            return None
        norm_type = option_type  # store CE / PE directly

    trading_symbol = _get(row, "trading_symbol").upper()
    # underlying for F&O: strip the derivative suffix ("NIFTY-Jul2026-25000-CE" or "NIFTY 25000 CALL")
    underlying = trading_symbol.split("-")[0].split(" ")[0]

    strike_raw = _get(row, "strike")
    try:
        strike = float(strike_raw) if strike_raw and float(strike_raw) > 0 else None
    except ValueError:
        strike = None

    return {
        "broker": "dhan",
        "security_id": _get(row, "security_id"),
        "exchange_segment": segment,
        "symbol": trading_symbol,
        "display_name": _get(row, "display_name"),
        "instrument_type": norm_type,
        "underlying": underlying,
        "expiry": _norm_expiry(_get(row, "expiry")),
        "strike": strike,
        "lot_size": float(_get(row, "lot_size") or 1),
        "tick_size": float(_get(row, "tick_size") or 0.05),
        "meta": {},
        "updated_at": datetime.now(UTC),
    }


async def sync_dhan_instruments(session: AsyncSession, url: str = SCRIP_MASTER_URL) -> int:
    """Download the scrip master and replace all Dhan rows. Returns row count."""
    async with httpx.AsyncClient(timeout=120.0) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        text = resp.text

    rows: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(text)):
        parsed = parse_scrip_row(row)
        if parsed and parsed["security_id"]:
            rows.append(parsed)

    await session.execute(delete(Instrument).where(Instrument.broker == "dhan"))
    for i in range(0, len(rows), 2000):
        await session.execute(Instrument.__table__.insert(), rows[i : i + 2000])
    await session.commit()
    log.info("dhan_scrip_master_synced", instruments=len(rows))
    return len(rows)
