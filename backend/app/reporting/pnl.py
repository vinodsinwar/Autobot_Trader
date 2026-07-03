"""P&L and activity reporting queries."""
import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Position, PositionStatus, Signal, SignalState
from app.risk.engine import IST


def _day_bounds(now: datetime) -> datetime:
    return now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


async def overview(session: AsyncSession) -> dict[str, Any]:
    now = datetime.now(UTC)
    day_start = _day_bounds(now)

    realized_today = (
        await session.execute(
            select(func.coalesce(func.sum(Position.realized_pnl), 0.0)).where(
                Position.closed_at >= day_start
            )
        )
    ).scalar() or 0.0
    realized_total = (
        await session.execute(select(func.coalesce(func.sum(Position.realized_pnl), 0.0)))
    ).scalar() or 0.0
    open_positions = (
        await session.execute(
            select(func.count(Position.id)).where(Position.status == PositionStatus.OPEN.value)
        )
    ).scalar() or 0
    signals_today = (
        await session.execute(
            select(func.count(Signal.id)).where(Signal.created_at >= day_start)
        )
    ).scalar() or 0
    awaiting = (
        await session.execute(
            select(func.count(Signal.id)).where(
                Signal.state == SignalState.AWAITING_APPROVAL.value
            )
        )
    ).scalar() or 0
    closed = (
        await session.execute(
            select(func.count(Position.id)).where(Position.status == PositionStatus.CLOSED.value)
        )
    ).scalar() or 0
    wins = (
        await session.execute(
            select(func.count(Position.id)).where(
                Position.status == PositionStatus.CLOSED.value, Position.realized_pnl > 0
            )
        )
    ).scalar() or 0

    return {
        "realized_pnl_today": round(realized_today, 2),
        "realized_pnl_total": round(realized_total, 2),
        "open_positions": open_positions,
        "signals_today": signals_today,
        "awaiting_approval": awaiting,
        "closed_trades": closed,
        "win_rate": round(wins / closed, 3) if closed else None,
    }


async def daily_pnl_series(session: AsyncSession, days: int = 30) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(Position.closed_at, Position.realized_pnl).where(
                Position.status == PositionStatus.CLOSED.value,
                Position.closed_at >= since,
            )
        )
    ).all()
    buckets: dict[str, dict[str, Any]] = {}
    for closed_at, pnl in rows:
        if closed_at is None:
            continue
        day = closed_at.astimezone(IST).strftime("%Y-%m-%d")
        b = buckets.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
        b["pnl"] = round(b["pnl"] + (pnl or 0.0), 2)
        b["trades"] += 1
        b["wins"] += 1 if (pnl or 0) > 0 else 0
    series = sorted(buckets.values(), key=lambda b: b["date"])
    cumulative = 0.0
    for b in series:
        cumulative = round(cumulative + b["pnl"], 2)
        b["cumulative"] = cumulative
    return series


async def per_source_stats(session: AsyncSession, days: int = 90) -> list[dict[str, Any]]:
    """Win-rate / P&L per signal source and channel."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(Signal.source, Signal.parser, Position.realized_pnl)
            .join(Position, Position.signal_id == Signal.id)
            .where(Position.status == PositionStatus.CLOSED.value, Position.closed_at >= since)
        )
    ).all()
    agg: dict[str, dict[str, Any]] = {}
    for source, _parser, pnl in rows:
        key = source or "unknown"
        a = agg.setdefault(key, {"source": key, "trades": 0, "wins": 0, "pnl": 0.0})
        a["trades"] += 1
        a["wins"] += 1 if (pnl or 0) > 0 else 0
        a["pnl"] = round(a["pnl"] + (pnl or 0.0), 2)
    for a in agg.values():
        a["win_rate"] = round(a["wins"] / a["trades"], 3) if a["trades"] else None
    return sorted(agg.values(), key=lambda a: -a["pnl"])


async def trades_csv(session: AsyncSession, days: int = 365) -> str:
    """Closed-trade export for spreadsheets / tax records."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(Position, Signal)
            .outerjoin(Signal, Position.signal_id == Signal.id)
            .where(Position.status == PositionStatus.CLOSED.value, Position.closed_at >= since)
            .order_by(Position.closed_at)
        )
    ).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "closed_at", "broker", "symbol", "side", "qty", "entry_price",
        "exit_price", "realized_pnl", "source", "parser", "signal_summary",
    ])
    for pos, sig in rows:
        writer.writerow([
            pos.closed_at.isoformat() if pos.closed_at else "",
            pos.broker, pos.symbol, pos.side, abs(pos.qty),
            pos.avg_entry_price, pos.exit_price, round(pos.realized_pnl, 2),
            sig.source if sig else "", sig.parser if sig else "",
            (sig.parsed or {}).get("notes", "") if sig else "",
        ])
    return buf.getvalue()


async def recent_orders(session: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
    rows = (
        await session.execute(select(Order).order_by(Order.id.desc()).limit(limit))
    ).scalars().all()
    return [
        {
            "id": o.id, "signal_id": o.signal_id, "broker": o.broker,
            "broker_order_id": o.broker_order_id, "symbol": o.symbol,
            "side": o.side, "qty": o.qty, "price": o.price,
            "order_type": o.order_type, "status": o.status,
            "avg_fill_price": o.avg_fill_price,
            "created_at": o.created_at.isoformat(),
        }
        for o in rows
    ]
