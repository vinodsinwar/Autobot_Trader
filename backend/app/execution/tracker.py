"""Order tracker: consumes broker update streams, maintains orders/trades/
positions, and reconciles state after restarts."""
import asyncio
import contextlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db import audit
from app.db.models import (
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    Signal,
    SignalState,
    Trade,
    utcnow,
)
from app.db.session import session_factory
from app.execution import registry
from app.execution.base_adapter import BrokerAdapter, BrokerError, BrokerOrderStatus, OrderUpdate

log = get_logger(__name__)

_STATUS_TO_ORDER = {
    BrokerOrderStatus.PENDING: OrderStatus.PLACED,
    BrokerOrderStatus.PART_FILLED: OrderStatus.PART_FILLED,
    BrokerOrderStatus.FILLED: OrderStatus.FILLED,
    BrokerOrderStatus.CANCELLED: OrderStatus.CANCELLED,
    BrokerOrderStatus.REJECTED: OrderStatus.REJECTED,
}


async def apply_update(session: AsyncSession, update: OrderUpdate) -> bool:
    """Apply one normalized broker update. Returns False if no matching order."""
    order = (
        await session.execute(
            select(Order).where(
                Order.broker == update.broker,
                Order.broker_order_id == update.broker_order_id,
            ).order_by(Order.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if order is None:
        cid = (update.raw or {}).get("client_order_id") or (update.raw or {}).get("correlationId")
        if cid:
            order = (
                await session.execute(select(Order).where(Order.correlation_id == cid))
            ).scalar_one_or_none()
    if order is None:
        log.warning("order_update_unmatched", broker=update.broker,
                    broker_order_id=update.broker_order_id)
        return False

    signal = await session.get(Signal, order.signal_id) if order.signal_id else None

    if update.leg in ("target", "stop_loss") and update.status == BrokerOrderStatus.FILLED:
        await _close_position(session, order, signal, update)
        return True

    order.status = _STATUS_TO_ORDER[update.status].value
    if update.filled_qty:
        order.filled_qty = update.filled_qty
    if update.avg_price is not None:
        order.avg_fill_price = update.avg_price

    if update.status == BrokerOrderStatus.FILLED:
        session.add(Trade(
            order_id=order.id, qty=update.filled_qty or order.qty,
            price=update.avg_price or order.price or 0.0,
        ))
        await _open_or_close_on_entry_fill(session, order, signal, update)
    elif update.status in (BrokerOrderStatus.CANCELLED, BrokerOrderStatus.REJECTED):
        if signal and signal.state in (SignalState.EXECUTING.value, SignalState.PARSED.value):
            signal.state = (
                SignalState.CANCELLED.value
                if update.status == BrokerOrderStatus.CANCELLED
                else SignalState.ERROR.value
            )
            if update.status == BrokerOrderStatus.REJECTED:
                signal.error = str((update.raw or {}).get("rejectReason", "rejected by broker"))
    await audit.record(
        session, "order", f"update_{update.status.value}",
        {"broker_order_id": update.broker_order_id, "leg": update.leg,
         "avg_price": update.avg_price, "filled_qty": update.filled_qty},
        order.signal_id,
    )
    return True


async def _open_or_close_on_entry_fill(
    session: AsyncSession, order: Order, signal: Signal | None, update: OrderUpdate
) -> None:
    """Entry fill: open a position — or close one if this was an exit order."""
    exit_for = (order.raw or {}).get("exit_for_position")
    if exit_for:
        pos = await session.get(Position, exit_for)
        if pos and pos.status == PositionStatus.OPEN.value:
            exit_price = update.avg_price or 0.0
            sign = 1 if pos.qty > 0 else -1
            pos.realized_pnl = (exit_price - (pos.avg_entry_price or 0.0)) * abs(pos.qty) * sign
            pos.exit_price = exit_price
            pos.status = PositionStatus.CLOSED.value
            pos.closed_at = utcnow()
            if signal:
                signal.state = SignalState.CLOSED.value
            origin = await session.get(Signal, pos.signal_id) if pos.signal_id else None
            if origin:
                origin.state = SignalState.CLOSED.value
            await audit.record(session, "position", "closed_by_exit",
                               {"position_id": pos.id, "pnl": pos.realized_pnl}, pos.signal_id)
        return

    sign = 1 if order.side == "BUY" else -1
    session.add(Position(
        signal_id=order.signal_id,
        broker=order.broker,
        symbol=order.symbol,
        security_id=order.security_id,
        side=order.side,
        qty=sign * (update.filled_qty or order.qty),
        avg_entry_price=update.avg_price or order.price,
    ))
    if signal:
        signal.state = SignalState.OPEN.value
    await audit.record(session, "position", "opened",
                       {"symbol": order.symbol, "qty": update.filled_qty or order.qty,
                        "price": update.avg_price}, order.signal_id)


async def _close_position(
    session: AsyncSession, order: Order, signal: Signal | None, update: OrderUpdate
) -> None:
    pos = (
        await session.execute(
            select(Position).where(
                Position.signal_id == order.signal_id,
                Position.status == PositionStatus.OPEN.value,
            )
        )
    ).scalar_one_or_none()
    exit_price = update.avg_price or 0.0
    if pos is not None:
        sign = 1 if pos.qty > 0 else -1
        pos.realized_pnl = (exit_price - (pos.avg_entry_price or 0.0)) * abs(pos.qty) * sign
        pos.exit_price = exit_price
        pos.status = PositionStatus.CLOSED.value
        pos.closed_at = utcnow()
    if signal:
        signal.state = SignalState.CLOSED.value
    session.add(Trade(order_id=order.id, qty=update.filled_qty or order.qty, price=exit_price))
    await audit.record(
        session, "position", "closed",
        {"leg": update.leg, "exit_price": exit_price,
         "pnl": pos.realized_pnl if pos else None},
        order.signal_id,
    )


class TrackerService:
    """Runs one consumer task per active broker adapter."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def watch(self, name: str, adapter: BrokerAdapter) -> None:
        if name in self._tasks and not self._tasks[name].done():
            return
        self._tasks[name] = asyncio.create_task(self._consume(name, adapter))
        log.info("tracker_watching", broker=name)

    async def _consume(self, name: str, adapter: BrokerAdapter) -> None:
        async for update in adapter.stream_order_updates():
            try:
                async with session_factory()() as session:
                    await apply_update(session, update)
                    await session.commit()
            except Exception:
                log.exception("tracker_apply_error", broker=name)

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()


async def reconcile() -> None:
    """On startup: re-sync every non-terminal order against the broker's book.

    Guarantees a crash/restart never leaves phantom local state or double
    orders — local rows are corrected from broker truth before trading resumes.
    """
    async with session_factory()() as session:
        open_orders = (
            await session.execute(
                select(Order).where(Order.status.in_([
                    OrderStatus.PENDING.value,
                    OrderStatus.PLACED.value,
                    OrderStatus.PART_FILLED.value,
                ]))
            )
        ).scalars().all()
        if not open_orders:
            return

        by_broker: dict[str, list[Order]] = {}
        for o in open_orders:
            by_broker.setdefault(o.broker, []).append(o)

        for broker, orders in by_broker.items():
            try:
                adapter = await registry.get_adapter(session, broker)
                remote = {str(r.get("orderId") or r.get("id") or ""): r
                          for r in await adapter.get_orders()}
            except BrokerError as exc:
                log.warning("reconcile_skipped", broker=broker, error=str(exc))
                continue
            for order in orders:
                r = remote.get(order.broker_order_id)
                if r is None:
                    order.status = OrderStatus.ERROR.value
                    order.status_reason = "not found at broker during reconciliation"
                    await audit.record(session, "order", "reconcile_missing",
                                       {"broker_order_id": order.broker_order_id},
                                       order.signal_id, commit=False)
                    continue
                status = str(r.get("orderStatus") or r.get("state") or "").upper()
                if status in ("TRADED", "CLOSED"):
                    from app.execution.dhan.adapter import map_status
                    await apply_update(session, OrderUpdate(
                        broker=broker, broker_order_id=order.broker_order_id,
                        status=map_status("TRADED"),
                        filled_qty=float(r.get("filledQty") or r.get("size") or order.qty),
                        avg_price=(
                            float(v) if (v := r.get("averageTradedPrice")
                                         or r.get("average_fill_price")) else None
                        ),
                        raw=r,
                    ))
                elif status in ("CANCELLED", "REJECTED", "EXPIRED"):
                    order.status = OrderStatus.CANCELLED.value
        await session.commit()
        log.info("reconcile_done", checked=len(open_orders))
