"""Ingestion persistence: watched-channel filtering, edit versioning, callbacks."""
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import Channel, RawMessage
from app.ingestion.telegram_listener import handle_incoming

NOW = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)


async def make_channel(db, chat_id=-1001, enabled=True) -> Channel:
    async with db.session_factory()() as session:
        ch = Channel(tg_chat_id=chat_id, title="Test Signals", enabled=enabled)
        session.add(ch)
        await session.commit()
        await session.refresh(ch)
        return ch


async def test_message_from_watched_channel_persisted(db):
    await make_channel(db)
    raw = await handle_incoming(-1001, 42, "BUY NIFTY 25000 CE ABOVE 150", NOW, is_edit=False)
    assert raw is not None
    assert raw.edit_version == 0


async def test_unwatched_chat_ignored(db):
    await make_channel(db, chat_id=-1001)
    raw = await handle_incoming(-9999, 42, "BUY NIFTY 25000 CE", NOW, is_edit=False)
    assert raw is None


async def test_disabled_channel_ignored(db):
    await make_channel(db, enabled=False)
    raw = await handle_incoming(-1001, 42, "BUY NIFTY 25000 CE", NOW, is_edit=False)
    assert raw is None


async def test_edit_increments_version(db):
    await make_channel(db)
    await handle_incoming(-1001, 42, "BUY NIFTY 25000 CE SL 130", NOW, is_edit=False)
    raw2 = await handle_incoming(-1001, 42, "BUY NIFTY 25000 CE SL 135", NOW, is_edit=True)
    assert raw2.edit_version == 1

    async with db.session_factory()() as session:
        rows = (await session.execute(select(RawMessage))).scalars().all()
    assert len(rows) == 2  # both versions kept verbatim


async def test_identical_edit_dropped(db):
    await make_channel(db)
    await handle_incoming(-1001, 42, "same text", NOW, is_edit=False)
    raw2 = await handle_incoming(-1001, 42, "same text", NOW, is_edit=True)
    assert raw2 is None


async def test_edit_of_unseen_message_treated_as_new(db):
    await make_channel(db)
    raw = await handle_incoming(-1001, 77, "BUY NIFTY 25000 CE", NOW, is_edit=True)
    assert raw is not None
    assert raw.edit_version == 0


async def test_callback_receives_message_and_channel(db):
    await make_channel(db)
    seen = []

    async def cb(raw, channel, is_edit):
        seen.append((raw.text, channel.title, is_edit))

    await handle_incoming(-1001, 1, "hello signal", NOW, is_edit=False, on_message=cb)
    assert seen == [("hello signal", "Test Signals", False)]


async def test_empty_message_ignored(db):
    await make_channel(db)
    assert await handle_incoming(-1001, 5, "   ", NOW, is_edit=False) is None
