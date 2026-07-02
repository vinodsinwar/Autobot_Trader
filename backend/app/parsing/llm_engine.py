"""LLM fallback parser — provider-agnostic via litellm.

The provider/model/API key are read from the dashboard-managed `llm` setting,
so the user can point this at Anthropic, OpenAI, Gemini, Ollama or anything
litellm supports, and swap models without redeploying. LLM output is only
trusted if it validates against ParsedSignal; anything else is a parse failure.
"""
import json
import re
import time
from typing import Any

from app.core.logging import get_logger
from app.parsing.schema import ParsedSignal, ParseResult

log = get_logger(__name__)

DEFAULT_LLM_SETTINGS: dict[str, Any] = {
    "model": "",              # litellm model string, e.g. "anthropic/claude-haiku-4-5"
    "api_key": "",
    "api_base": "",           # for Ollama / self-hosted OpenAI-compatible endpoints
    "temperature": 0.0,
    "confidence_threshold": 0.75,
    "max_tokens": 800,
    # optional separate fast model for the YouTube live hot path
    "fast_model": "",
    "fast_api_key": "",
}

_SYSTEM_PROMPT = """You extract trading signals from messages posted in trading channels.

Return ONLY a JSON object, no prose. If the message is NOT an actionable trade signal
(chatter, performance brag, ad, market commentary, hypothetical/educational example),
return exactly: {"not_a_signal": true, "reason": "<short reason>"}

Otherwise return:
{
  "kind": "ENTRY" | "EXIT" | "MODIFY",
  "action": "BUY" | "SELL",
  "symbol": "<underlying, e.g. NIFTY, BANKNIFTY, RELIANCE, BTCUSD>",
  "instrument": "OPTION" | "FUTURE" | "EQUITY" | "CRYPTO_PERP" | "CRYPTO_SPOT",
  "strike": <number or null>,
  "option_type": "CE" | "PE" | null,
  "expiry_hint": "<string or null>",
  "entry_type": "MARKET" | "LIMIT" | "ABOVE" | "BELOW",
  "entry_price": <number or null>,
  "entry_range_low": <number or null>,
  "entry_range_high": <number or null>,
  "targets": [<numbers>],
  "stop_loss": <number or null>,
  "quantity_hint": "<string or null>",
  "notes": "<anything important you noticed>",
  "confidence": <0.0-1.0, your honest confidence that this is a real, correctly-extracted signal>
}

Rules:
- Never invent numbers that are not in the message.
- "CALL"→CE, "PUT"→PE. LONG→BUY, SHORT→SELL.
- If multiple targets are given (e.g. "TGT 170/190"), list them all in order.
- Be conservative with confidence when the message is vague."""


def _parse_llm_json(content: str) -> dict[str, Any]:
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in LLM response")
    return json.loads(m.group(0))


async def parse_with_llm(
    text: str,
    llm_settings: dict[str, Any],
    channel_context: str = "",
    few_shot: list[dict[str, Any]] | None = None,
    use_fast_model: bool = False,
) -> ParseResult:
    started = time.perf_counter()
    cfg = {**DEFAULT_LLM_SETTINGS, **(llm_settings or {})}
    model = (cfg.get("fast_model") if use_fast_model else "") or cfg["model"]

    def result(ok: bool, signal: ParsedSignal | None = None, error: str = "") -> ParseResult:
        return ParseResult(
            ok=ok, signal=signal, parser=f"llm:{model or 'unconfigured'}",
            error=error, latency_ms=(time.perf_counter() - started) * 1000,
        )

    if not model:
        return result(False, error="LLM parser not configured (set model in Settings → LLM)")

    messages: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if channel_context:
        messages.append({"role": "system", "content": f"Channel context: {channel_context}"})
    for ex in few_shot or []:
        messages.append({"role": "user", "content": ex["input"]})
        messages.append({"role": "assistant", "content": json.dumps(ex["output"])})
    messages.append({"role": "user", "content": text})

    try:
        import litellm

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        }
        api_key = (cfg.get("fast_api_key") if use_fast_model else "") or cfg.get("api_key")
        if api_key:
            kwargs["api_key"] = api_key
        if cfg.get("api_base"):
            kwargs["api_base"] = cfg["api_base"]
        response = await litellm.acompletion(**kwargs)
        content = response.choices[0].message.content or ""
    except Exception as exc:  # provider/network errors must never crash the pipeline
        log.warning("llm_parse_error", error=str(exc))
        return result(False, error=f"LLM call failed: {exc}")

    try:
        data = _parse_llm_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        return result(False, error=f"unparseable LLM output: {exc}")

    if data.get("not_a_signal"):
        return result(False, error=f"not a signal: {data.get('reason', '')}")

    try:
        signal = ParsedSignal.model_validate(data)
    except Exception as exc:
        return result(False, error=f"LLM output failed schema validation: {exc}")

    threshold = float(cfg.get("confidence_threshold") or 0.0)
    if signal.confidence < threshold:
        return result(
            False,
            error=f"confidence {signal.confidence:.2f} below threshold {threshold:.2f}",
        )
    return result(True, signal)
