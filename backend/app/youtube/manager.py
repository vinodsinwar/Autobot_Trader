"""YouTube service: per-channel live pollers + capture sessions + VOD worker."""
import asyncio
import contextlib

from sqlalchemy import select

from app.core.eventbus import bus
from app.core.logging import get_logger
from app.db import audit
from app.db.models import YtChannel, YtStream, YtTranscriptSegment, utcnow
from app.db.session import get_setting, session_factory
from app.youtube import capture, relay
from app.youtube.extractor import LiveExtractor
from app.youtube.stt.base import STTConfigError, get_engine
from app.youtube.vod import process_vod

log = get_logger(__name__)

VOD_POLL_SECONDS = 10


class YouTubeManager:
    def __init__(self) -> None:
        self._pollers: dict[int, asyncio.Task] = {}
        self._vod_task: asyncio.Task | None = None
        self.live_streams: dict[int, int] = {}  # yt_channel_id -> yt_stream_id

    async def start(self) -> None:
        await self.refresh_channels()
        self._vod_task = asyncio.create_task(self._vod_worker())
        log.info("youtube_manager_started", channels=len(self._pollers))

    async def refresh_channels(self) -> None:
        """Sync poller tasks with enabled channels (called after CRUD changes)."""
        async with session_factory()() as session:
            channels = (
                await session.execute(select(YtChannel).where(YtChannel.enabled))
            ).scalars().all()
        wanted = {c.id for c in channels}
        for cid in list(self._pollers):
            if cid not in wanted:
                self._pollers.pop(cid).cancel()
        for c in channels:
            if c.id not in self._pollers or self._pollers[c.id].done():
                self._pollers[c.id] = asyncio.create_task(
                    self._poll_channel(c.id, c.channel_id, c.poll_interval_s)
                )

    async def _poll_channel(self, yt_channel_id: int, channel_id: str, interval: int) -> None:
        while True:
            try:
                live = await capture.check_live(channel_id)
                if live is not None:
                    await self._capture_stream(yt_channel_id, live)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("yt_poll_error", channel=channel_id)
            await asyncio.sleep(max(interval, 30))

    async def _capture_stream(self, yt_channel_id: int, live: dict[str, str]) -> None:
        async with session_factory()() as session:
            yt_channel = await session.get(YtChannel, yt_channel_id)
            stt_cfg = await get_setting(session, "stt", {}) or {}
            llm_cfg = await get_setting(session, "llm", {}) or {}
            stream = YtStream(
                yt_channel_id=yt_channel_id, video_id=live["video_id"],
                kind="live", title=live["title"], status="capturing",
                started_at=utcnow(),
            )
            session.add(stream)
            await session.commit()
            stream_id = stream.id
            calibration = dict(yt_channel.calibration or {})
            title = yt_channel.title or yt_channel.handle
            await audit.record(session, "youtube", "live_started",
                               {"channel": title, "video": live["title"],
                                "stream_id": stream_id})

        try:
            engine = get_engine(stt_cfg)
        except STTConfigError as exc:
            log.warning("yt_capture_blocked", error=str(exc))
            async with session_factory()() as session:
                s = await session.get(YtStream, stream_id)
                s.status = "error"
                s.error = str(exc)
                await session.commit()
            return

        self.live_streams[yt_channel_id] = stream_id
        extractor = LiveExtractor(llm_cfg, calibration)
        segment_count = 0
        try:
            pcm = capture.stream_live_pcm(live["audio_url"])
            async for seg in engine.stream(pcm):
                segment_count += 1
                async with session_factory()() as session:
                    session.add(YtTranscriptSegment(
                        stream_id=stream_id, t_start=seg.t_start,
                        t_end=seg.t_end, text=seg.text,
                    ))
                    await session.commit()
                bus.publish("youtube.transcript",
                            {"stream_id": stream_id, "t": seg.t_start, "text": seg.text})
                candidate = await extractor.on_segment(seg)
                if candidate is not None:
                    await relay.emit(stream_id, candidate)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("yt_capture_error", stream_id=stream_id)
        finally:
            self.live_streams.pop(yt_channel_id, None)
            async with session_factory()() as session:
                s = await session.get(YtStream, stream_id)
                if s is not None:
                    s.status = "done" if s.status == "capturing" else s.status
                    s.ended_at = utcnow()
                    s.stats = {**(s.stats or {}), "segments": segment_count}
                    await session.commit()
                    await audit.record(session, "youtube", "live_ended",
                                       {"stream_id": stream_id, "segments": segment_count})

    async def _vod_worker(self) -> None:
        while True:
            try:
                async with session_factory()() as session:
                    pending = (
                        await session.execute(
                            select(YtStream)
                            .where(YtStream.kind == "vod", YtStream.status == "pending")
                            .order_by(YtStream.id).limit(1)
                        )
                    ).scalar_one_or_none()
                if pending is not None:
                    await process_vod(pending.id)
                    continue  # immediately look for the next one
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vod_worker_error")
            await asyncio.sleep(VOD_POLL_SECONDS)

    async def stop(self) -> None:
        tasks = list(self._pollers.values())
        if self._vod_task:
            tasks.append(self._vod_task)
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._pollers.clear()
