"""Deepgram: streaming websocket (live) + prerecorded REST (VOD).

The streaming API returns finalized utterances in well under a second —
this is the recommended live engine.
"""
import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from app.core.logging import get_logger
from app.youtube.stt.base import SAMPLE_RATE, STTEngine, TranscriptSegment

log = get_logger(__name__)

WS_URL = "wss://api.deepgram.com/v1/listen"
REST_URL = "https://api.deepgram.com/v1/listen"


class DeepgramEngine(STTEngine):
    name = "deepgram"

    def __init__(self, api_key: str, language: str = "") -> None:
        self.api_key = api_key
        self.language = language

    def _params(self, streaming: bool) -> str:
        params = [
            "model=nova-2", "smart_format=true", "punctuate=true",
        ]
        if self.language:
            params.append(f"language={self.language}")
        if streaming:
            params += [
                "encoding=linear16", f"sample_rate={SAMPLE_RATE}", "channels=1",
                "interim_results=false", "endpointing=400",
            ]
        return "&".join(params)

    async def stream(self, pcm: AsyncIterator[bytes]) -> AsyncIterator[TranscriptSegment]:
        import websockets

        url = f"{WS_URL}?{self._params(streaming=True)}"
        async with websockets.connect(
            url, additional_headers={"Authorization": f"Token {self.api_key}"},
        ) as ws:
            async def sender() -> None:
                try:
                    async for chunk in pcm:
                        await ws.send(chunk)
                finally:
                    await ws.send(json.dumps({"type": "CloseStream"}))

            send_task = asyncio.create_task(sender())
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("type") != "Results" or not msg.get("is_final"):
                        continue
                    alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
                    text = (alt.get("transcript") or "").strip()
                    if not text:
                        continue
                    start = float(msg.get("start", 0.0))
                    yield TranscriptSegment(
                        t_start=start,
                        t_end=start + float(msg.get("duration", 0.0)),
                        text=text,
                    )
            finally:
                send_task.cancel()

    async def transcribe_file(self, path: str) -> list[TranscriptSegment]:
        audio = await asyncio.to_thread(lambda: open(path, "rb").read())  # noqa: ASYNC230
        async with httpx.AsyncClient(timeout=600.0) as http:
            resp = await http.post(
                f"{REST_URL}?{self._params(streaming=False)}&utterances=true",
                headers={"Authorization": f"Token {self.api_key}",
                         "Content-Type": "audio/*"},
                content=audio,
            )
            resp.raise_for_status()
            data = resp.json()
        segments = []
        for utt in data.get("results", {}).get("utterances", []) or []:
            text = (utt.get("transcript") or "").strip()
            if text:
                segments.append(TranscriptSegment(
                    t_start=float(utt.get("start", 0)),
                    t_end=float(utt.get("end", 0)),
                    text=text,
                ))
        return segments
