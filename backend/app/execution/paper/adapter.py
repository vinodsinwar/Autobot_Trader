"""Paper broker: simulates bracket-order fills. The default safety mode.

Fill model (deliberately simple and pessimistic-neutral):
- MARKET entries fill immediately at `meta["ltp"]` (or entry price fallback).
- LIMIT / STOP_LIMIT entries fill when a simulated or fed tick crosses them
  (`on_tick`), or immediately if `meta["ltp"]` already satisfies the price.
- After entry fill, target/stop legs arm; the first tick to touch one fills
  it and cancels the sibling (OCO), closing the bracket.
"""
import asyncio
import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.execution.base_adapter import (
    BracketOrderRequest,
    BrokerAdapter,
    BrokerError,
    BrokerOrderStatus,
    BrokerPosition,
    OrderUpdate,
    PlacedOrder,
)

log = get_logger(__name__)


@dataclass(slots=True)
class _PaperOrder:
    req: BracketOrderRequest
    order_id: str
    status: BrokerOrderStatus = BrokerOrderStatus.PENDING
    entry_filled: bool = False
    fill_price: float | None = None
    exit_price: float | None = None
    closed: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class PaperAdapter(BrokerAdapter):
    name = "paper"

    def __init__(self) -> None:
        self._orders: dict[str, _PaperOrder] = {}
        self._updates: asyncio.Queue[OrderUpdate] = asyncio.Queue()
        self._seq = itertools.count(1)

    # -- helpers -----------------------------------------------------------
    def _emit(self, order: _PaperOrder, status: BrokerOrderStatus, leg: str = "entry",
              avg_price: float | None = None, filled_qty: float | None = None) -> None:
        self._updates.put_nowait(
            OrderUpdate(
                broker=self.name,
                broker_order_id=order.order_id,
                status=status,
                filled_qty=filled_qty if filled_qty is not None else order.req.qty,
                avg_price=avg_price,
                leg=leg,
            )
        )

    def _fill_entry(self, order: _PaperOrder, price: float) -> None:
        order.entry_filled = True
        order.fill_price = price
        order.status = BrokerOrderStatus.FILLED
        self._emit(order, BrokerOrderStatus.FILLED, "entry", avg_price=price)
        log.info("paper_entry_filled", order_id=order.order_id, symbol=order.req.symbol, price=price)

    def _close(self, order: _PaperOrder, price: float, leg: str) -> None:
        order.closed = True
        order.exit_price = price
        self._emit(order, BrokerOrderStatus.FILLED, leg, avg_price=price)
        log.info("paper_bracket_closed", order_id=order.order_id, leg=leg, price=price)

    # -- BrokerAdapter -----------------------------------------------------
    async def place_bracket(self, req: BracketOrderRequest) -> PlacedOrder:
        if req.qty <= 0:
            raise BrokerError("quantity must be positive")
        order = _PaperOrder(req=req, order_id=f"PAPER-{next(self._seq):06d}")
        self._orders[order.order_id] = order
        self._emit(order, BrokerOrderStatus.PENDING)

        ltp = req.meta.get("ltp")
        if req.order_type == "MARKET":
            self._fill_entry(order, float(ltp if ltp is not None else req.price or 0))
        elif ltp is not None:
            self.on_tick_for(order, float(ltp))
        return PlacedOrder(broker_order_id=order.order_id, status=order.status)

    async def modify_bracket(
        self, broker_order_id: str,
        target_price: float | None = None, stop_loss_price: float | None = None,
    ) -> None:
        order = self._orders.get(broker_order_id)
        if order is None or order.closed:
            raise BrokerError(f"unknown or closed order {broker_order_id}")
        if target_price is not None:
            order.req.target_price = target_price
        if stop_loss_price is not None:
            order.req.stop_loss_price = stop_loss_price

    async def cancel(self, broker_order_id: str) -> None:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        if order.closed:
            return
        order.closed = True
        order.status = BrokerOrderStatus.CANCELLED
        self._emit(order, BrokerOrderStatus.CANCELLED)

    async def get_orders(self) -> list[dict[str, Any]]:
        return [
            {
                "orderId": o.order_id,
                "correlationId": o.req.correlation_id,
                "status": o.status.value,
                "symbol": o.req.symbol,
            }
            for o in self._orders.values()
        ]

    async def get_positions(self) -> list[BrokerPosition]:
        out = []
        for o in self._orders.values():
            if o.entry_filled and not o.closed:
                sign = 1 if o.req.side == "BUY" else -1
                out.append(
                    BrokerPosition(
                        symbol=o.req.symbol,
                        security_id=o.req.security_id,
                        qty=sign * o.req.qty,
                        avg_price=o.fill_price,
                    )
                )
        return out

    async def stream_order_updates(self) -> AsyncIterator[OrderUpdate]:
        while True:
            yield await self._updates.get()

    # -- simulation --------------------------------------------------------
    def on_tick(self, symbol: str, price: float) -> None:
        """Feed a price tick to all live paper orders for `symbol`."""
        for order in list(self._orders.values()):
            if order.req.symbol == symbol and not order.closed:
                self.on_tick_for(order, price)

    def on_tick_for(self, order: _PaperOrder, price: float) -> None:
        req = order.req
        if order.closed:
            return
        if not order.entry_filled:
            if req.order_type == "LIMIT" and req.price is not None:
                crossed = price <= req.price if req.side == "BUY" else price >= req.price
                if crossed:
                    self._fill_entry(order, req.price)
            elif req.order_type == "STOP_LIMIT" and req.trigger_price is not None:
                crossed = price >= req.trigger_price if req.side == "BUY" else price <= req.trigger_price
                if crossed:
                    self._fill_entry(order, req.trigger_price)
            return
        # bracket legs (OCO)
        if req.side == "BUY":
            if req.target_price is not None and price >= req.target_price:
                self._close(order, req.target_price, "target")
            elif req.stop_loss_price is not None and price <= req.stop_loss_price:
                self._close(order, req.stop_loss_price, "stop_loss")
        else:
            if req.target_price is not None and price <= req.target_price:
                self._close(order, req.target_price, "target")
            elif req.stop_loss_price is not None and price >= req.stop_loss_price:
                self._close(order, req.stop_loss_price, "stop_loss")
