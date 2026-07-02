"""Thin async client for the Delta Exchange India REST API.

Auth per docs.delta.exchange: signature = HMAC_SHA256(api_secret,
method + timestamp + path + query_string + body), hex-encoded, with
`api-key`, `timestamp`, `signature` headers. Production base is
api.india.delta.exchange; the India testnet base can be injected for E2E tests.
"""
import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from app.execution.base_adapter import BrokerError

BASE_URL = "https://api.india.delta.exchange"
TESTNET_URL = "https://cdn-ind.testnet.deltaex.org"


def sign(secret: str, method: str, timestamp: str, path: str, query: str = "", body: str = "") -> str:
    message = method + timestamp + path + query + body
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class DeltaClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Content-Type": "application/json", "User-Agent": "autobot-trader"},
            timeout=15.0,
        )

    async def _request(
        self, method: str, path: str,
        params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        body = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = {}
        if auth:
            ts = str(int(time.time()))
            headers = {
                "api-key": self.api_key,
                "timestamp": ts,
                "signature": sign(self.api_secret, method, ts, path, query, body),
            }
        try:
            resp = await self._http.request(
                method, path + query, content=body or None, headers=headers
            )
        except httpx.HTTPError as exc:
            raise BrokerError(f"delta http error: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
            raise BrokerError(f"delta {resp.status_code}: {json.dumps(data)[:500]}")
        return data.get("result", data) if isinstance(data, dict) else data

    # -- market/instrument data ---------------------------------------------
    async def get_products(self, contract_types: str = "perpetual_futures") -> list[dict[str, Any]]:
        return await self._request(
            "GET", "/v2/products", params={"contract_types": contract_types, "page_size": "500"},
            auth=False,
        ) or []

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._request("GET", f"/v2/tickers/{symbol}", auth=False) or {}

    # -- trading --------------------------------------------------------------
    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v2/orders", json_body=payload)

    async def edit_bracket(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", "/v2/orders/bracket", json_body=payload)

    async def cancel_order(self, order_id: int, product_id: int) -> dict[str, Any]:
        return await self._request(
            "DELETE", "/v2/orders", json_body={"id": order_id, "product_id": product_id}
        )

    async def get_live_orders(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/v2/orders") or []

    async def get_positions(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/v2/positions/margined") or []

    async def get_wallet(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/v2/wallet/balances") or []

    async def close(self) -> None:
        await self._http.aclose()
