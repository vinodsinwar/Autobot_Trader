"""Dhan & Delta adapters: REST contract tests (respx-mocked) + WS parsing."""
import json

import respx
from httpx import Response

from app.execution.base_adapter import BracketOrderRequest, BrokerOrderStatus
from app.execution.delta.adapter import DeltaAdapter
from app.execution.delta.client import sign
from app.execution.dhan.adapter import DhanAdapter


def bracket_req(**kw):
    base = dict(
        correlation_id="sig-42", security_id="43492", symbol="NIFTY 25000 CE",
        side="BUY", qty=75, order_type="LIMIT", price=150.0,
        target_price=170.0, stop_loss_price=130.0, exchange_segment="NSE_FNO",
    )
    base.update(kw)
    return BracketOrderRequest(**base)


class TestDhan:
    @respx.mock
    async def test_place_super_order(self):
        route = respx.post("https://api.dhan.co/v2/super/orders").mock(
            return_value=Response(200, json={"orderId": "112111182045", "orderStatus": "PENDING"})
        )
        adapter = DhanAdapter("1000000001", "token")
        placed = await adapter.place_bracket(bracket_req())
        assert placed.broker_order_id == "112111182045"
        assert placed.status == BrokerOrderStatus.PENDING

        body = json.loads(route.calls[0].request.content)
        assert body["dhanClientId"] == "1000000001"
        assert body["transactionType"] == "BUY"
        assert body["securityId"] == "43492"
        assert body["quantity"] == 75
        assert body["targetPrice"] == 170.0
        assert body["stopLossPrice"] == 130.0
        assert body["correlationId"] == "sig-42"
        assert route.calls[0].request.headers["access-token"] == "token"
        await adapter.close()

    @respx.mock
    async def test_expired_token_maps_to_broker_error(self):
        respx.post("https://api.dhan.co/v2/super/orders").mock(return_value=Response(401))
        adapter = DhanAdapter("1", "stale")
        try:
            await adapter.place_bracket(bracket_req())
            raise AssertionError("expected BrokerError")
        except Exception as exc:
            assert "token expired" in str(exc)
        await adapter.close()

    @respx.mock
    async def test_modify_bracket_targets_correct_legs(self):
        route = respx.put("https://api.dhan.co/v2/super/orders/OID1").mock(
            return_value=Response(200, json={"orderId": "OID1", "orderStatus": "TRADED"})
        )
        adapter = DhanAdapter("1", "t")
        await adapter.modify_bracket("OID1", target_price=180, stop_loss_price=140)
        legs = [json.loads(c.request.content)["legName"] for c in route.calls]
        assert legs == ["TARGET_LEG", "STOP_LOSS_LEG"]
        await adapter.close()

    def test_ws_order_alert_parsing(self):
        adapter = DhanAdapter("1", "t")
        msg = json.dumps({
            "Type": "order_alert",
            "Data": {"orderNo": "5125022053491", "status": "TRADED",
                     "tradedQty": 75, "tradedPrice": 151.2, "legName": "ENTRY_LEG"},
        })
        u = adapter._parse_ws_message(msg)
        assert u.broker_order_id == "5125022053491"
        assert u.status == BrokerOrderStatus.FILLED
        assert u.avg_price == 151.2
        assert u.leg == "entry"

    def test_ws_garbage_ignored(self):
        adapter = DhanAdapter("1", "t")
        assert adapter._parse_ws_message("not json") is None
        assert adapter._parse_ws_message(json.dumps({"Type": "heartbeat"})) is None


class TestDelta:
    def test_hmac_signature_stable(self):
        sig = sign("secret", "POST", "1700000000", "/v2/orders", "", '{"a":1}')
        # signature = HMAC_SHA256(secret, method+timestamp+path+query+body)
        import hashlib
        import hmac as hmaclib
        expected = hmaclib.new(
            b"secret", b'POST1700000000/v2/orders{"a":1}', hashlib.sha256
        ).hexdigest()
        assert sig == expected

    @respx.mock
    async def test_place_order_with_bracket(self):
        route = respx.post("https://api.india.delta.exchange/v2/orders").mock(
            return_value=Response(200, json={"success": True, "result": {
                "id": 123456, "state": "open", "size": 10, "unfilled_size": 10,
            }})
        )
        adapter = DeltaAdapter("key", "secret")
        placed = await adapter.place_bracket(bracket_req(
            security_id="27", symbol="BTCUSD", qty=10, price=65000.0,
            target_price=67000.0, stop_loss_price=63500.0,
        ))
        assert placed.broker_order_id == "123456"
        assert placed.status == BrokerOrderStatus.PENDING

        body = json.loads(route.calls[0].request.content)
        assert body["product_id"] == 27
        assert body["side"] == "buy"
        assert body["order_type"] == "limit_order"
        assert body["bracket_take_profit_price"] == "67000.0"
        assert body["bracket_stop_loss_price"] == "63500.0"
        assert body["client_order_id"] == "sig-42"
        headers = route.calls[0].request.headers
        assert "signature" in headers and "timestamp" in headers
        assert headers["api-key"] == "key"
        await adapter.close()

    @respx.mock
    async def test_api_error_raises_broker_error(self):
        respx.post("https://api.india.delta.exchange/v2/orders").mock(
            return_value=Response(400, json={"success": False, "error": {"code": "insufficient_margin"}})
        )
        adapter = DeltaAdapter("key", "secret")
        try:
            await adapter.place_bracket(bracket_req(security_id="27", qty=10))
            raise AssertionError("expected BrokerError")
        except Exception as exc:
            assert "insufficient_margin" in str(exc)
        await adapter.close()

    def test_ws_order_update_parsing(self):
        adapter = DeltaAdapter("key", "secret")
        u = adapter._parse_ws_message(json.dumps({
            "type": "orders", "order_id": "9", "state": "closed",
            "size": 10, "unfilled_size": 0, "average_fill_price": "65010.5",
        }))
        assert u.status == BrokerOrderStatus.FILLED
        assert u.filled_qty == 10
        assert u.avg_price == 65010.5

    def test_ws_partial_fill_state(self):
        adapter = DeltaAdapter("key", "secret")
        u = adapter._parse_ws_message(json.dumps({
            "type": "orders", "order_id": "9", "state": "open",
            "size": 10, "unfilled_size": 4,
        }))
        assert u.status == BrokerOrderStatus.PART_FILLED
        assert u.filled_qty == 6
