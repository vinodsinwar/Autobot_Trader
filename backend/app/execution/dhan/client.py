"""Thin async client for the DhanHQ v2 REST API.

Hand-rolled with httpx (instead of the sync official SDK) so the whole money
path stays on one asyncio loop and every call is trivially mockable in tests.
Endpoints per https://dhanhq.co/docs/v2/ (orders, super-order, positions).
"""
from typing import Any

import httpx

from app.execution.base_adapter import BrokerError

BASE_URL = "https://api.dhan.co/v2"


class DhanClient:
    def __init__(self, client_id: str, access_token: str, base_url: str = BASE_URL) -> None:
        self.client_id = client_id
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "access-token": access_token,
                "client-id": client_id,
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise BrokerError(f"dhan http error: {exc}") from exc
        if resp.status_code == 401:
            raise BrokerError("dhan: access token expired or invalid (tokens last 24h — renew from dashboard)")
        if resp.status_code >= 400:
            raise BrokerError(f"dhan {resp.status_code}: {resp.text[:500]}")
        if not resp.content:
            return None
        return resp.json()

    # -- super orders (entry + target + SL in one request) ------------------
    async def place_super_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/super/orders", {**payload, "dhanClientId": self.client_id})

    async def modify_super_order(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "PUT", f"/super/orders/{order_id}",
            {**payload, "dhanClientId": self.client_id, "orderId": order_id},
        )

    async def cancel_super_order(self, order_id: str, leg: str = "ENTRY_LEG") -> dict[str, Any]:
        return await self._request("DELETE", f"/super/orders/{order_id}/{leg}")

    async def get_super_orders(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/super/orders") or []

    # -- plain orders (exits) ------------------------------------------------
    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/orders", {**payload, "dhanClientId": self.client_id})

    async def get_orders(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/orders") or []

    async def get_positions(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/positions") or []

    async def get_fund_limit(self) -> dict[str, Any]:
        return await self._request("GET", "/fundlimit") or {}

    async def close(self) -> None:
        await self._http.aclose()
