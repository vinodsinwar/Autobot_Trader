"""Two-tier live signal extraction from a transcript stream.

Tier 1 (instant, <10ms): a lexicon spotter arms a candidate the moment a
finalized utterance looks signal-like — built-in trade vocabulary plus the
channel's calibrated phrases.

Tier 2 (0.5–2s): the armed candidate plus a rolling transcript window goes to
the pluggable LLM (fast model if configured) with the channel's few-shot
calibration examples; output is schema-validated. Per-stream dedup collapses
a streamer repeating the same level into one signal.
"""
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.parsing.llm_engine import parse_with_llm
from app.parsing.rule_engine import looks_like_signal
from app.parsing.schema import ParsedSignal
from app.youtube.stt.base import TranscriptSegment

log = get_logger(__name__)

WINDOW_SECONDS = 90.0

_SYSTEM_CONTEXT = (
    "This text is a speech-to-text transcript window from a live trading stream "
    "(often Hinglish). It may contain transcription errors. Extract ONLY a trade "
    "call the streamer is actually giving RIGHT NOW at the end of the window — "
    "not levels merely discussed, hypothetical/educational examples, or recaps "
    "of earlier trades. If the newest utterances do not contain a fresh actionable "
    "call, return not_a_signal."
)


@dataclass(slots=True)
class ExtractedCandidate:
    parsed: ParsedSignal
    excerpt: str
    t_in_stream: float
    latency: dict[str, Any] = field(default_factory=dict)


class LiveExtractor:
    def __init__(
        self,
        llm_settings: dict[str, Any],
        calibration: dict[str, Any] | None = None,
        window_seconds: float = WINDOW_SECONDS,
    ) -> None:
        self.llm_settings = llm_settings
        self.calibration = calibration or {}
        self.window_seconds = window_seconds
        self._window: list[TranscriptSegment] = []
        self._emitted_hashes: set[str] = set()
        self._lexicon = [
            re.compile(p, re.IGNORECASE)
            for p in self.calibration.get("lexicon", [])
            if _safe_regex(p)
        ]

    # -- tier 1 --------------------------------------------------------------
    def spot(self, text: str) -> bool:
        if looks_like_signal(text):
            return True
        return any(rx.search(text) for rx in self._lexicon)

    # -- pipeline ------------------------------------------------------------
    async def on_segment(self, seg: TranscriptSegment) -> ExtractedCandidate | None:
        self._window.append(seg)
        cutoff = seg.t_end - self.window_seconds
        self._window = [s for s in self._window if s.t_end >= cutoff]

        spotted_at = time.monotonic()
        if not self.spot(seg.text):
            return None

        window_text = "\n".join(s.text for s in self._window)
        result = await parse_with_llm(
            window_text,
            self.llm_settings,
            channel_context=_SYSTEM_CONTEXT,
            few_shot=self.calibration.get("few_shot") or None,
            use_fast_model=True,
        )
        if not result.ok or result.signal is None:
            log.debug("yt_extract_no_signal", reason=result.error, trigger=seg.text[:120])
            return None

        dedup = result.signal.dedup_hash()
        if dedup in self._emitted_hashes:
            log.debug("yt_extract_duplicate_suppressed", summary=result.signal.human_summary())
            return None
        self._emitted_hashes.add(dedup)

        return ExtractedCandidate(
            parsed=result.signal,
            excerpt=seg.text,
            t_in_stream=seg.t_start,
            latency={
                "trigger_segment_end_s": round(seg.t_end, 1),
                "llm_ms": round(result.latency_ms, 1),
                "tier2_total_ms": round((time.monotonic() - spotted_at) * 1000, 1),
                "parser": result.parser,
            },
        )


def _safe_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False
