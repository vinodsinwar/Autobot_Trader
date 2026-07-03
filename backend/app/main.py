"""Application entrypoint: FastAPI app + background trading services."""
import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import services
from app.api import routes as api_routes
from app.api import ws as api_ws
from app.api import youtube_routes
from app.core import security
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import get_setting, init_db, session_factory, set_setting

log = get_logger(__name__)

_background: list[asyncio.Task] = []


async def _bootstrap_auth() -> None:
    settings = get_settings()
    async with session_factory()() as session:
        stored = await get_setting(session, "auth", {}) or {}
        if not stored.get("password_hash"):
            await set_setting(
                session, "auth",
                {"password_hash": security.hash_password(settings.admin_password)},
            )
            log.info("auth_bootstrapped", hint="password from AUTOBOT_ADMIN_PASSWORD")


async def _daily_scrip_sync() -> None:
    from app.instruments.dhan_scrip_master import sync_dhan_instruments

    while True:
        try:
            async with session_factory()() as session:
                await sync_dhan_instruments(session)
        except Exception:
            log.exception("scrip_sync_failed")
        await asyncio.sleep(24 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    await init_db()
    await _bootstrap_auth()

    if settings.enable_telegram:
        from app.execution.router import on_telegram_message
        from app.ingestion.telegram_listener import TelegramService

        services.telegram_service = TelegramService(on_message=on_telegram_message)
        await services.telegram_service.start()

        from app.reporting.notifier import NotifierService

        services.notifier_service = NotifierService(services.telegram_service.send_message)
        services.notifier_service.start()

    if settings.enable_tracker:
        from app.execution import registry
        from app.execution.tracker import TrackerService, reconcile

        services.tracker_service = TrackerService()
        services.tracker_service.watch("paper", registry.get_paper())
        async with session_factory()() as session:
            for broker in ("dhan", "delta"):
                cfg = await get_setting(session, broker, {}) or {}
                if cfg.get("api_key") or cfg.get("access_token"):
                    try:
                        adapter = await registry.get_adapter(session, broker)
                        services.tracker_service.watch(broker, adapter)
                    except Exception:
                        log.exception("tracker_start_failed", broker=broker)
        await reconcile()

    if settings.enable_scrip_sync:
        _background.append(asyncio.create_task(_daily_scrip_sync()))

    if settings.enable_youtube:
        from app.youtube.manager import YouTubeManager

        services.youtube_manager = YouTubeManager()
        await services.youtube_manager.start()

    log.info("autobot_started",
             telegram=settings.enable_telegram, tracker=settings.enable_tracker,
             youtube=settings.enable_youtube)
    yield

    for task in _background:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for svc in (services.youtube_manager, services.notifier_service,
                services.tracker_service, services.telegram_service):
        if svc is not None:
            with contextlib.suppress(Exception):
                await svc.stop()


app = FastAPI(title="Autobot Trader", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_routes.router)
app.include_router(api_routes.protected)
app.include_router(youtube_routes.router)
app.include_router(api_ws.router)

@app.get("/healthz")
async def healthz():
    return {"ok": True}


# serve the built dashboard when present (docker image copies it here);
# registered last so API routes always win
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="dashboard")
