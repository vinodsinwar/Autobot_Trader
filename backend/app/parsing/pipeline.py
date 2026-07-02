"""Parse orchestration: rules first, LLM fallback second (gated + validated)."""
from typing import Any

from app.parsing import llm_engine, rule_engine
from app.parsing.schema import ParseResult


async def parse_message(
    text: str,
    rule_pack: str = "generic",
    rule_overrides: dict[str, Any] | None = None,
    llm_fallback: bool = True,
    llm_settings: dict[str, Any] | None = None,
    channel_context: str = "",
    few_shot: list[dict[str, Any]] | None = None,
) -> ParseResult:
    pack = rule_engine.get_pack(rule_pack, rule_overrides)
    result = rule_engine.parse_with_rules(text, pack)
    if result.ok:
        return result

    # LLM fallback only for messages that plausibly contain a signal — chatter
    # never burns tokens.
    if llm_fallback and rule_engine.looks_like_signal(text):
        llm_result = await llm_engine.parse_with_llm(
            text,
            llm_settings or {},
            channel_context=channel_context,
            few_shot=few_shot,
        )
        if llm_result.ok:
            return llm_result
        llm_result.error = f"rules: {result.error} | llm: {llm_result.error}"
        return llm_result

    return result
