"""OpenAI transcription: batch REST; live path uses ~15s rolling chunks.

Higher live latency than a true streaming engine — fine for VOD calibration,
flagged as slower for live use.
"""
import asyncio
import io
import wave
from collections.abc import AsyncIterator

import httpx

from app.youtube.stt.base import BYTES_PER_SECOND, SAMPLE_RATE, STTEngine, TranscriptSegment

API_URL = "https://api.openai.com/v1/audio/transcriptions"
LIVE_CHUNK_SECONDS = 15


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class OpenAIEngine(STTEngine):
    name = "openai"

    def __init__(self, api_key: str, language: str = "", model: str = "whisper-1") -> None:
        self.api_key = api_key
        self.language = language
        self.model = model

    async def _transcribe_bytes(self, wav: bytes) -> str:
        data = {"model": self.model}
        if self.language:
            data["language"] = self.language
        async with httpx.AsyncClient(timeout=120.0) as http:
            resp = await http.post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=data,
                files={"file": ("audio.wav", wav, "audio/wav")},
            )
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip()

    async def stream(self, pcm: AsyncIterator[bytes]) -> AsyncIterator[TranscriptSegment]:
        buffer = b""
        clock = 0.0
        target = LIVE_CHUNK_SECONDS * BYTES_PER_SECOND
        async for chunk in pcm:
            buffer += chunk
            if len(buffer) < target:
                continue
            piece, buffer = buffer, b""
            duration = len(piece) / BYTES_PER_SECOND
            try:
                text = await self._transcribe_bytes(pcm_to_wav(piece))
            except (TimeoutError, httpx.HTTPError):
                text = ""
            if text:
                yield TranscriptSegment(t_start=clock, t_end=clock + duration, text=text)
            clock += duration
        if buffer:
            duration = len(buffer) / BYTES_PER_SECOND
            try:
                text = await self._transcribe_bytes(pcm_to_wav(buffer))
            except (TimeoutError, httpx.HTTPError):
                text = ""
            if text:
                yield TranscriptSegment(t_start=clock, t_end=clock + duration, text=text)

    async def transcribe_file(self, path: str) -> list[TranscriptSegment]:
        audio = await asyncio.to_thread(lambda: open(path, "rb").read())  # noqa: ASYNC230
        data = {"model": self.model, "response_format": "verbose_json"}
        if self.language:
            data["language"] = self.language
        async with httpx.AsyncClient(timeout=600.0) as http:
            resp = await http.post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=data,
                files={"file": (path.rsplit("/", 1)[-1], audio, "audio/mpeg")},
            )
            resp.raise_for_status()
            payload = resp.json()
        out = []
        for seg in payload.get("segments") or []:
            text = (seg.get("text") or "").strip()
            if text:
                out.append(TranscriptSegment(
                    t_start=float(seg.get("start", 0)),
                    t_end=float(seg.get("end", 0)), text=text,
                ))
        if not out and payload.get("text"):
            out.append(TranscriptSegment(t_start=0.0, t_end=0.0, text=payload["text"].strip()))
        return out
