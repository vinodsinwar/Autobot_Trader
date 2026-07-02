"""Delta Exchange India adapter: entry order with attached bracket (TP/SL)."""
import asyncio
import json
import time
from collections.abc import AsyncIterator
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
from app.execution.delta.client import DeltaClient, sign

log = get_logger(__name__)

WS_URL = "wss://socket.india.delta.exchange"
TESTNET_WS_URL = "wss://socket-ind.testnet.deltaex.org"


def map_state(state: str, unfilled: float, size: float) -> BrokerOrderStatus:
    state = (state or "").lower()
    if state == "cancelled":
        return BrokerOrderStatus.CANCELLED
    if state == "closed":
        return BrokerOrderStatus.FILLED
    if state in ("open", "pending"):
        if 0 < unfilled < size:
            return BrokerOrderStatus.PART_FILLED
        return BrokerOrderStatus.PENDING
    return BrokerOrderStatus.PENDING


class DeltaAdapter(BrokerAdapter):
    name = "delta"

    def __init__(
        self, api_key: str, api_secret: str,
        client: DeltaClient | None = None, ws_url: str = WS_URL,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.ws_url = ws_url
        self.client = client or DeltaClient(
            api_key, api_secret, **({"base_url": base_url} if base_url else {})
        )

    async def place_bracket(self, req: BracketOrderRequest) -> PlacedOrder:
        payload: dict[str, Any] = {
            "product_id": int(req.security_id),
            "size": int(req.qty),
            "side": req.side.lower(),
            "client_order_id": req.correlation_id,
        }
        if req.order_type == "MARKET":
            payload["order_type"] = "market_order"
        elif req.order_type == "STOP_LIMIT":
            payload["order_type"] = "limit_order"
            payload["limit_price"] = str(req.price)
            payload["stop_order_type"] = "stop_loss_order"
            payload["stop_price"] = str(req.trigger_price)
        else:
            payload["order_type"] = "limit_order"
            payload["limit_price"] = str(req.price)
        if req.stop_loss_price is not None:
            payload["bracket_stop_loss_price"] = str(req.stop_loss_price)
            payload["bracket_stop_loss_limit_price"] = str(req.stop_loss_price)
        if req.target_price is not None:
            payload["bracket_take_profit_price"] = str(req.target_price)
            payload["bracket_take_profit_limit_price"] = str(req.target_price)
        if req.trailing_jump is not None:
            payload["bracket_trail_amount"] = str(req.trailing_jump)

        data = await self.client.place_order(payload)
        order_id = str((data or {}).get("id", ""))
        if not order_id:
            raise BrokerError(f"delta: no order id in response {data}")
        size = float(data.get("size", req.qty) or req.qty)
        unfilled = float(data.get("unfilled_size", size) or 0)
        return PlacedOrder(
            broker_order_id=order_id,
            status=map_state(data.get("state", ""), unfilled, size),
            raw=data or {},
        )

    async def modify_bracket(
        self, broker_order_id: str,
        target_price: float | None = None, stop_loss_price: float | None = None,
    ) -> None:
        # bracket edits are per-position on Delta; need the product_id from the order
        orders = await self.client.get_live_orders()
        product_id = None
        for o in orders:
            if str(o.get("id")) == broker_order_id or o.get("client_order_id") == broker_order_id:
                product_id = o.get("product_id")
                break
        if product_id is None:
            raise BrokerError(f"delta: cannot find order {broker_order_id} to modify bracket")
        payload: dict[str, Any] = {"product_id": int(product_id)}
        if stop_loss_price is not None:
            payload["bracket_stop_loss_price"] = str(stop_loss_price)
            payload["bracket_stop_loss_limit_price"] = str(stop_loss_price)
        if target_price is not None:
            payload["bracket_take_profit_price"] = str(target_price)
            payload["bracket_take_profit_limit_price"] = str(target_price)
        await self.client.edit_bracket(payload)

    async def cancel(self, broker_order_id: str) -> None:
        orders = await self.client.get_live_orders()
        for o in orders:
            if str(o.get("id")) == broker_order_id:
                await self.client.cancel_order(int(o["id"]), int(o["product_id"]))
                return
        raise BrokerError(f"delta: order {broker_order_id} not found among live orders")

    async def get_orders(self) -> list[dict[str, Any]]:
        return await self.client.get_live_orders()

    async def get_positions(self) -> list[BrokerPosition]:
        rows = await self.client.get_positions()
        out = []
        for p in rows:
            qty = float(p.get("size", 0) or 0)
            if qty == 0:
                continue
            product = p.get("product") or {}
            out.append(
                BrokerPosition(
                    symbol=product.get("symbol", str(p.get("product_id", ""))),
                    security_id=str(p.get("product_id", "")),
                    qty=qty,
                    avg_price=float(p.get("entry_price", 0) or 0) or None,
                    unrealized_pnl=float(p.get("unrealized_pnl", 0) or 0),
                    raw=p,
                )
            )
        return out

    async def stream_order_updates(self) -> AsyncIterator[OrderUpdate]:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    ts = str(int(time.time()))
                    await ws.send(json.dumps({
                        "type": "key-auth",
                        "payload": {
                            "api-key": self.api_key,
                            "signature": sign(self.api_secret, "GET", ts, "/live"),
                            "timestamp": ts,
                        },
                    }))
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "payload": {"channels": [{"name": "orders", "symbols": ["all"]}]},
                    }))
                    log.info("delta_order_ws_connected")
                    backoff = 1.0
                    async for raw in ws:
                        update = self._parse_ws_message(raw)
                        if update is not None:
                            yield update
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("delta_order_ws_error", error=str(exc), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _parse_ws_message(self, raw: str | bytes) -> OrderUpdate | None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(msg, dict) or msg.get("type") != "orders":
            return None
        order_id = str(msg.get("order_id") or msg.get("id") or "")
        if not order_id:
            return None
        size = float(msg.get("size", 0) or 0)
        unfilled = float(msg.get("unfilled_size", 0) or 0)
        avg = msg.get("average_fill_price") or msg.get("avg_fill_price")
        return OrderUpdate(
            broker=self.name,
            broker_order_id=order_id,
            status=map_state(msg.get("state", ""), unfilled, size),
            filled_qty=size - unfilled,
            avg_price=float(avg) if avg else None,
            leg="entry",
            raw=msg,
        )

    async def close(self) -> None:
        await self.client.close()
