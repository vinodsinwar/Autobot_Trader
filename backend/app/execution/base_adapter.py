"""Broker adapter contract.

One shape for all brokers: an entry order with optional target/stop-loss
bracket. Dhan implements it natively as a Super Order, Delta as an order with
bracket params, Paper simulates it.
"""
import abc
import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


class BrokerOrderStatus(enum.StrEnum):
    PENDING = "pending"        # accepted, waiting at exchange
    PART_FILLED = "part_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(slots=True)
class BracketOrderRequest:
    correlation_id: str
    security_id: str            # broker instrument id (Dhan securityId / Delta product_id)
    symbol: str                 # human-readable, for logs and paper mode
    side: str                   # BUY | SELL
    qty: float
    order_type: str = "LIMIT"   # LIMIT | MARKET | STOP_LIMIT (entry above/below)
    price: float | None = None
    trigger_price: float | None = None
    target_price: float | None = None
    stop_loss_price: float | None = None
    trailing_jump: float | None = None
    product_type: str = "INTRADAY"       # Dhan: INTRADAY|MARGIN|CNC ; Delta ignores
    exchange_segment: str = ""           # Dhan: NSE_FNO etc.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlacedOrder:
    broker_order_id: str
    status: BrokerOrderStatus
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderUpdate:
    """Normalized order-update event from a broker stream."""
    broker: str
    broker_order_id: str
    status: BrokerOrderStatus
    filled_qty: float = 0.0
    avg_price: float | None = None
    leg: str = "entry"           # entry | target | stop_loss
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrokerPosition:
    symbol: str
    security_id: str
    qty: float                   # signed: negative = short
    avg_price: float | None = None
    unrealized_pnl: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class BrokerError(Exception):
    """Raised when the broker rejects a request or is unreachable."""


class BrokerAdapter(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def place_bracket(self, req: BracketOrderRequest) -> PlacedOrder: ...

    @abc.abstractmethod
    async def modify_bracket(
        self,
        broker_order_id: str,
        target_price: float | None = None,
        stop_loss_price: float | None = None,
    ) -> None: ...

    @abc.abstractmethod
    async def cancel(self, broker_order_id: str) -> None: ...

    @abc.abstractmethod
    async def get_orders(self) -> list[dict[str, Any]]:
        """Raw open/today orders — used by restart reconciliation."""

    @abc.abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abc.abstractmethod
    def stream_order_updates(self) -> AsyncIterator[OrderUpdate]:
        """Long-lived stream of normalized order updates (reconnects internally)."""

    async def close(self) -> None:  # noqa: B027  (optional hook)
        pass
