"""REST API for the dashboard."""
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth
from app.core import security
from app.db import audit
from app.db.models import (
    Channel,
    EventLog,
    Instrument,
    Position,
    Signal,
    SignalState,
)
from app.db.session import get_session, get_setting, set_setting
from app.execution import registry
from app.execution import router as signal_router
from app.parsing import pipeline
from app.reporting import pnl
from app.risk.engine import DEFAULT_RISK_SETTINGS, load_risk_settings

router = APIRouter(prefix="/api")
protected = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str


@router.post("/auth/login")
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)):
    stored = await get_setting(session, "auth", {}) or {}
    if not stored.get("password_hash") or not security.verify_password(
        body.password, stored["password_hash"]
    ):
        raise HTTPException(401, "wrong password")
    return {"token": security.create_token()}


class PasswordChange(BaseModel):
    new_password: str


@protected.post("/auth/change-password")
async def change_password(body: PasswordChange, session: AsyncSession = Depends(get_session)):
    if len(body.new_password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    await set_setting(session, "auth", {"password_hash": security.hash_password(body.new_password)})
    return {"ok": True}


# ---------------------------------------------------------------------------
# overview / reports
# ---------------------------------------------------------------------------

@protected.get("/overview")
async def overview(session: AsyncSession = Depends(get_session)):
    data = await pnl.overview(session)
    risk = await load_risk_settings(session)
    data["kill_switch"] = risk["kill_switch"]
    data["live_trading"] = risk["live_trading"]
    return data


@protected.get("/reports/daily")
async def report_daily(days: int = 30, session: AsyncSession = Depends(get_session)):
    return await pnl.daily_pnl_series(session, days)


@protected.get("/reports/sources")
async def report_sources(days: int = 90, session: AsyncSession = Depends(get_session)):
    return await pnl.per_source_stats(session, days)


@protected.get("/reports/trades.csv")
async def report_trades_csv(days: int = 365, session: AsyncSession = Depends(get_session)):
    return PlainTextResponse(await pnl.trades_csv(session, days), media_type="text/csv")


# ---------------------------------------------------------------------------
# signals / orders / positions / events
# ---------------------------------------------------------------------------

@protected.get("/signals")
async def list_signals(
    state: str | None = None, limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    q = select(Signal).order_by(Signal.id.desc()).limit(min(limit, 500))
    if state:
        q = q.where(Signal.state == state)
    rows = (await session.execute(q)).scalars().all()
    return [_signal_json(s) for s in rows]


def _signal_json(s: Signal) -> dict[str, Any]:
    return {
        "id": s.id, "source": s.source, "state": s.state, "parser": s.parser,
        "confidence": s.confidence, "broker": s.broker, "parsed": s.parsed,
        "error": s.error, "created_at": s.created_at.isoformat(),
        "raw_message_id": s.raw_message_id,
    }


@protected.post("/signals/{signal_id}/approve")
async def approve(signal_id: int, session: AsyncSession = Depends(get_session)):
    try:
        signal = await signal_router.approve_signal(session, signal_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _signal_json(signal)


@protected.post("/signals/{signal_id}/reject")
async def reject(signal_id: int, session: AsyncSession = Depends(get_session)):
    try:
        signal = await signal_router.reject_signal(session, signal_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _signal_json(signal)


@protected.get("/orders")
async def list_orders(limit: int = 100, session: AsyncSession = Depends(get_session)):
    return await pnl.recent_orders(session, limit)


@protected.get("/positions")
async def list_positions(
    status: str | None = None, limit: int = 200,
    session: AsyncSession = Depends(get_session),
):
    q = select(Position).order_by(Position.id.desc()).limit(min(limit, 500))
    if status:
        q = q.where(Position.status == status)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": p.id, "signal_id": p.signal_id, "broker": p.broker,
            "symbol": p.symbol, "side": p.side, "qty": p.qty,
            "avg_entry_price": p.avg_entry_price, "exit_price": p.exit_price,
            "realized_pnl": p.realized_pnl, "status": p.status,
            "opened_at": p.opened_at.isoformat(),
            "closed_at": p.closed_at.isoformat() if p.closed_at else None,
        }
        for p in rows
    ]


@protected.get("/events")
async def list_events(
    category: str | None = None, signal_id: int | None = None, limit: int = 200,
    session: AsyncSession = Depends(get_session),
):
    q = select(EventLog).order_by(EventLog.id.desc()).limit(min(limit, 1000))
    if category:
        q = q.where(EventLog.category == category)
    if signal_id:
        q = q.where(EventLog.signal_id == signal_id)
    rows = (await session.execute(q)).scalars().all()
    return [
        {"id": e.id, "ts": e.ts.isoformat(), "category": e.category,
         "name": e.name, "signal_id": e.signal_id, "payload": e.payload}
        for e in rows
    ]


# ---------------------------------------------------------------------------
# telegram channels
# ---------------------------------------------------------------------------

class ChannelBody(BaseModel):
    tg_chat_id: int | None = None
    identifier: str | None = None    # @username / t.me link — resolved via Telethon
    title: str = ""
    enabled: bool = True
    broker: str = "paper"
    execution_mode: str = "manual"
    rule_pack: str = "generic"
    llm_fallback: bool = True
    sizing: dict[str, Any] = {}


@protected.get("/channels")
async def list_channels(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Channel).order_by(Channel.id))).scalars().all()
    return [_channel_json(c) for c in rows]


def _channel_json(c: Channel) -> dict[str, Any]:
    return {
        "id": c.id, "tg_chat_id": c.tg_chat_id, "title": c.title,
        "enabled": c.enabled, "broker": c.broker, "execution_mode": c.execution_mode,
        "rule_pack": c.rule_pack, "llm_fallback": c.llm_fallback, "sizing": c.sizing,
    }


@protected.post("/channels")
async def create_channel(body: ChannelBody, session: AsyncSession = Depends(get_session)):
    chat_id, title = body.tg_chat_id, body.title
    if chat_id is None and body.identifier:
        from app.services import telegram_service
        if telegram_service is None or not telegram_service.connected:
            raise HTTPException(503, "telegram not connected; provide tg_chat_id directly")
        info = await telegram_service.resolve_chat(body.identifier)
        chat_id, title = info["chat_id"], title or info["title"]
    if chat_id is None:
        raise HTTPException(400, "tg_chat_id or identifier required")
    channel = Channel(
        tg_chat_id=chat_id, title=title, enabled=body.enabled, broker=body.broker,
        execution_mode=body.execution_mode, rule_pack=body.rule_pack,
        llm_fallback=body.llm_fallback, sizing=body.sizing,
    )
    session.add(channel)
    await session.commit()
    return _channel_json(channel)


@protected.patch("/channels/{channel_id}")
async def update_channel(
    channel_id: int, body: dict[str, Any], session: AsyncSession = Depends(get_session)
):
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(404, "no such channel")
    for field in ("title", "enabled", "broker", "execution_mode",
                  "rule_pack", "llm_fallback", "sizing"):
        if field in body:
            setattr(channel, field, body[field])
    await session.commit()
    return _channel_json(channel)


@protected.delete("/channels/{channel_id}")
async def delete_channel(channel_id: int, session: AsyncSession = Depends(get_session)):
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(404, "no such channel")
    channel.enabled = False  # soft-disable: raw history stays intact
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# parser test bench
# ---------------------------------------------------------------------------

class ParseTest(BaseModel):
    text: str
    rule_pack: str = "generic"
    use_llm: bool = False


@protected.post("/parse-test")
async def parse_test(body: ParseTest, session: AsyncSession = Depends(get_session)):
    llm_settings = await get_setting(session, "llm", {}) or {}
    result = await pipeline.parse_message(
        body.text, rule_pack=body.rule_pack,
        llm_fallback=body.use_llm, llm_settings=llm_settings,
    )
    return {
        "ok": result.ok,
        "parser": result.parser,
        "error": result.error,
        "latency_ms": round(result.latency_ms, 1),
        "signal": result.signal.model_dump(mode="json") if result.signal else None,
        "summary": result.signal.human_summary() if result.signal else None,
    }


# ---------------------------------------------------------------------------
# settings (risk / llm / stt / brokers / notifications)
# ---------------------------------------------------------------------------

SECRET_SETTINGS = {"dhan", "delta", "llm", "stt", "telegram"}
SECRET_FIELDS = {"access_token", "api_key", "api_secret", "session", "fast_api_key"}


def _mask(key: str, value: dict[str, Any]) -> dict[str, Any]:
    if key not in SECRET_SETTINGS or not isinstance(value, dict):
        return value
    return {
        k: ("•••" if k in SECRET_FIELDS and v else v)
        for k, v in value.items()
    }


@protected.get("/settings/{key}")
async def get_setting_api(key: str, session: AsyncSession = Depends(get_session)):
    defaults: dict[str, Any] = {"risk": DEFAULT_RISK_SETTINGS}.get(key, {})
    value = await get_setting(session, key, defaults) or defaults
    return {"key": key, "value": _mask(key, value), "secret": key in SECRET_SETTINGS}


@protected.put("/settings/{key}")
async def put_setting(
    key: str, body: dict[str, Any], session: AsyncSession = Depends(get_session)
):
    if key == "auth":
        raise HTTPException(400, "use /auth/change-password")
    # keep masked secrets unchanged: merge "•••" fields from the stored value
    stored = await get_setting(session, key, {}) or {}
    merged = {
        k: (stored.get(k) if v == "•••" else v)
        for k, v in body.items()
    }
    await set_setting(session, key, merged, secret=key in SECRET_SETTINGS)
    if key in ("dhan", "delta"):
        await registry.invalidate(key)
        if key == "dhan":
            merged["token_updated_at"] = datetime.now(UTC).isoformat()
            await set_setting(session, key, merged, secret=True)
    await audit.record(session, "system", "setting_changed", {"key": key})
    return {"ok": True}


@protected.post("/kill-switch")
async def kill_switch(body: dict[str, Any], session: AsyncSession = Depends(get_session)):
    risk = await load_risk_settings(session)
    risk["kill_switch"] = bool(body.get("on"))
    await set_setting(session, "risk", risk)
    await audit.record(session, "risk", "kill_switch",
                       {"on": risk["kill_switch"], "summary": "kill switch toggled"})
    return {"kill_switch": risk["kill_switch"]}


# ---------------------------------------------------------------------------
# brokers
# ---------------------------------------------------------------------------

@protected.get("/brokers/status")
async def broker_status(session: AsyncSession = Depends(get_session)):
    from app.services import telegram_service

    dhan_cfg = await get_setting(session, "dhan", {}) or {}
    delta_cfg = await get_setting(session, "delta", {}) or {}
    token_age_h = None
    if dhan_cfg.get("token_updated_at"):
        age = datetime.now(UTC) - datetime.fromisoformat(dhan_cfg["token_updated_at"])
        token_age_h = round(age.total_seconds() / 3600, 1)

    counts = {}
    for broker in ("dhan", "delta"):
        counts[broker] = (
            await session.execute(
                select(Instrument.id).where(Instrument.broker == broker).limit(1)
            )
        ).first() is not None

    return {
        "telegram_connected": bool(telegram_service and telegram_service.connected),
        "dhan": {
            "configured": bool(dhan_cfg.get("access_token")),
            "token_age_hours": token_age_h,
            "token_expires_in_hours": round(24 - token_age_h, 1) if token_age_h is not None else None,
            "instruments_synced": counts["dhan"],
        },
        "delta": {
            "configured": bool(delta_cfg.get("api_key")),
            "testnet": bool(delta_cfg.get("testnet")),
            "instruments_synced": counts["delta"],
        },
    }


@protected.post("/brokers/{broker}/sync-instruments")
async def sync_instruments(broker: str, session: AsyncSession = Depends(get_session)):
    if broker == "dhan":
        from app.instruments.dhan_scrip_master import sync_dhan_instruments
        count = await sync_dhan_instruments(session)
    elif broker == "delta":
        from app.execution.delta.client import DeltaClient
        from app.instruments.delta_products import sync_delta_instruments
        cfg = await get_setting(session, "delta", {}) or {}
        client = DeltaClient(cfg.get("api_key", ""), cfg.get("api_secret", ""))
        try:
            count = await sync_delta_instruments(session, client)
        finally:
            await client.close()
    else:
        raise HTTPException(404, "unknown broker")
    return {"synced": count}


# convenience: signals awaiting approval (badge polling)
@protected.get("/pending-approvals")
async def pending_approvals(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Signal).where(Signal.state == SignalState.AWAITING_APPROVAL.value)
            .order_by(Signal.id.desc())
        )
    ).scalars().all()
    return [_signal_json(s) for s in rows]
