"""Append-only audit log + event bus fan-out in one call."""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.eventbus import bus
from app.db.models import EventLog


async def record(
    session: AsyncSession,
    category: str,
    name: str,
    payload: dict[str, Any] | None = None,
    signal_id: int | None = None,
    commit: bool = True,
) -> None:
    payload = payload or {}
    session.add(EventLog(category=category, name=name, payload=payload, signal_id=signal_id))
    if commit:
        await session.commit()
    bus.publish(f"{category}.{name}", {**payload, "signal_id": signal_id})
