"""Signal processor: message → parse → risk → (approval) → order.

This is the money path. Every branch persists its decision and reason to the
signals/events tables before anything else happens.
"""
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db import audit
from app.db.models import (
    Channel,
    ExecutionMode,
    Order,
    OrderLeg,
    OrderStatus,
    Position,
    PositionStatus,
    RawMessage,
    Signal,
    SignalSource,
    SignalState,
)
from app.db.session import get_setting, session_factory
from app.execution import registry
from app.execution.base_adapter import BracketOrderRequest, BrokerError
from app.instruments.resolver import ResolutionError, resolve
from app.parsing import pipeline
from app.parsing.rule_engine import looks_like_signal
from app.parsing.schema import EntryType, ParsedSignal, SignalKind
from app.risk import engine as risk_engine
from app.risk import sizing as sizing_mod

log = get_logger(__name__)


async def on_telegram_message(raw: RawMessage, channel: Channel, is_edit: bool) -> None:
    """Ingestion callback — runs the full pipeline in its own session."""
    async with session_factory()() as session:
        try:
            await process_message(session, raw, channel, is_edit)
        except Exception:
            log.exception("signal_pipeline_error", raw_message_id=raw.id)


async def process_message(
    session: AsyncSession, raw: RawMessage, channel: Channel, is_edit: bool
) -> Signal | None:
    llm_settings = await get_setting(session, "llm", {}) or {}
    result = await pipeline.parse_message(
        raw.text,
        rule_pack=channel.rule_pack,
        rule_overrides=(channel.config or {}).get("rule_pack_overrides"),
        llm_fallback=channel.llm_fallback,
        llm_settings=llm_settings,
        channel_context=f"Telegram channel: {channel.title}",
    )

    if not result.ok:
        if not looks_like_signal(raw.text):
            return None  # chatter — not worth a row
        signal = Signal(
            source=SignalSource.TELEGRAM.value,
            state=SignalState.PARSE_FAILED.value,
            raw_message_id=raw.id,
            parser=result.parser,
            error=result.error,
            broker=channel.broker,
        )
        session.add(signal)
        await session.flush()
        await audit.record(session, "signal", "parse_failed",
                           {"error": result.error, "text": raw.text[:200]}, signal.id)
        return signal

    parsed = result.signal
    assert parsed is not None

    if is_edit:
        handled = await _apply_edit(session, raw, parsed)
        if handled:
            return None

    signal = Signal(
        source=SignalSource.TELEGRAM.value,
        state=SignalState.PARSED.value,
        raw_message_id=raw.id,
        parsed=parsed.model_dump(mode="json"),
        parser=result.parser,
        confidence=parsed.confidence,
        broker=channel.broker,
        dedup_hash=parsed.dedup_hash(),
    )
    session.add(signal)
    await session.flush()
    await audit.record(session, "signal", "parsed",
                       {"summary": parsed.human_summary(), "parser": result.parser,
                        "latency_ms": round(result.latency_ms, 1)}, signal.id)

    if parsed.kind == SignalKind.EXIT:
        await _handle_exit(session, signal, parsed)
        return signal

    return await gate_and_execute(session, signal, parsed, channel.execution_mode, channel.sizing)


async def gate_and_execute(
    session: AsyncSession,
    signal: Signal,
    parsed: ParsedSignal,
    execution_mode: str,
    sizing: dict[str, Any] | None,
) -> Signal:
    """Risk gate + approval gate + execution. Shared by Telegram and YouTube paths."""
    risk_cfg = await risk_engine.load_risk_settings(session)
    broker = signal.broker if risk_cfg.get("live_trading") else "paper"

    decision = await risk_engine.check(session, parsed, signal.broker,
                                       exclude_signal_id=signal.id)
    if not decision.approved:
        signal.state = SignalState.RISK_REJECTED.value
        signal.error = decision.reason_text
        await audit.record(session, "risk", "rejected", {"reasons": decision.reasons}, signal.id)
        return signal

    signal.state = SignalState.RISK_APPROVED.value
    await audit.record(session, "risk", "approved", {"broker": broker}, signal.id)

    if execution_mode == ExecutionMode.MANUAL.value:
        signal.state = SignalState.AWAITING_APPROVAL.value
        signal.parsed = {**signal.parsed, "_sizing": sizing or {}}
        await session.commit()
        await audit.record(session, "signal", "awaiting_approval",
                           {"summary": parsed.human_summary()}, signal.id)
        return signal

    await execute_signal(session, signal, parsed, broker, sizing)
    return signal


async def execute_signal(
    session: AsyncSession,
    signal: Signal,
    parsed: ParsedSignal,
    broker: str,
    sizing: dict[str, Any] | None,
) -> Order | None:
    risk_cfg = await risk_engine.load_risk_settings(session)
    try:
        try:
            instrument = await resolve(session, parsed, "delta" if broker == "delta" else "dhan")
        except ResolutionError:
            if broker != "paper":
                raise
            instrument = _pseudo_instrument(parsed)  # paper trades anything

        sized = sizing_mod.size_order(
            parsed, instrument, sizing, risk_cfg.get("max_capital_per_trade")
        )
        adapter = await registry.get_adapter(session, broker)
        req = _build_request(signal, parsed, instrument, sized.qty)
        placed = await adapter.place_bracket(req)
    except (BrokerError, ResolutionError, sizing_mod.SizingError) as exc:
        signal.state = SignalState.ERROR.value
        signal.error = str(exc)
        await audit.record(session, "order", "placement_failed", {"error": str(exc)}, signal.id)
        return None

    order = Order(
        signal_id=signal.id,
        broker=broker,
        correlation_id=req.correlation_id,
        broker_order_id=placed.broker_order_id,
        leg=OrderLeg.ENTRY.value,
        symbol=instrument.symbol,
        security_id=str(instrument.security_id),
        side=req.side,
        qty=req.qty,
        price=req.price,
        trigger_price=req.trigger_price,
        order_type=req.order_type,
        status=placed.status.value,
        raw={"request": _req_summary(req), "response": placed.raw,
             "all_targets": parsed.targets},
    )
    session.add(order)
    signal.state = SignalState.EXECUTING.value
    await session.flush()
    await audit.record(session, "order", "placed",
                       {"broker": broker, "order_id": placed.broker_order_id,
                        "symbol": instrument.symbol, "qty": req.qty,
                        "summary": parsed.human_summary()}, signal.id)
    return order


def _pseudo_instrument(parsed: ParsedSignal):
    from app.db.models import Instrument

    label = parsed.symbol.upper()
    if parsed.strike:
        label = f"{label} {parsed.strike:g} {parsed.option_type.value if parsed.option_type else ''}".strip()
    return Instrument(
        broker="paper", security_id=f"PAPER:{label}", symbol=label,
        instrument_type=parsed.option_type.value if parsed.option_type else "EQ",
        underlying=parsed.symbol.upper(), lot_size=1, tick_size=0.05,
    )


def _build_request(
    signal: Signal, parsed: ParsedSignal, instrument, qty: float
) -> BracketOrderRequest:
    entry_map = {
        EntryType.MARKET: ("MARKET", None, None),
        EntryType.LIMIT: ("LIMIT", parsed.entry_price, None),
        EntryType.ABOVE: ("STOP_LIMIT", parsed.entry_price, parsed.entry_price),
        EntryType.BELOW: ("STOP_LIMIT", parsed.entry_price, parsed.entry_price),
    }
    order_type, price, trigger = entry_map[parsed.entry_type]
    return BracketOrderRequest(
        correlation_id=f"ab-{signal.id}-{uuid.uuid4().hex[:8]}",
        security_id=str(instrument.security_id),
        symbol=instrument.symbol,
        side=parsed.action.value,
        qty=qty,
        order_type=order_type,
        price=price,
        trigger_price=trigger,
        target_price=parsed.targets[0] if parsed.targets else None,
        stop_loss_price=parsed.stop_loss,
        exchange_segment=instrument.exchange_segment,
        product_type="INTRADAY",
    )


def _req_summary(req: BracketOrderRequest) -> dict[str, Any]:
    return {
        "side": req.side, "qty": req.qty, "order_type": req.order_type,
        "price": req.price, "trigger": req.trigger_price,
        "target": req.target_price, "stop_loss": req.stop_loss_price,
    }


async def _apply_edit(session: AsyncSession, raw: RawMessage, parsed: ParsedSignal) -> bool:
    """Provider edited an active signal → treat changed SL/target as a modification.

    Returns True if the edit was applied to a live order (no new signal needed).
    """
    prior = (
        await session.execute(
            select(Signal)
            .join(RawMessage, Signal.raw_message_id == RawMessage.id)
            .where(
                RawMessage.channel_id == raw.channel_id,
                RawMessage.tg_message_id == raw.tg_message_id,
                Signal.state.in_([SignalState.EXECUTING.value, SignalState.OPEN.value]),
            )
            .order_by(Signal.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior is None:
        return False

    old = ParsedSignal.model_validate(prior.parsed)
    new_sl = parsed.stop_loss if parsed.stop_loss != old.stop_loss else None
    new_target = (
        parsed.targets[0]
        if parsed.targets and parsed.targets[:1] != old.targets[:1]
        else None
    )
    if new_sl is None and new_target is None:
        return True  # edit didn't change trade levels; original signal stands

    order = (
        await session.execute(
            select(Order).where(
                Order.signal_id == prior.id, Order.leg == OrderLeg.ENTRY.value
            ).order_by(Order.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if order is None:
        return False

    try:
        adapter = await registry.get_adapter(session, order.broker)
        await adapter.modify_bracket(
            order.broker_order_id, target_price=new_target, stop_loss_price=new_sl
        )
    except BrokerError as exc:
        await audit.record(session, "order", "modify_failed", {"error": str(exc)}, prior.id)
        return True

    prior.parsed = parsed.model_dump(mode="json")
    await audit.record(session, "order", "modified",
                       {"new_target": new_target, "new_stop_loss": new_sl}, prior.id)
    return True


async def _handle_exit(session: AsyncSession, signal: Signal, parsed: ParsedSignal) -> None:
    """EXIT signal: close the matching open position at market."""
    symbol = parsed.symbol.upper()
    q = select(Position).where(Position.status == PositionStatus.OPEN.value)
    positions = (await session.execute(q)).scalars().all()
    matched = [p for p in positions if symbol in p.symbol.upper()]
    if parsed.strike:
        matched = [p for p in matched if f"{parsed.strike:g}" in p.symbol]
    if not matched:
        signal.state = SignalState.CANCELLED.value
        signal.error = f"exit signal but no open position for {symbol}"
        await audit.record(session, "signal", "exit_no_position", {"symbol": symbol}, signal.id)
        return

    for pos in matched:
        entry_order = (
            await session.execute(
                select(Order).where(
                    Order.signal_id == pos.signal_id, Order.leg == OrderLeg.ENTRY.value
                )
            )
        ).scalar_one_or_none()
        broker = pos.broker
        try:
            adapter = await registry.get_adapter(session, broker)
            if entry_order and entry_order.status in (
                OrderStatus.PENDING.value, OrderStatus.PLACED.value
            ):
                await adapter.cancel(entry_order.broker_order_id)
                continue
            close_req = BracketOrderRequest(
                correlation_id=f"ab-exit-{signal.id}-{uuid.uuid4().hex[:8]}",
                security_id=pos.security_id,
                symbol=pos.symbol,
                side="SELL" if pos.qty > 0 else "BUY",
                qty=abs(pos.qty),
                order_type="MARKET",
                exchange_segment=(entry_order.raw or {}).get("request", {}).get("exchange_segment", "")
                if entry_order else "",
            )
            placed = await adapter.place_bracket(close_req)
            session.add(Order(
                signal_id=signal.id, broker=broker,
                correlation_id=close_req.correlation_id,
                broker_order_id=placed.broker_order_id,
                leg=OrderLeg.ENTRY.value, symbol=pos.symbol,
                security_id=pos.security_id, side=close_req.side,
                qty=close_req.qty, order_type="MARKET",
                status=placed.status.value,
                raw={"exit_for_position": pos.id},
            ))
            signal.state = SignalState.EXECUTING.value
            await audit.record(session, "order", "exit_placed",
                               {"position_id": pos.id, "symbol": pos.symbol}, signal.id)
        except BrokerError as exc:
            signal.state = SignalState.ERROR.value
            signal.error = str(exc)
            await audit.record(session, "order", "exit_failed", {"error": str(exc)}, signal.id)


async def approve_signal(session: AsyncSession, signal_id: int) -> Signal:
    """Dashboard one-click approval of an AWAITING_APPROVAL signal."""
    signal = await session.get(Signal, signal_id)
    if signal is None or signal.state != SignalState.AWAITING_APPROVAL.value:
        raise ValueError(f"signal {signal_id} is not awaiting approval")
    parsed_data = dict(signal.parsed)
    sizing = parsed_data.pop("_sizing", {})
    parsed = ParsedSignal.model_validate(parsed_data)
    risk_cfg = await risk_engine.load_risk_settings(session)
    broker = signal.broker if risk_cfg.get("live_trading") else "paper"
    await audit.record(session, "signal", "manually_approved", {}, signal.id)
    await execute_signal(session, signal, parsed, broker, sizing)
    await session.commit()
    return signal


async def reject_signal(session: AsyncSession, signal_id: int) -> Signal:
    signal = await session.get(Signal, signal_id)
    if signal is None or signal.state != SignalState.AWAITING_APPROVAL.value:
        raise ValueError(f"signal {signal_id} is not awaiting approval")
    signal.state = SignalState.APPROVAL_REJECTED.value
    await audit.record(session, "signal", "manually_rejected", {}, signal.id)
    await session.commit()
    return signal
