"""Local faster-whisper engine: free and private; needs CPU muscle for live.

Install with: pip install "autobot-trader[stt-local]"
"""
import asyncio
from collections.abc import AsyncIterator

from app.youtube.stt.base import BYTES_PER_SECOND, STTEngine, TranscriptSegment

LIVE_CHUNK_SECONDS = 10


class WhisperLocalEngine(STTEngine):
    name = "whisper-local"

    def __init__(self, model_size: str = "small", language: str = "") -> None:
        self.model_size = model_size
        self.language = language or None
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    'faster-whisper not installed — pip install "autobot-trader[stt-local]"'
                ) from exc
            self._model = WhisperModel(self.model_size, compute_type="int8")
        return self._model

    def _transcribe_pcm(self, pcm: bytes) -> list[tuple[float, float, str]]:
        import numpy as np

        model = self._load()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = model.transcribe(audio, language=self.language, vad_filter=True)
        return [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]

    async def stream(self, pcm: AsyncIterator[bytes]) -> AsyncIterator[TranscriptSegment]:
        buffer = b""
        clock = 0.0
        target = LIVE_CHUNK_SECONDS * BYTES_PER_SECOND
        loop = asyncio.get_running_loop()
        async for chunk in pcm:
            buffer += chunk
            if len(buffer) < target:
                continue
            piece, buffer = buffer, b""
            duration = len(piece) / BYTES_PER_SECOND
            results = await loop.run_in_executor(None, self._transcribe_pcm, piece)
            for start, end, text in results:
                yield TranscriptSegment(t_start=clock + start, t_end=clock + end, text=text)
            clock += duration

    async def transcribe_file(self, path: str) -> list[TranscriptSegment]:
        loop = asyncio.get_running_loop()

        def run() -> list[TranscriptSegment]:
            model = self._load()
            segments, _info = model.transcribe(path, language=self.language, vad_filter=True)
            return [
                TranscriptSegment(t_start=s.start, t_end=s.end, text=s.text.strip())
                for s in segments if s.text.strip()
            ]

        return await loop.run_in_executor(None, run)
