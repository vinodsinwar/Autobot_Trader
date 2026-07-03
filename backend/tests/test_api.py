"""API integration tests over the ASGI app (no network)."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.core import security
from app.db.models import Channel, Signal, SignalState
from app.db.session import set_setting
from app.execution import registry


@pytest.fixture(autouse=True)
async def clean_registry():
    await registry.invalidate()
    yield
    await registry.invalidate()


@pytest.fixture
async def client(db):
    from app.main import app

    async with db.session_factory()() as session:
        await set_setting(session, "auth",
                          {"password_hash": security.hash_password("test-password")})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth(client):
    resp = await client.post("/api/auth/login", json={"password": "test-password"})
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    async def test_wrong_password_rejected(self, client):
        resp = await client.post("/api/auth/login", json={"password": "nope"})
        assert resp.status_code == 401

    async def test_protected_routes_need_token(self, client):
        assert (await client.get("/api/overview")).status_code == 401

    async def test_login_and_access(self, client, auth):
        resp = await client.get("/api/overview", headers=auth)
        assert resp.status_code == 200
        assert "realized_pnl_today" in resp.json()


class TestSignalsApi:
    async def test_approve_flow(self, db, client, auth):
        async with db.session_factory()() as session:
            sig = Signal(
                source="telegram", state=SignalState.AWAITING_APPROVAL.value,
                broker="paper",
                parsed={"kind": "ENTRY", "action": "BUY", "symbol": "NIFTY",
                        "instrument": "OPTION", "strike": 25000, "option_type": "CE",
                        "entry_type": "ABOVE", "entry_price": 150,
                        "targets": [170], "stop_loss": 130, "confidence": 0.9,
                        "_sizing": {"mode": "units", "value": 75}},
            )
            session.add(sig)
            await session.commit()
            sig_id = sig.id

        resp = await client.post(f"/api/signals/{sig_id}/approve", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["state"] == "executing"

        orders = (await client.get("/api/orders", headers=auth)).json()
        assert len(orders) == 1
        assert orders[0]["qty"] == 75

    async def test_approve_wrong_state_409(self, db, client, auth):
        async with db.session_factory()() as session:
            sig = Signal(source="telegram", state=SignalState.CLOSED.value, broker="paper")
            session.add(sig)
            await session.commit()
            sig_id = sig.id
        assert (await client.post(f"/api/signals/{sig_id}/approve", headers=auth)).status_code == 409


class TestChannelsApi:
    async def test_create_and_update(self, client, auth):
        resp = await client.post("/api/channels", headers=auth, json={
            "tg_chat_id": -100123, "title": "My Signals", "broker": "paper",
            "execution_mode": "auto", "sizing": {"mode": "lots", "value": 1},
        })
        assert resp.status_code == 200
        cid = resp.json()["id"]

        resp = await client.patch(f"/api/channels/{cid}", headers=auth,
                                  json={"execution_mode": "manual"})
        assert resp.json()["execution_mode"] == "manual"


class TestSettingsApi:
    async def test_secrets_masked_and_merge_preserved(self, db, client, auth):
        await client.put("/api/settings/dhan", headers=auth, json={
            "client_id": "1000000001", "access_token": "super-secret-jwt",
        })
        got = (await client.get("/api/settings/dhan", headers=auth)).json()
        assert got["value"]["access_token"] == "•••"
        assert got["value"]["client_id"] == "1000000001"

        # resubmitting the masked form must keep the real secret
        await client.put("/api/settings/dhan", headers=auth, json={
            "client_id": "1000000002", "access_token": "•••",
        })
        from app.db.session import get_setting
        async with db.session_factory()() as session:
            stored = await get_setting(session, "dhan")
        assert stored["access_token"] == "super-secret-jwt"
        assert stored["client_id"] == "1000000002"

    async def test_kill_switch_toggle(self, client, auth):
        resp = await client.post("/api/kill-switch", headers=auth, json={"on": True})
        assert resp.json()["kill_switch"] is True
        overview = (await client.get("/api/overview", headers=auth)).json()
        assert overview["kill_switch"] is True


class TestParseTestBench:
    async def test_rules_parse(self, client, auth):
        resp = await client.post("/api/parse-test", headers=auth, json={
            "text": "BUY NIFTY 25000 CE ABOVE 150 TGT 170/190 SL 130",
        })
        data = resp.json()
        assert data["ok"] is True
        assert data["signal"]["strike"] == 25000
        assert "BUY NIFTY 25000 CE" in data["summary"]

    async def test_chatter_refused(self, client, auth):
        resp = await client.post("/api/parse-test", headers=auth,
                                 json={"text": "good morning!"})
        assert resp.json()["ok"] is False


class TestPipelineToApi:
    async def test_full_signal_visible_in_api(self, db, client, auth):
        from datetime import UTC, datetime

        from app.execution import router as signal_router
        from app.ingestion.telegram_listener import handle_incoming

        async with db.session_factory()() as session:
            session.add(Channel(tg_chat_id=-1001, title="Sig", enabled=True,
                                broker="paper", execution_mode="auto",
                                llm_fallback=False, sizing={"mode": "units", "value": 75}))
            await session.commit()

        await handle_incoming(-1001, 1, "BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130",
                              datetime.now(UTC), False,
                              on_message=signal_router.on_telegram_message)

        signals = (await client.get("/api/signals", headers=auth)).json()
        assert len(signals) == 1
        assert signals[0]["state"] == "executing"
        events = (await client.get("/api/events", headers=auth)).json()
        assert any(e["name"] == "placed" for e in events)
