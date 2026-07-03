"""Telegram self-notifications: pushes important events to your Saved Messages
or a private group via the same Telethon session."""
import asyncio
import contextlib

from app.core.eventbus import Event, bus
from app.core.logging import get_logger
from app.db.session import get_setting, session_factory

log = get_logger(__name__)

DEFAULT_TOPICS = {
    "order.placed": "🟢 Order placed",
    "order.placement_failed": "🔴 Order FAILED",
    "order.exit_placed": "🟠 Exit placed",
    "position.opened": "📈 Position opened",
    "position.closed": "🏁 Position closed",
    "position.closed_by_exit": "🏁 Position closed (exit signal)",
    "risk.rejected": "⛔ Signal blocked by risk engine",
    "signal.awaiting_approval": "⏳ Signal awaiting your approval",
    "youtube.signal_extracted": "🎥 YouTube signal detected",
}


def format_event(event: Event) -> str | None:
    label = DEFAULT_TOPICS.get(event.topic)
    if label is None:
        return None
    p = event.payload
    detail_keys = ("summary", "symbol", "reasons", "error", "pnl", "exit_price", "broker")
    details = [f"{k}: {p[k]}" for k in detail_keys if p.get(k) not in (None, "", [])]
    return f"{label}\n" + "\n".join(details) if details else label


class NotifierService:
    def __init__(self, send_fn) -> None:
        """send_fn: async (chat_id: int, text: str) -> Any — TelegramService.send_message."""
        self._send = send_fn
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async with bus.subscribe() as queue:
            while True:
                event = await queue.get()
                try:
                    await self._maybe_notify(event)
                except Exception:
                    log.exception("notifier_error", topic=event.topic)

    async def _maybe_notify(self, event: Event) -> None:
        text = format_event(event)
        if text is None:
            return
        async with session_factory()() as session:
            cfg = await get_setting(session, "notifications", {}) or {}
        if not cfg.get("enabled") or not cfg.get("chat_id"):
            return
        muted = set(cfg.get("muted_topics", []))
        if event.topic in muted:
            return
        await self._send(int(cfg["chat_id"]), text)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
