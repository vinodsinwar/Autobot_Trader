"""Dashboard live feed: bus events over a websocket."""
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core import security
from app.core.eventbus import bus
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


@router.websocket("/api/ws")
async def event_stream(ws: WebSocket, token: str = Query(default="")):
    try:
        security.verify_token(token)
    except Exception:
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        async with bus.subscribe() as queue:
            while True:
                event = await queue.get()
                await ws.send_json(event.as_dict())
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws_stream_error")
        with contextlib.suppress(Exception):
            await ws.close()
