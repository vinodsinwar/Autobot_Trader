"""Pluggable speech-to-text engines.

Two modes:
- `stream(pcm)`   — real-time: consume 16kHz mono s16le PCM chunks, yield
                    finalized utterances as they happen (live trading path).
- `transcribe_file(path)` — batch: whole audio file → segments (VOD path).

Provider chosen in dashboard Settings → STT ({"provider", "api_key", "language"}).
"""
import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # s16le mono


@dataclass(slots=True)
class TranscriptSegment:
    t_start: float   # seconds from capture start
    t_end: float
    text: str


class STTEngine(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def stream(self, pcm: AsyncIterator[bytes]) -> AsyncIterator[TranscriptSegment]:
        """Real-time transcription of a PCM byte stream."""

    @abc.abstractmethod
    async def transcribe_file(self, path: str) -> list[TranscriptSegment]:
        """Batch transcription of an audio file."""


class STTConfigError(Exception):
    pass


def get_engine(cfg: dict[str, Any] | None) -> STTEngine:
    cfg = cfg or {}
    provider = (cfg.get("provider") or "").lower()
    if provider == "deepgram":
        from app.youtube.stt.deepgram import DeepgramEngine
        return DeepgramEngine(cfg.get("api_key", ""), cfg.get("language", ""))
    if provider == "openai":
        from app.youtube.stt.openai_stt import OpenAIEngine
        return OpenAIEngine(cfg.get("api_key", ""), cfg.get("language", ""))
    if provider in ("local", "faster-whisper", "whisper"):
        from app.youtube.stt.whisper_local import WhisperLocalEngine
        return WhisperLocalEngine(cfg.get("model_size", "small"), cfg.get("language", ""))
    raise STTConfigError(
        f"STT provider {provider!r} not configured — set Settings → STT to "
        "deepgram | openai | local"
    )
