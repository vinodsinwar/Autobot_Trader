"""Emit an extracted YouTube signal: persist → inject into the trading
pipeline → post to the Telegram signal group. Execution never waits on the
Telegram round-trip."""
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.db import audit
from app.db.models import Signal, SignalSource, SignalState, YtChannel, YtExtractedSignal, YtStream
from app.db.session import get_setting, session_factory
from app.execution.router import gate_and_execute
from app.youtube.extractor import ExtractedCandidate

log = get_logger(__name__)

YOUTUBE_SETTINGS_KEY = "youtube"  # {"relay_chat_id": int|None}


def format_relay_message(
    channel_title: str, candidate: ExtractedCandidate, signal_id: int
) -> str:
    p = candidate.parsed
    t = int(candidate.t_in_stream)
    return (
        f"🎥 #AUTOBOT YouTube signal · {channel_title}\n"
        f"{p.human_summary()}\n"
        f"confidence {p.confidence:.0%} · heard at {t // 60}:{t % 60:02d} · signal #{signal_id}\n"
        f"“{candidate.excerpt[:200]}”"
    )


async def emit(stream_id: int, candidate: ExtractedCandidate) -> int | None:
    """Persist + execute + relay one extracted candidate. Returns signal id."""
    async with session_factory()() as session:
        stream = await session.get(YtStream, stream_id)
        if stream is None:
            return None
        yt_channel = await session.get(YtChannel, stream.yt_channel_id)
        if yt_channel is None:
            return None

        extracted = YtExtractedSignal(
            stream_id=stream_id,
            transcript_excerpt=candidate.excerpt,
            t_in_stream=candidate.t_in_stream,
            parsed=candidate.parsed.model_dump(mode="json"),
            confidence=candidate.parsed.confidence,
            dedup_hash=candidate.parsed.dedup_hash(),
            latency={**candidate.latency, "extracted_at": datetime.now(UTC).isoformat()},
        )
        session.add(extracted)
        await session.flush()

        signal = Signal(
            source=SignalSource.YOUTUBE.value,
            state=SignalState.PARSED.value,
            yt_extracted_signal_id=extracted.id,
            parsed=candidate.parsed.model_dump(mode="json"),
            parser=candidate.latency.get("parser", "llm"),
            confidence=candidate.parsed.confidence,
            broker=yt_channel.broker,
            dedup_hash=candidate.parsed.dedup_hash(),
        )
        session.add(signal)
        await session.flush()
        await audit.record(
            session, "youtube", "signal_extracted",
            {"summary": candidate.parsed.human_summary(),
             "channel": yt_channel.title or yt_channel.handle,
             "excerpt": candidate.excerpt[:200],
             "latency": candidate.latency},
            signal.id,
        )

        # inject into risk → approval → execution (same path as Telegram)
        await gate_and_execute(
            session, signal, candidate.parsed,
            yt_channel.execution_mode, yt_channel.sizing,
        )
        await session.commit()
        signal_id = signal.id

        # relay to the Telegram signal group (audit log / approval surface)
        relay_cfg = await get_setting(session, YOUTUBE_SETTINGS_KEY, {}) or {}

    chat_id = relay_cfg.get("relay_chat_id")
    if chat_id:
        from app import services

        if services.telegram_service is not None and services.telegram_service.connected:
            try:
                msg_id = await services.telegram_service.send_message(
                    int(chat_id),
                    format_relay_message(yt_channel.title or yt_channel.handle,
                                         candidate, signal_id),
                )
                async with session_factory()() as session:
                    row = await session.get(YtExtractedSignal, extracted.id)
                    if row is not None:
                        row.relay_message_id = msg_id
                        await session.commit()
            except Exception:
                log.exception("yt_relay_send_failed")
    return signal_id
