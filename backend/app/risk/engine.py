"""Risk engine: every signal passes these gates before any order is placed.

All limits are dashboard-editable (settings key "risk"). Decisions are
returned with machine-readable reasons and audit-logged by the caller.
"""
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Position, PositionStatus, Signal, SignalState
from app.db.session import get_setting
from app.parsing.schema import ParsedSignal

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_RISK_SETTINGS: dict[str, Any] = {
    "kill_switch": False,
    "live_trading": False,           # false = every order routed to the paper broker
    "max_trades_per_day": 10,
    "max_concurrent_positions": 3,
    "max_capital_per_trade": 50_000.0,   # notional, in account currency
    "daily_loss_limit": 5_000.0,         # positive number; halt when realized loss exceeds it
    "dedup_window_minutes": 30,
    "trading_hours": {
        # broker -> [start, end] local trading window; crypto is 24x7
        "dhan": ["09:15", "15:25"],
        "delta": ["00:00", "23:59"],
        "paper": ["00:00", "23:59"],
    },
    "weekend_block": ["dhan"],       # brokers blocked on Sat/Sun
}


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


async def load_risk_settings(session: AsyncSession) -> dict[str, Any]:
    stored = await get_setting(session, "risk", {}) or {}
    return {**DEFAULT_RISK_SETTINGS, **stored}


def _within_hours(cfg: dict[str, Any], broker: str, now: datetime) -> bool:
    local = now.astimezone(IST)
    if broker in cfg.get("weekend_block", []) and local.weekday() >= 5:
        return False
    window = (cfg.get("trading_hours") or {}).get(broker)
    if not window:
        return True
    start = time.fromisoformat(window[0])
    end = time.fromisoformat(window[1])
    return start <= local.time() <= end


async def check(
    session: AsyncSession,
    parsed: ParsedSignal,
    broker: str,
    now: datetime | None = None,
    settings_override: dict[str, Any] | None = None,
    exclude_signal_id: int | None = None,
) -> RiskDecision:
    now = now or datetime.now(UTC)
    cfg = settings_override or await load_risk_settings(session)
    reasons: list[str] = []

    if cfg["kill_switch"]:
        reasons.append("kill switch is ON")

    if not _within_hours(cfg, broker, now):
        reasons.append(f"outside trading hours for {broker}")

    # duplicate suppression: same trade-relevant content seen recently
    window_start = now - timedelta(minutes=float(cfg["dedup_window_minutes"]))
    dup_query = select(func.count(Signal.id)).where(
                Signal.dedup_hash == parsed.dedup_hash(),
                Signal.created_at >= window_start,
                Signal.state.notin_([
                    SignalState.PARSE_FAILED.value,
                    SignalState.RISK_REJECTED.value,
                    SignalState.ERROR.value,
                ]),
            )
    if exclude_signal_id is not None:
        dup_query = dup_query.where(Signal.id != exclude_signal_id)
    dup = (await session.execute(dup_query)).scalar() or 0
    if dup > 0:
        reasons.append(f"duplicate signal within {cfg['dedup_window_minutes']}m window")

    open_positions = (
        await session.execute(
            select(func.count(Position.id)).where(Position.status == PositionStatus.OPEN.value)
        )
    ).scalar() or 0
    if parsed.kind.value == "ENTRY" and open_positions >= int(cfg["max_concurrent_positions"]):
        reasons.append(f"max concurrent positions reached ({open_positions})")

    day_start = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    executed_today = (
        await session.execute(
            select(func.count(Signal.id)).where(
                Signal.created_at >= day_start.astimezone(UTC),
                Signal.state.in_([
                    SignalState.EXECUTING.value,
                    SignalState.OPEN.value,
                    SignalState.CLOSED.value,
                ]),
            )
        )
    ).scalar() or 0
    if parsed.kind.value == "ENTRY" and executed_today >= int(cfg["max_trades_per_day"]):
        reasons.append(f"max trades per day reached ({executed_today})")

    realized_today = (
        await session.execute(
            select(func.coalesce(func.sum(Position.realized_pnl), 0.0)).where(
                Position.closed_at >= day_start.astimezone(UTC)
            )
        )
    ).scalar() or 0.0
    if realized_today <= -abs(float(cfg["daily_loss_limit"])):
        reasons.append(
            f"daily loss limit hit (realized {realized_today:.0f}, limit {cfg['daily_loss_limit']})"
        )

    return RiskDecision(approved=not reasons, reasons=reasons)
