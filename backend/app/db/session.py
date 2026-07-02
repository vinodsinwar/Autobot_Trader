"""Async engine / session factory, plus the settings-table helper."""
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import security
from app.core.config import get_settings
from app.db.models import Base, Setting

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, echo=False)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_factory()() as session:
        yield session


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


# ---------------------------------------------------------------------------
# settings-table helpers (dashboard-editable config, secrets encrypted at rest)
# ---------------------------------------------------------------------------

async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    if row is None:
        return default
    value = row.value
    if row.encrypted and isinstance(value, dict) and "_enc" in value:
        import json

        return json.loads(security.decrypt(value["_enc"]))
    return value


async def set_setting(session: AsyncSession, key: str, value: Any, secret: bool = False) -> None:
    if secret:
        import json

        stored: dict[str, Any] = {"_enc": security.encrypt(json.dumps(value))}
    else:
        stored = value
    row = await session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=stored, encrypted=secret)
        session.add(row)
    else:
        row.value = stored
        row.encrypted = secret
    await session.commit()
