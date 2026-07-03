"""Calibration profile builder: user feedback → few-shot examples + lexicon.

"Training" is prompt-level and provider-agnostic: works identically whichever
LLM the user selects. Output stored in YtChannel.calibration:
  {"few_shot": [...], "lexicon": [...], "examples": n,
   "precision": float|None, "recall": float|None}
"""
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import YtChannel, YtExtractedSignal, YtFeedback, YtStream

MAX_FEW_SHOT = 12
MAX_LEXICON = 40

_STOPWORDS = {
    "THE", "AND", "FOR", "WITH", "HAI", "KA", "KE", "KI", "TO", "ME", "PE",
    "NA", "HO", "AB", "IS", "YE", "WO", "PAR", "SE", "BHI", "TOH", "JO",
}


def lexicon_pattern_from_excerpt(excerpt: str) -> str | None:
    """Distill a spotter regex from a labeled excerpt: its content words in order.

    'nifty ka 25000 wala call le lo' → r'NIFTY.*CALL.*LE' — cheap, per-channel,
    tolerant of number changes.
    """
    words = re.findall(r"[A-Za-z]{2,}", excerpt.upper())
    content = [w for w in words if w not in _STOPWORDS][:4]
    if len(content) < 2:
        return None
    return r"\b" + r"\b.*\b".join(re.escape(w) for w in content) + r"\b"


async def rebuild_profile(session: AsyncSession, yt_channel_id: int) -> dict[str, Any]:
    channel = await session.get(YtChannel, yt_channel_id)
    if channel is None:
        raise ValueError(f"no yt channel {yt_channel_id}")

    rows = (
        await session.execute(
            select(YtFeedback, YtExtractedSignal)
            .join(YtStream, YtFeedback.stream_id == YtStream.id)
            .outerjoin(YtExtractedSignal, YtFeedback.yt_extracted_signal_id == YtExtractedSignal.id)
            .where(YtStream.yt_channel_id == yt_channel_id)
            .order_by(YtFeedback.id.desc())
        )
    ).all()

    few_shot: list[dict[str, Any]] = []
    lexicon: list[str] = []
    correct = wrong = missed = 0

    for fb, extracted in rows:
        if fb.label == "correct" and extracted is not None:
            correct += 1
            if len(few_shot) < MAX_FEW_SHOT:
                few_shot.append({"input": extracted.transcript_excerpt,
                                 "output": extracted.parsed})
            pattern = lexicon_pattern_from_excerpt(extracted.transcript_excerpt)
            if pattern and pattern not in lexicon:
                lexicon.append(pattern)
        elif fb.label == "wrong" and extracted is not None:
            wrong += 1
            if len(few_shot) < MAX_FEW_SHOT:
                few_shot.append({
                    "input": extracted.transcript_excerpt,
                    "output": {"not_a_signal": True,
                               "reason": "user marked this extraction as wrong"},
                })
        elif fb.label == "missed":
            missed += 1
            if fb.corrected and len(few_shot) < MAX_FEW_SHOT:
                few_shot.append({"input": fb.excerpt, "output": fb.corrected})
            pattern = lexicon_pattern_from_excerpt(fb.excerpt)
            if pattern and pattern not in lexicon:
                lexicon.append(pattern)

    detected = correct + wrong
    actual = correct + missed
    profile = {
        "few_shot": few_shot,
        "lexicon": lexicon[:MAX_LEXICON],
        "examples": correct + wrong + missed,
        "precision": round(correct / detected, 3) if detected else None,
        "recall": round(correct / actual, 3) if actual else None,
    }
    channel.calibration = profile
    await session.commit()
    return profile
