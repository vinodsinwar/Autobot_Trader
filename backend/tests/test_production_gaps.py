"""Regression tests for the pre-production audit fixes."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core import security
from app.db.models import EventLog, Order, Position, Signal
from app.db.session import set_setting
from app.execution import registry, tracker


@pytest.fixture(autouse=True)
async def clean_registry():
    await registry.invalidate()
    yield
    await registry.invalidate()


@pytest.fixture
async def client(db):
    from app.api import routes
    from app.main import app

    routes._LOGIN_FAILS.clear()
    async with db.session_factory()() as session:
        await set_setting(session, "auth",
                          {"password_hash": security.hash_password("pw12345678")})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    routes._LOGIN_FAILS.clear()


class TestEnvResolution:
    def test_env_file_includes_repo_root(self):
        """Server launched from backend/ must still read the repo-root .env."""
        from app.core.config import Settings

        files = Settings.model_config.get("env_file")
        assert "../.env" in tuple(files)


class TestLoginBruteForce:
    async def test_lockout_after_five_failures(self, client):
        for _ in range(5):
            resp = await client.post("/api/auth/login", json={"password": "wrong"})
            assert resp.status_code == 401
        resp = await client.post("/api/auth/login", json={"password": "wrong"})
        assert resp.status_code == 429
        # even the RIGHT password is refused during lockout
        resp = await client.post("/api/auth/login", json={"password": "pw12345678"})
        assert resp.status_code == 429

    async def test_success_clears_counter(self, client):
        for _ in range(3):
            await client.post("/api/auth/login", json={"password": "wrong"})
        resp = await client.post("/api/auth/login", json={"password": "pw12345678"})
        assert resp.status_code == 200


class TestDhanTokenCountdown:
    async def test_masked_resave_keeps_token_age(self, db, client):
        resp = await client.post("/api/auth/login", json={"password": "pw12345678"})
        auth = {"Authorization": f"Bearer {resp.json()['token']}"}

        await client.put("/api/settings/dhan", headers=auth,
                         json={"client_id": "1", "access_token": "real-token"})
        from app.db.session import get_setting
        async with db.session_factory()() as session:
            first = (await get_setting(session, "dhan"))["token_updated_at"]

        # editing client_id while token stays masked must NOT reset the countdown
        await client.put("/api/settings/dhan", headers=auth,
                         json={"client_id": "2", "access_token": "•••"})
        async with db.session_factory()() as session:
            stored = await get_setting(session, "dhan")
        assert stored["token_updated_at"] == first
        assert stored["access_token"] == "real-token"

        # a genuinely new token DOES reset it
        await client.put("/api/settings/dhan", headers=auth,
                         json={"client_id": "2", "access_token": "fresh-token"})
        async with db.session_factory()() as session:
            stored = await get_setting(session, "dhan")
        assert stored["token_updated_at"] != first


class TestDeltaReconcile:
    async def test_missing_delta_order_flagged_not_errored(self, db):
        """Delta lists only open orders — absence must not destroy local state."""
        async with db.session_factory()() as session:
            await set_setting(session, "delta",
                              {"api_key": "k", "api_secret": "s"}, secret=True)
            sig = Signal(source="telegram", state="executing", broker="delta")
            session.add(sig)
            await session.flush()
            session.add(Order(signal_id=sig.id, broker="delta", correlation_id="c1",
                              broker_order_id="999", symbol="BTCUSD", side="BUY",
                              qty=10, status="placed"))
            await session.commit()

        from unittest.mock import AsyncMock, patch
        with patch("app.execution.delta.adapter.DeltaAdapter.get_orders",
                   new=AsyncMock(return_value=[])):
            await tracker.reconcile()

        async with db.session_factory()() as session:
            order = (await session.execute(select(Order))).scalar_one()
            events = (await session.execute(select(EventLog))).scalars().all()
        assert order.status == "placed"  # unchanged, not "error"
        assert any(e.name == "reconcile_unresolved" for e in events)


class TestPositionMismatchCheck:
    async def test_locally_open_but_absent_at_broker_raises_event(self, db):
        async with db.session_factory()() as session:
            await set_setting(session, "delta",
                              {"api_key": "k", "api_secret": "s"}, secret=True)
            session.add(Position(broker="delta", symbol="BTCUSD", security_id="27",
                                 side="BUY", qty=10, avg_entry_price=65000.0))
            await session.commit()

        from unittest.mock import AsyncMock, patch
        with patch("app.execution.delta.adapter.DeltaAdapter.get_positions",
                   new=AsyncMock(return_value=[])):
            await tracker.check_position_mismatches()

        async with db.session_factory()() as session:
            events = (await session.execute(select(EventLog))).scalars().all()
            pos = (await session.execute(select(Position))).scalar_one()
        assert any(e.name == "mismatch_detected" for e in events)
        assert pos.status == "open"  # never silently rewritten

    async def test_paper_positions_ignored(self, db):
        async with db.session_factory()() as session:
            session.add(Position(broker="paper", symbol="X", security_id="1",
                                 side="BUY", qty=1))
            await session.commit()
        await tracker.check_position_mismatches()  # must not raise / hit brokers
