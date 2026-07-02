"""Dhan adapter: BracketOrderRequest → Super Order (entry + target + SL + trail)."""
import asyncio
import json
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
from app.execution.dhan.client import DhanClient

log = get_logger(__name__)

ORDER_WS_URL = "wss://api-order-update.dhan.co"

_STATUS_MAP = {
    "TRADED": BrokerOrderStatus.FILLED,
    "PART_TRADED": BrokerOrderStatus.PART_FILLED,
    "REJECTED": BrokerOrderStatus.REJECTED,
    "CANCELLED": BrokerOrderStatus.CANCELLED,
    "EXPIRED": BrokerOrderStatus.CANCELLED,
}

_LEG_MAP = {
    "ENTRY_LEG": "entry",
    "TARGET_LEG": "target",
    "STOP_LOSS_LEG": "stop_loss",
}


def map_status(dhan_status: str) -> BrokerOrderStatus:
    return _STATUS_MAP.get((dhan_status or "").upper(), BrokerOrderStatus.PENDING)


class DhanAdapter(BrokerAdapter):
    name = "dhan"

    def __init__(self, client_id: str, access_token: str, client: DhanClient | None = None) -> None:
        self.client_id = client_id
        self.access_token = access_token
        self.client = client or DhanClient(client_id, access_token)

    async def place_bracket(self, req: BracketOrderRequest) -> PlacedOrder:
        order_type = {
            "MARKET": "MARKET",
            "LIMIT": "LIMIT",
            "STOP_LIMIT": "STOP_LOSS",  # Dhan's stop-limit entry
        }.get(req.order_type, "LIMIT")
        payload: dict[str, Any] = {
            "correlationId": req.correlation_id,
            "transactionType": req.side,
            "exchangeSegment": req.exchange_segment or "NSE_FNO",
            "productType": req.product_type,
            "orderType": order_type,
            "securityId": req.security_id,
            "quantity": int(req.qty),
            "price": float(req.price) if req.price is not None else 0.0,
        }
        if req.trigger_price is not None:
            payload["triggerPrice"] = float(req.trigger_price)
        if req.target_price is not None:
            payload["targetPrice"] = float(req.target_price)
        if req.stop_loss_price is not None:
            payload["stopLossPrice"] = float(req.stop_loss_price)
        if req.trailing_jump is not None:
            payload["trailingJump"] = float(req.trailing_jump)

        data = await self.client.place_super_order(payload)
        order_id = str((data or {}).get("orderId", ""))
        if not order_id:
            raise BrokerError(f"dhan: no orderId in response {data}")
        return PlacedOrder(
            broker_order_id=order_id,
            status=map_status((data or {}).get("orderStatus", "PENDING")),
            raw=data or {},
        )

    async def modify_bracket(
        self, broker_order_id: str,
        target_price: float | None = None, stop_loss_price: float | None = None,
    ) -> None:
        # after entry is TRADED only TARGET_LEG / STOP_LOSS_LEG may be modified
        if target_price is not None:
            await self.client.modify_super_order(
                broker_order_id, {"legName": "TARGET_LEG", "targetPrice": float(target_price)}
            )
        if stop_loss_price is not None:
            await self.client.modify_super_order(
                broker_order_id, {"legName": "STOP_LOSS_LEG", "stopLossPrice": float(stop_loss_price)}
            )

    async def cancel(self, broker_order_id: str) -> None:
        await self.client.cancel_super_order(broker_order_id, "ENTRY_LEG")

    async def get_orders(self) -> list[dict[str, Any]]:
        return await self.client.get_super_orders()

    async def get_positions(self) -> list[BrokerPosition]:
        rows = await self.client.get_positions()
        out = []
        for p in rows:
            qty = float(p.get("netQty", 0) or 0)
            if qty == 0:
                continue
            out.append(
                BrokerPosition(
                    symbol=p.get("tradingSymbol", ""),
                    security_id=str(p.get("securityId", "")),
                    qty=qty,
                    avg_price=float(p.get("buyAvg" if qty > 0 else "sellAvg", 0) or 0) or None,
                    unrealized_pnl=float(p.get("unrealizedProfit", 0) or 0),
                    raw=p,
                )
            )
        return out

    async def stream_order_updates(self) -> AsyncIterator[OrderUpdate]:
        """Dhan live order-update websocket; reconnects with backoff forever."""
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(ORDER_WS_URL, ping_interval=20) as ws:
                    await ws.send(json.dumps({
                        "LoginReq": {
                            "MsgCode": 42,
                            "ClientId": self.client_id,
                            "Token": self.access_token,
                        },
                        "UserType": "SELF",
                    }))
                    log.info("dhan_order_ws_connected")
                    backoff = 1.0
                    async for raw in ws:
                        update = self._parse_ws_message(raw)
                        if update is not None:
                            yield update
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("dhan_order_ws_error", error=str(exc), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _parse_ws_message(self, raw: str | bytes) -> OrderUpdate | None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(msg, dict):
            return None
        data = msg.get("Data") or msg.get("data")
        if (msg.get("Type") or msg.get("type")) != "order_alert" or not isinstance(data, dict):
            return None
        order_id = str(data.get("orderNo") or data.get("OrderNo") or data.get("orderId") or "")
        if not order_id:
            return None
        status_str = str(data.get("status") or data.get("Status") or "")
        leg = _LEG_MAP.get(str(data.get("legName") or data.get("LegName") or ""), "entry")
        avg = data.get("tradedPrice") or data.get("TradedPrice") or data.get("avgTradedPrice")
        filled = data.get("tradedQty") or data.get("TradedQty") or data.get("filledQty") or 0
        return OrderUpdate(
            broker=self.name,
            broker_order_id=order_id,
            status=map_status(status_str),
            filled_qty=float(filled or 0),
            avg_price=float(avg) if avg else None,
            leg=leg,
            raw=data,
        )

    async def close(self) -> None:
        await self.client.close()
