"""LLM fallback: output must validate against the schema or be refused."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.parsing.llm_engine import parse_with_llm
from app.parsing.pipeline import parse_message

CFG = {"model": "anthropic/test-model", "api_key": "k", "confidence_threshold": 0.75}


def _llm_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _valid_payload(**overrides):
    data = {
        "kind": "ENTRY",
        "action": "BUY",
        "symbol": "NIFTY",
        "instrument": "OPTION",
        "strike": 25000,
        "option_type": "CE",
        "entry_type": "ABOVE",
        "entry_price": 150,
        "targets": [170, 190],
        "stop_loss": 130,
        "confidence": 0.92,
    }
    data.update(overrides)
    return data


async def test_valid_llm_output_accepted():
    with patch("litellm.acompletion", new=AsyncMock(return_value=_llm_response(json.dumps(_valid_payload())))):
        r = await parse_with_llm("nifty 25000 wala call le lo 150 ke upar", CFG)
    assert r.ok
    assert r.signal.strike == 25000
    assert r.parser == "llm:anthropic/test-model"


async def test_not_a_signal_refused():
    content = json.dumps({"not_a_signal": True, "reason": "market commentary"})
    with patch("litellm.acompletion", new=AsyncMock(return_value=_llm_response(content))):
        r = await parse_with_llm("market looking good today", CFG)
    assert not r.ok


async def test_low_confidence_refused():
    content = json.dumps(_valid_payload(confidence=0.4))
    with patch("litellm.acompletion", new=AsyncMock(return_value=_llm_response(content))):
        r = await parse_with_llm("maybe nifty something 25000", CFG)
    assert not r.ok
    assert "below threshold" in r.error


async def test_inconsistent_llm_output_refused():
    # LLM hallucinating SL above entry on a BUY must fail schema validation
    content = json.dumps(_valid_payload(stop_loss=160))
    with patch("litellm.acompletion", new=AsyncMock(return_value=_llm_response(content))):
        r = await parse_with_llm("buy nifty 25000 ce above 150 sl 160", CFG)
    assert not r.ok
    assert "schema validation" in r.error


async def test_provider_error_never_raises():
    with patch("litellm.acompletion", new=AsyncMock(side_effect=RuntimeError("api down"))):
        r = await parse_with_llm("buy nifty 25000 ce above 150 tgt 170 sl 130", CFG)
    assert not r.ok
    assert "LLM call failed" in r.error


async def test_unconfigured_llm_refuses():
    r = await parse_with_llm("buy nifty 25000 ce", {})
    assert not r.ok
    assert "not configured" in r.error


async def test_pipeline_rules_win_without_llm_call():
    llm = AsyncMock()
    with patch("litellm.acompletion", new=llm):
        r = await parse_message("BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130", llm_settings=CFG)
    assert r.ok
    assert r.parser.startswith("rules:")
    llm.assert_not_called()


async def test_pipeline_chatter_never_reaches_llm():
    llm = AsyncMock()
    with patch("litellm.acompletion", new=llm):
        r = await parse_message("good morning traders, have a nice day", llm_settings=CFG)
    assert not r.ok
    llm.assert_not_called()


async def test_pipeline_falls_back_to_llm():
    content = json.dumps(_valid_payload())
    with patch("litellm.acompletion", new=AsyncMock(return_value=_llm_response(content))):
        # Hinglish free text the rule engine can't parse but the gate lets through
        r = await parse_message("aaj nifty me 25000 ka CE lena 150 ke upar, tgt 170", llm_settings=CFG)
    assert r.ok
    assert r.parser.startswith("llm:")
