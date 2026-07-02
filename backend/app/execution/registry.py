"""Adapter registry: builds broker adapters from dashboard settings and caches them."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_setting
from app.execution.base_adapter import BrokerAdapter, BrokerError
from app.execution.delta.adapter import TESTNET_WS_URL, WS_URL, DeltaAdapter
from app.execution.delta.client import TESTNET_URL
from app.execution.dhan.adapter import DhanAdapter
from app.execution.paper.adapter import PaperAdapter

_cache: dict[str, BrokerAdapter] = {}


def get_paper() -> PaperAdapter:
    if "paper" not in _cache:
        _cache["paper"] = PaperAdapter()
    return _cache["paper"]  # type: ignore[return-value]


async def get_adapter(session: AsyncSession, broker: str) -> BrokerAdapter:
    if broker == "paper":
        return get_paper()
    if broker in _cache:
        return _cache[broker]

    if broker == "dhan":
        cfg = await get_setting(session, "dhan", {}) or {}
        if not cfg.get("client_id") or not cfg.get("access_token"):
            raise BrokerError("Dhan is not configured (Settings → Brokers)")
        _cache["dhan"] = DhanAdapter(cfg["client_id"], cfg["access_token"])
    elif broker == "delta":
        cfg = await get_setting(session, "delta", {}) or {}
        if not cfg.get("api_key") or not cfg.get("api_secret"):
            raise BrokerError("Delta Exchange is not configured (Settings → Brokers)")
        testnet = bool(cfg.get("testnet"))
        _cache["delta"] = DeltaAdapter(
            cfg["api_key"], cfg["api_secret"],
            ws_url=TESTNET_WS_URL if testnet else WS_URL,
            base_url=TESTNET_URL if testnet else None,
        )
    else:
        raise BrokerError(f"unknown broker {broker!r}")
    return _cache[broker]


async def invalidate(broker: str | None = None) -> None:
    """Drop cached adapters (after credential changes)."""
    names = [broker] if broker else list(_cache)
    for name in names:
        adapter = _cache.pop(name, None)
        if adapter is not None:
            await adapter.close()


def active_adapters() -> dict[str, BrokerAdapter]:
    return dict(_cache)
