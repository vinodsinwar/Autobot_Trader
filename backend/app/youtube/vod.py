"""VOD processing for the calibration studio: download → batch STT →
windowed extraction → candidates for human review. Never executes trades."""
import tempfile
from typing import Any

from app.core.logging import get_logger
from app.db import audit
from app.db.models import YtChannel, YtExtractedSignal, YtStream, YtTranscriptSegment, utcnow
from app.db.session import get_setting, session_factory
from app.parsing.llm_engine import parse_with_llm
from app.youtube import capture
from app.youtube.extractor import _SYSTEM_CONTEXT, WINDOW_SECONDS
from app.youtube.stt.base import TranscriptSegment, get_engine

log = get_logger(__name__)


async def queue_vod(yt_channel_id: int, url: str) -> int:
    """Register a VOD for processing; returns the yt_streams row id."""
    info = await capture.resolve_vod(url)
    async with session_factory()() as session:
        stream = YtStream(
            yt_channel_id=yt_channel_id,
            video_id=info["video_id"],
            kind="vod",
            title=info["title"],
            status="pending",
            stats={"url": url},
        )
        session.add(stream)
        await session.commit()
        return stream.id


async def process_vod(stream_id: int) -> None:
    """Full VOD pipeline. Status: pending → transcribing → extracting → review."""
    async with session_factory()() as session:
        stream = await session.get(YtStream, stream_id)
        if stream is None or stream.kind != "vod":
            return
        yt_channel = await session.get(YtChannel, stream.yt_channel_id)
        url = (stream.stats or {}).get("url") or f"https://www.youtube.com/watch?v={stream.video_id}"
        stt_cfg = await get_setting(session, "stt", {}) or {}
        llm_cfg = await get_setting(session, "llm", {}) or {}
        calibration = dict(yt_channel.calibration or {}) if yt_channel else {}
        stream.status = "transcribing"
        stream.started_at = utcnow()
        await session.commit()

    try:
        engine = get_engine(stt_cfg)
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = await capture.download_vod_audio(url, tmp)
            segments = await engine.transcribe_file(audio_path)

        async with session_factory()() as session:
            stream = await session.get(YtStream, stream_id)
            for seg in segments:
                session.add(YtTranscriptSegment(
                    stream_id=stream_id, t_start=seg.t_start, t_end=seg.t_end, text=seg.text,
                ))
            stream.status = "extracting"
            stream.stats = {**(stream.stats or {}), "segments": len(segments)}
            await session.commit()

        candidates = await extract_from_transcript(segments, llm_cfg, calibration)

        async with session_factory()() as session:
            stream = await session.get(YtStream, stream_id)
            for cand in candidates:
                session.add(YtExtractedSignal(
                    stream_id=stream_id,
                    transcript_excerpt=cand["excerpt"],
                    t_in_stream=cand["t_in_stream"],
                    parsed=cand["parsed"],
                    confidence=cand["confidence"],
                    dedup_hash=cand["dedup_hash"],
                ))
            stream.status = "review"
            stream.ended_at = utcnow()
            stream.stats = {**(stream.stats or {}), "candidates": len(candidates)}
            await session.commit()
            await audit.record(session, "youtube", "vod_processed",
                               {"stream_id": stream_id, "segments": len(segments),
                                "candidates": len(candidates)})
    except Exception as exc:
        log.exception("vod_processing_failed", stream_id=stream_id)
        async with session_factory()() as session:
            stream = await session.get(YtStream, stream_id)
            if stream is not None:
                stream.status = "error"
                stream.error = str(exc)[:1000]
                await session.commit()


async def extract_from_transcript(
    segments: list[TranscriptSegment],
    llm_cfg: dict[str, Any],
    calibration: dict[str, Any],
) -> list[dict[str, Any]]:
    """Slide the live extractor's window over a full transcript (batch mode)."""
    from app.youtube.extractor import LiveExtractor

    extractor = LiveExtractor(llm_cfg, calibration, window_seconds=WINDOW_SECONDS)
    out: list[dict[str, Any]] = []
    for seg in segments:
        # batch path uses the main (stronger) model rather than the fast one
        if not extractor.spot(seg.text):
            extractor._window.append(seg)
            continue
        extractor._window.append(seg)
        cutoff = seg.t_end - extractor.window_seconds
        extractor._window = [s for s in extractor._window if s.t_end >= cutoff]
        window_text = "\n".join(s.text for s in extractor._window)
        result = await parse_with_llm(
            window_text, llm_cfg,
            channel_context=_SYSTEM_CONTEXT,
            few_shot=calibration.get("few_shot") or None,
        )
        if not result.ok or result.signal is None:
            continue
        dedup = result.signal.dedup_hash()
        if dedup in extractor._emitted_hashes:
            continue
        extractor._emitted_hashes.add(dedup)
        out.append({
            "excerpt": seg.text,
            "t_in_stream": seg.t_start,
            "parsed": result.signal.model_dump(mode="json"),
            "confidence": result.signal.confidence,
            "dedup_hash": dedup,
        })
    return out
