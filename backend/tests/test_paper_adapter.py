"""Paper broker: full bracket lifecycle on simulated ticks."""
import asyncio

import pytest

from app.execution.base_adapter import BracketOrderRequest, BrokerError, BrokerOrderStatus
from app.execution.paper.adapter import PaperAdapter


def req(**kw):
    base = dict(
        correlation_id="sig-1", security_id="43492", symbol="NIFTY 25000 CE",
        side="BUY", qty=75, order_type="STOP_LIMIT", trigger_price=150,
        target_price=170, stop_loss_price=130,
    )
    base.update(kw)
    return BracketOrderRequest(**base)


async def drain(adapter, n):
    out = []
    stream = adapter.stream_order_updates()
    for _ in range(n):
        out.append(await asyncio.wait_for(anext(stream), 1))
    return out


async def test_market_entry_fills_immediately():
    a = PaperAdapter()
    placed = await a.place_bracket(req(order_type="MARKET", meta={"ltp": 151.5}))
    assert placed.status == BrokerOrderStatus.FILLED
    updates = await drain(a, 2)
    assert updates[1].status == BrokerOrderStatus.FILLED
    assert updates[1].avg_price == 151.5


async def test_stop_entry_then_target_hit():
    a = PaperAdapter()
    placed = await a.place_bracket(req())
    assert placed.status == BrokerOrderStatus.PENDING

    a.on_tick("NIFTY 25000 CE", 149)   # below trigger: nothing
    a.on_tick("NIFTY 25000 CE", 151)   # crosses above 150: entry fills
    a.on_tick("NIFTY 25000 CE", 171)   # target 170 hit

    updates = await drain(a, 3)
    assert [u.leg for u in updates] == ["entry", "entry", "target"]
    assert updates[1].avg_price == 150
    assert updates[2].avg_price == 170
    assert (await a.get_positions()) == []  # closed


async def test_stop_loss_closes_position():
    a = PaperAdapter()
    await a.place_bracket(req())
    a.on_tick("NIFTY 25000 CE", 152)
    a.on_tick("NIFTY 25000 CE", 129)
    updates = await drain(a, 3)
    assert updates[2].leg == "stop_loss"
    assert updates[2].avg_price == 130


async def test_sell_side_bracket():
    a = PaperAdapter()
    await a.place_bracket(req(side="SELL", order_type="LIMIT", price=150,
                              target_price=130, stop_loss_price=165))
    a.on_tick("NIFTY 25000 CE", 152)   # >= limit for SELL: entry fills
    a.on_tick("NIFTY 25000 CE", 128)   # target for short
    updates = await drain(a, 3)
    assert updates[2].leg == "target"
    assert updates[2].avg_price == 130


async def test_open_position_visible_until_closed():
    a = PaperAdapter()
    await a.place_bracket(req(order_type="MARKET", meta={"ltp": 150}))
    positions = await a.get_positions()
    assert len(positions) == 1
    assert positions[0].qty == 75


async def test_modify_bracket_updates_levels():
    a = PaperAdapter()
    placed = await a.place_bracket(req(order_type="MARKET", meta={"ltp": 150}))
    await a.modify_bracket(placed.broker_order_id, stop_loss_price=140)
    a.on_tick("NIFTY 25000 CE", 139)
    updates = await drain(a, 3)
    assert updates[2].leg == "stop_loss"
    assert updates[2].avg_price == 140


async def test_cancel_pending_order():
    a = PaperAdapter()
    placed = await a.place_bracket(req())
    await a.cancel(placed.broker_order_id)
    updates = await drain(a, 2)
    assert updates[1].status == BrokerOrderStatus.CANCELLED


async def test_zero_qty_rejected():
    a = PaperAdapter()
    with pytest.raises(BrokerError):
        await a.place_bracket(req(qty=0))
