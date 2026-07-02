"""Telegram ingestion via a Telethon (MTProto) user session.

The user session can read any channel the account has joined — no cooperation
from signal providers needed. Credentials and the StringSession are stored
encrypted in the settings table (key: "telegram").

`handle_incoming` is deliberately free of Telethon types so the persistence
and edit-versioning logic is unit-testable without a live connection.
"""
import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.db import audit
from app.db.models import Channel, RawMessage
from app.db.session import get_setting, session_factory

log = get_logger(__name__)

# Signature: (raw_message, channel, is_edit) -> None. Wired to the signal processor.
MessageCallback = Callable[[RawMessage, Channel, bool], Awaitable[None]]

TELEGRAM_SETTINGS_KEY = "telegram"  # {"api_id": int, "api_hash": str, "session": str, "relay_chat_id": int|None}


async def handle_incoming(
    chat_id: int,
    message_id: int,
    text: str,
    posted_at: datetime,
    is_edit: bool,
    on_message: MessageCallback | None = None,
) -> RawMessage | None:
    """Persist an inbound message/edit and fan out to the pipeline.

    Returns the stored RawMessage, or None if the chat isn't a watched channel
    or the payload is empty/duplicate.
    """
    if not text or not text.strip():
        return None
    async with session_factory()() as session:
        channel = (
            await session.execute(select(Channel).where(Channel.tg_chat_id == chat_id))
        ).scalar_one_or_none()
        if channel is None or not channel.enabled:
            return None

        version = 0
        if is_edit:
            latest = (
                await session.execute(
                    select(func.max(RawMessage.edit_version)).where(
                        RawMessage.channel_id == channel.id,
                        RawMessage.tg_message_id == message_id,
                    )
                )
            ).scalar()
            if latest is None:
                # edit of a message we never saw — treat as first sighting
                version = 0
                is_edit = False
            else:
                previous = (
                    await session.execute(
                        select(RawMessage.text).where(
                            RawMessage.channel_id == channel.id,
                            RawMessage.tg_message_id == message_id,
                            RawMessage.edit_version == latest,
                        )
                    )
                ).scalar()
                if previous == text:
                    return None  # formatting-only edit, nothing new
                version = latest + 1

        raw = RawMessage(
            channel_id=channel.id,
            tg_message_id=message_id,
            edit_version=version,
            text=text,
            posted_at=posted_at,
        )
        session.add(raw)
        await session.flush()
        await audit.record(
            session,
            "telegram",
            "message_edited" if is_edit else "message_received",
            {
                "channel": channel.title,
                "chat_id": chat_id,
                "raw_message_id": raw.id,
                "text": text[:500],
                "edit_version": version,
            },
        )
        if on_message is not None:
            await on_message(raw, channel, is_edit)
        return raw


class TelegramService:
    """Owns the Telethon client: inbound listening and outbound sends."""

    def __init__(self, on_message: MessageCallback | None = None) -> None:
        self.on_message = on_message
        self.client: Any = None
        self._task: asyncio.Task | None = None
        self.connected = False

    async def _load_config(self) -> dict[str, Any] | None:
        async with session_factory()() as session:
            cfg = await get_setting(session, TELEGRAM_SETTINGS_KEY)
        if not cfg or not cfg.get("session"):
            return None
        return cfg

    async def start(self) -> None:
        cfg = await self._load_config()
        if cfg is None:
            log.warning("telegram_not_configured",
                        hint="run `python -m app.ingestion.tg_login` or configure from dashboard")
            return

        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        self.client = TelegramClient(
            StringSession(cfg["session"]), int(cfg["api_id"]), cfg["api_hash"]
        )

        @self.client.on(events.NewMessage())
        async def _on_new(event: Any) -> None:
            await self._safe_handle(event, is_edit=False)

        @self.client.on(events.MessageEdited())
        async def _on_edit(event: Any) -> None:
            await self._safe_handle(event, is_edit=True)

        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.error("telegram_session_invalid", hint="re-run login helper")
            return
        self.connected = True
        me = await self.client.get_me()
        log.info("telegram_connected", user=getattr(me, "username", None) or me.id)
        self._task = asyncio.create_task(self.client.run_until_disconnected())

    async def _safe_handle(self, event: Any, is_edit: bool) -> None:
        try:
            message = event.message
            posted = message.date or datetime.now(UTC)
            await handle_incoming(
                chat_id=event.chat_id,
                message_id=message.id,
                text=message.raw_text or "",
                posted_at=posted,
                is_edit=is_edit,
                on_message=self.on_message,
            )
        except Exception:
            log.exception("telegram_handler_error")

    async def send_message(self, chat_id: int, text: str) -> int | None:
        """Send a message (relay/notifications). Returns the Telegram message id."""
        if self.client is None or not self.connected:
            log.warning("telegram_send_skipped_not_connected")
            return None
        msg = await self.client.send_message(chat_id, text)
        return msg.id

    async def resolve_chat(self, identifier: str) -> dict[str, Any]:
        """Resolve a t.me link / @username / numeric id to chat metadata."""
        if self.client is None or not self.connected:
            raise RuntimeError("telegram not connected")
        entity = await self.client.get_entity(identifier)
        chat_id = entity.id
        # Telethon channel ids are positive; bot-api style is -100<id>
        if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
            chat_id = int(f"-100{entity.id}")
        return {"chat_id": chat_id, "title": getattr(entity, "title", "") or getattr(entity, "username", "")}

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.client is not None:
            await self.client.disconnect()
        self.connected = False
