"""YouTube source + calibration studio API (contract matches Youtube.tsx)."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth
from app.db.models import YtChannel, YtExtractedSignal, YtFeedback, YtStream, YtTranscriptSegment
from app.db.session import get_session
from app.youtube import calibration as calibration_mod
from app.youtube import capture, vod

router = APIRouter(prefix="/api/youtube", dependencies=[Depends(require_auth)])


async def _refresh_manager() -> None:
    from app import services

    if services.youtube_manager is not None:
        await services.youtube_manager.refresh_channels()


def _channel_json(c: YtChannel, live: bool) -> dict[str, Any]:
    cal = c.calibration or {}
    return {
        "id": c.id, "url": c.url, "channel_id": c.channel_id, "handle": c.handle,
        "title": c.title, "enabled": c.enabled, "execution_mode": c.execution_mode,
        "broker": c.broker, "poll_interval_s": c.poll_interval_s, "live": live,
        "calibration": {
            "examples": cal.get("examples", 0),
            "precision": cal.get("precision"),
            "recall": cal.get("recall"),
            "lexicon_patterns": len(cal.get("lexicon", [])),
        },
    }


def _live_ids() -> set[int]:
    from app import services

    if services.youtube_manager is None:
        return set()
    return set(services.youtube_manager.live_streams.keys())


@router.get("/channels")
async def list_channels(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(YtChannel).order_by(YtChannel.id))).scalars().all()
    live = _live_ids()
    return [_channel_json(c, c.id in live) for c in rows]


class YtChannelBody(BaseModel):
    url: str
    broker: str = "paper"
    execution_mode: str = "manual"   # voice signals default to manual approval
    sizing: dict[str, Any] = {}


@router.post("/channels")
async def create_channel(body: YtChannelBody, session: AsyncSession = Depends(get_session)):
    try:
        info = await capture.resolve_channel(body.url)
    except Exception as exc:
        raise HTTPException(400, f"could not resolve channel: {exc}") from exc
    if not info.get("channel_id"):
        raise HTTPException(400, "no channel id found at that URL")
    existing = (
        await session.execute(
            select(YtChannel).where(YtChannel.channel_id == info["channel_id"])
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"channel already subscribed: {existing.title}")
    channel = YtChannel(
        url=body.url, channel_id=info["channel_id"], handle=info.get("handle", ""),
        title=info.get("title", ""), broker=body.broker,
        execution_mode=body.execution_mode, sizing=body.sizing,
    )
    session.add(channel)
    await session.commit()
    await _refresh_manager()
    return _channel_json(channel, False)


@router.patch("/channels/{channel_id}")
async def update_channel(
    channel_id: int, body: dict[str, Any], session: AsyncSession = Depends(get_session)
):
    channel = await session.get(YtChannel, channel_id)
    if channel is None:
        raise HTTPException(404, "no such channel")
    for field in ("enabled", "execution_mode", "broker", "poll_interval_s", "sizing", "title"):
        if field in body:
            setattr(channel, field, body[field])
    await session.commit()
    await _refresh_manager()
    return _channel_json(channel, channel.id in _live_ids())


@router.post("/channels/{channel_id}/rebuild-profile")
async def rebuild_profile(channel_id: int, session: AsyncSession = Depends(get_session)):
    try:
        profile = await calibration_mod.rebuild_profile(session, channel_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "examples": profile["examples"],
        "lexicon_patterns": len(profile["lexicon"]),
        "precision": profile["precision"],
        "recall": profile["recall"],
    }


@router.get("/streams")
async def list_streams(
    channel_id: int | None = None, limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    q = select(YtStream).order_by(YtStream.id.desc()).limit(min(limit, 200))
    if channel_id:
        q = q.where(YtStream.yt_channel_id == channel_id)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": s.id, "yt_channel_id": s.yt_channel_id, "video_id": s.video_id,
            "kind": s.kind, "title": s.title, "status": s.status,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "stats": s.stats, "error": s.error,
        }
        for s in rows
    ]


@router.get("/streams/{stream_id}")
async def stream_detail(stream_id: int, session: AsyncSession = Depends(get_session)):
    stream = await session.get(YtStream, stream_id)
    if stream is None:
        raise HTTPException(404, "no such stream")
    extracted = (
        await session.execute(
            select(YtExtractedSignal)
            .where(YtExtractedSignal.stream_id == stream_id)
            .order_by(YtExtractedSignal.t_in_stream)
        )
    ).scalars().all()
    feedback = (
        await session.execute(select(YtFeedback).where(YtFeedback.stream_id == stream_id))
    ).scalars().all()
    fb_by_extracted = {f.yt_extracted_signal_id: f.label for f in feedback
                       if f.yt_extracted_signal_id}
    segments = (
        await session.execute(
            select(YtTranscriptSegment)
            .where(YtTranscriptSegment.stream_id == stream_id)
            .order_by(YtTranscriptSegment.t_start)
            .limit(2000)
        )
    ).scalars().all()
    return {
        "id": stream.id, "video_id": stream.video_id, "title": stream.title,
        "kind": stream.kind, "status": stream.status, "stats": stream.stats,
        "error": stream.error,
        "extracted": [
            {
                "id": x.id, "t_in_stream": x.t_in_stream,
                "transcript_excerpt": x.transcript_excerpt,
                "parsed": x.parsed, "confidence": x.confidence,
                "latency": x.latency,
                "feedback": fb_by_extracted.get(x.id),
            }
            for x in extracted
        ],
        "segments": [
            {"id": g.id, "t_start": g.t_start, "t_end": g.t_end, "text": g.text}
            for g in segments
        ],
    }


class VodBody(BaseModel):
    yt_channel_id: int
    url: str


@router.post("/vod")
async def queue_vod(body: VodBody, session: AsyncSession = Depends(get_session)):
    channel = await session.get(YtChannel, body.yt_channel_id)
    if channel is None:
        raise HTTPException(404, "no such channel")
    try:
        stream_id = await vod.queue_vod(body.yt_channel_id, body.url)
    except Exception as exc:
        raise HTTPException(400, f"could not resolve video: {exc}") from exc
    return {"stream_id": stream_id, "status": "pending"}


class FeedbackBody(BaseModel):
    stream_id: int
    label: str                            # correct | wrong | missed
    yt_extracted_signal_id: int | None = None
    excerpt: str = ""
    corrected: dict[str, Any] = {}


@router.post("/feedback")
async def add_feedback(body: FeedbackBody, session: AsyncSession = Depends(get_session)):
    if body.label not in ("correct", "wrong", "missed"):
        raise HTTPException(400, "label must be correct | wrong | missed")
    if body.label in ("correct", "wrong") and not body.yt_extracted_signal_id:
        raise HTTPException(400, "correct/wrong feedback needs yt_extracted_signal_id")
    if body.label == "missed" and not body.excerpt:
        raise HTTPException(400, "missed feedback needs the transcript excerpt")
    stream = await session.get(YtStream, body.stream_id)
    if stream is None:
        raise HTTPException(404, "no such stream")
    session.add(YtFeedback(
        stream_id=body.stream_id,
        yt_extracted_signal_id=body.yt_extracted_signal_id,
        label=body.label, excerpt=body.excerpt, corrected=body.corrected,
    ))
    await session.commit()
    return {"ok": True}
