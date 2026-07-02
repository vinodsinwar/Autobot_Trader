"""End-to-end engine tests: message → parse → risk → order → fills → P&L,
all over the paper broker."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import (
    Channel,
    Order,
    Position,
    PositionStatus,
    Signal,
    SignalState,
)
from app.db.session import set_setting
from app.execution import registry, router, tracker
from app.execution.base_adapter import OrderUpdate
from app.ingestion.telegram_listener import handle_incoming
from app.risk import engine as risk_engine
from app.risk.sizing import SizingError, size_order

NOW = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def clean_registry():
    await registry.invalidate()
    yield
    await registry.invalidate()


@pytest.fixture
async def channel(db):
    async with db.session_factory()() as session:
        ch = Channel(
            tg_chat_id=-1001, title="Signals", enabled=True,
            broker="paper", execution_mode="auto", llm_fallback=False,
            sizing={"mode": "units", "value": 75},
        )
        session.add(ch)
        await session.commit()
        await session.refresh(ch)
        return ch


async def drain_paper_updates(db, n=10):
    """Apply up to n pending paper-adapter updates through the tracker."""
    paper = registry.get_paper()
    applied = 0
    while not paper._updates.empty() and applied < n:
        update = paper._updates.get_nowait()
        async with db.session_factory()() as session:
            await tracker.apply_update(session, update)
            await session.commit()
        applied += 1
    return applied


async def send(db, channel, text, msg_id=1, is_edit=False):
    return await handle_incoming(
        channel.tg_chat_id, msg_id, text, NOW, is_edit, on_message=router.on_telegram_message
    )


class TestFullPipeline:
    async def test_signal_to_open_position_to_target(self, db, channel):
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170/190 SL 130")

        async with db.session_factory()() as session:
            signal = (await session.execute(select(Signal))).scalar_one()
            order = (await session.execute(select(Order))).scalar_one()
        assert signal.state == SignalState.EXECUTING.value
        assert order.broker == "paper"
        assert order.qty == 75
        assert order.trigger_price == 150

        paper = registry.get_paper()
        await drain_paper_updates(db)          # placement PENDING update
        paper.on_tick("NIFTY 25000 CE", 151)   # entry fills
        await drain_paper_updates(db)

        async with db.session_factory()() as session:
            signal = (await session.execute(select(Signal))).scalar_one()
            pos = (await session.execute(select(Position))).scalar_one()
        assert signal.state == SignalState.OPEN.value
        assert pos.status == PositionStatus.OPEN.value
        assert pos.avg_entry_price == 150

        paper.on_tick("NIFTY 25000 CE", 171)   # target hits
        await drain_paper_updates(db)

        async with db.session_factory()() as session:
            signal = (await session.execute(select(Signal))).scalar_one()
            pos = (await session.execute(select(Position))).scalar_one()
        assert signal.state == SignalState.CLOSED.value
        assert pos.status == PositionStatus.CLOSED.value
        assert pos.realized_pnl == pytest.approx((170 - 150) * 75)

    async def test_stop_loss_produces_negative_pnl(self, db, channel):
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130")
        paper = registry.get_paper()
        await drain_paper_updates(db)
        paper.on_tick("NIFTY 25000 CE", 151)
        paper.on_tick("NIFTY 25000 CE", 129)
        await drain_paper_updates(db)
        async with db.session_factory()() as session:
            pos = (await session.execute(select(Position))).scalar_one()
        assert pos.realized_pnl == pytest.approx((130 - 150) * 75)

    async def test_chatter_creates_nothing(self, db, channel):
        await send(db, channel, "good morning traders! market bullish today")
        async with db.session_factory()() as session:
            assert (await session.execute(select(Signal))).scalars().all() == []

    async def test_duplicate_signal_rejected(self, db, channel):
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130", msg_id=1)
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130", msg_id=2)
        async with db.session_factory()() as session:
            signals = (await session.execute(select(Signal).order_by(Signal.id))).scalars().all()
        assert signals[0].state == SignalState.EXECUTING.value
        assert signals[1].state == SignalState.RISK_REJECTED.value
        assert "duplicate" in signals[1].error


class TestManualApproval:
    async def test_manual_mode_waits_then_executes_on_approve(self, db, channel):
        async with db.session_factory()() as session:
            ch = await session.get(Channel, channel.id)
            ch.execution_mode = "manual"
            await session.commit()

        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130")
        async with db.session_factory()() as session:
            signal = (await session.execute(select(Signal))).scalar_one()
            assert signal.state == SignalState.AWAITING_APPROVAL.value
            await router.approve_signal(session, signal.id)

        async with db.session_factory()() as session:
            signal = (await session.execute(select(Signal))).scalar_one()
            order = (await session.execute(select(Order))).scalar_one_or_none()
        assert signal.state == SignalState.EXECUTING.value
        assert order is not None
        assert order.qty == 75  # sizing preserved through the approval hop

    async def test_reject_flow(self, db, channel):
        async with db.session_factory()() as session:
            ch = await session.get(Channel, channel.id)
            ch.execution_mode = "manual"
            await session.commit()
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130")
        async with db.session_factory()() as session:
            signal = (await session.execute(select(Signal))).scalar_one()
            await router.reject_signal(session, signal.id)
        async with db.session_factory()() as session:
            signal = (await session.execute(select(Signal))).scalar_one()
            assert signal.state == SignalState.APPROVAL_REJECTED.value
            assert (await session.execute(select(Order))).scalar_one_or_none() is None


class TestEditModification:
    async def test_sl_edit_modifies_live_bracket(self, db, channel):
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130", msg_id=9)
        paper = registry.get_paper()
        await drain_paper_updates(db)
        paper.on_tick("NIFTY 25000 CE", 151)
        await drain_paper_updates(db)

        # provider edits the SL from 130 → 140
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 140",
                   msg_id=9, is_edit=True)

        async with db.session_factory()() as session:
            signals = (await session.execute(select(Signal))).scalars().all()
        assert len(signals) == 1  # modification, not a new signal

        paper.on_tick("NIFTY 25000 CE", 139)  # old SL 130 wouldn't trigger; new 140 does
        await drain_paper_updates(db)
        async with db.session_factory()() as session:
            pos = (await session.execute(select(Position))).scalar_one()
        assert pos.status == PositionStatus.CLOSED.value
        assert pos.exit_price == 140


class TestExitSignal:
    async def test_exit_closes_open_position(self, db, channel):
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 190 SL 130", msg_id=1)
        paper = registry.get_paper()
        await drain_paper_updates(db)
        paper.on_tick("NIFTY 25000 CE", 155)
        await drain_paper_updates(db)

        await send(db, channel, "EXIT NIFTY 25000 CE NOW", msg_id=2)
        # exit is a MARKET paper order; give it the current price and process
        paper.on_tick("NIFTY 25000 CE", 160)
        await drain_paper_updates(db)

        async with db.session_factory()() as session:
            pos = (
                await session.execute(
                    select(Position).where(Position.status == PositionStatus.CLOSED.value)
                )
            ).scalars().all()
        assert len(pos) == 1


class TestRiskScenarios:
    async def test_kill_switch_blocks_everything(self, db, channel):
        async with db.session_factory()() as session:
            await set_setting(session, "risk", {"kill_switch": True})
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130")
        async with db.session_factory()() as session:
            signal = (await session.execute(select(Signal))).scalar_one()
        assert signal.state == SignalState.RISK_REJECTED.value
        assert "kill switch" in signal.error

    async def test_trading_hours_block(self, db):
        async with db.session_factory()() as session:
            cfg = await risk_engine.load_risk_settings(session)
            from app.parsing.rule_engine import parse_with_rules
            parsed = parse_with_rules("BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130").signal
            # 2026-07-02 20:00 IST = outside 09:15-15:25 window
            late = datetime(2026, 7, 2, 14, 30, tzinfo=UTC)  # 20:00 IST
            decision = await risk_engine.check(session, parsed, "dhan", now=late,
                                               settings_override=cfg)
        assert not decision.approved
        assert "trading hours" in decision.reason_text

    async def test_max_concurrent_positions(self, db, channel):
        async with db.session_factory()() as session:
            await set_setting(session, "risk", {"max_concurrent_positions": 1})
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130", msg_id=1)
        paper = registry.get_paper()
        await drain_paper_updates(db)
        paper.on_tick("NIFTY 25000 CE", 151)
        await drain_paper_updates(db)

        await send(db, channel, "BUY BANKNIFTY 51000 CE ABOVE 200 TGT 250 SL 170", msg_id=2)
        async with db.session_factory()() as session:
            signals = (await session.execute(select(Signal).order_by(Signal.id))).scalars().all()
        assert signals[1].state == SignalState.RISK_REJECTED.value
        assert "concurrent" in signals[1].error


class TestSizing:
    def _instrument(self, **kw):
        from app.db.models import Instrument
        base = dict(broker="dhan", security_id="1", symbol="NIFTY-CE",
                    instrument_type="CE", underlying="NIFTY", lot_size=75, tick_size=0.05)
        base.update(kw)
        return Instrument(**base)

    def _parsed(self, text="BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130"):
        from app.parsing.rule_engine import parse_with_rules
        return parse_with_rules(text).signal

    def test_lots_mode(self):
        sized = size_order(self._parsed(), self._instrument(), {"mode": "lots", "value": 2})
        assert sized.qty == 150

    def test_capital_mode_floors_to_lots(self):
        sized = size_order(self._parsed(), self._instrument(),
                           {"mode": "capital", "value": 25000})
        assert sized.qty == 150  # floor(25000 / (150*75))=2 lots

    def test_capital_cap_enforced(self):
        with pytest.raises(SizingError):
            size_order(self._parsed(), self._instrument(),
                       {"mode": "lots", "value": 10}, max_capital_per_trade=50000)

    def test_market_order_capital_sizing_refused(self):
        parsed = self._parsed("BUY NIFTY 25000 CE TGT 170 SL 130")
        parsed.entry_price = None
        with pytest.raises(SizingError):
            size_order(parsed, self._instrument(), {"mode": "capital", "value": 25000})


class TestReconciliation:
    async def test_missing_order_marked_error(self, db, channel):
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130")
        # simulate restart: paper adapter loses its in-memory book
        await registry.invalidate()
        await tracker.reconcile()
        async with db.session_factory()() as session:
            order = (await session.execute(select(Order))).scalar_one()
        assert order.status == "error"
        assert "reconciliation" in order.status_reason

    async def test_live_order_survives_reconcile(self, db, channel):
        await send(db, channel, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130")
        await tracker.reconcile()  # same adapter still has the order
        async with db.session_factory()() as session:
            order = (await session.execute(select(Order))).scalar_one()
        assert order.status in ("pending", "placed")


class TestUnmatchedUpdate:
    async def test_unknown_broker_order_id_ignored(self, db):
        async with db.session_factory()() as session:
            from app.execution.base_adapter import BrokerOrderStatus
            ok = await tracker.apply_update(session, OrderUpdate(
                broker="paper", broker_order_id="GHOST-1",
                status=BrokerOrderStatus.FILLED,
            ))
        assert ok is False
