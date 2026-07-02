"""One-time interactive Telegram login helper.

Usage:
    python -m app.ingestion.tg_login

Walks through api_id/api_hash/phone/OTP, then stores the resulting
StringSession encrypted in the settings table. Get api_id/api_hash from
https://my.telegram.org → API Development Tools.
"""
import asyncio

from app.db.session import init_db, session_factory, set_setting
from app.ingestion.telegram_listener import TELEGRAM_SETTINGS_KEY


async def main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    print("== Autobot Trader: Telegram login ==")
    # blocking input is fine: this is a one-time interactive CLI, nothing else runs
    api_id = int(input("api_id: ").strip())  # noqa: ASYNC250
    api_hash = input("api_hash: ").strip()  # noqa: ASYNC250

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()  # prompts for phone + code (+ 2FA password if set)
    session_str = client.session.save()
    me = await client.get_me()
    await client.disconnect()

    await init_db()
    async with session_factory()() as session:
        await set_setting(
            session,
            TELEGRAM_SETTINGS_KEY,
            {"api_id": api_id, "api_hash": api_hash, "session": session_str},
            secret=True,
        )
    print(f"Logged in as {me.username or me.id}. Session stored (encrypted).")
    print("Set AUTOBOT_ENABLE_TELEGRAM=true and restart the app.")


if __name__ == "__main__":
    asyncio.run(main())
