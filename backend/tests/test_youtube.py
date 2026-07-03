"""YouTube pipeline: extraction, dedup, calibration, relay injection, API."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.models import (
    Signal,
    SignalState,
    YtChannel,
    YtExtractedSignal,
    YtFeedback,
    YtStream,
)
from app.execution import registry
from app.youtube.calibration import lexicon_pattern_from_excerpt, rebuild_profile
from app.youtube.extractor import LiveExtractor
from app.youtube.stt.base import STTConfigError, TranscriptSegment, get_engine

LLM_CFG = {"model": "anthropic/test", "api_key": "k", "confidence_threshold": 0.7}


def llm_response(payload: dict):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def signal_payload(**overrides):
    data = {
        "kind": "ENTRY", "action": "BUY", "symbol": "NIFTY", "instrument": "OPTION",
        "strike": 25000, "option_type": "CE", "entry_type": "ABOVE",
        "entry_price": 150, "targets": [170], "stop_loss": 130, "confidence": 0.9,
    }
    data.update(overrides)
    return data


def seg(t, text):
    return TranscriptSegment(t_start=t, t_end=t + 4, text=text)


@pytest.fixture(autouse=True)
async def clean_registry():
    await registry.invalidate()
    yield
    await registry.invalidate()


class TestLiveExtractor:
    async def test_chatter_never_calls_llm(self):
        llm = AsyncMock()
        with patch("litellm.acompletion", new=llm):
            ex = LiveExtractor(LLM_CFG)
            out = await ex.on_segment(seg(0, "market bahut acha lag raha hai aaj"))
        assert out is None
        llm.assert_not_called()

    async def test_signal_utterance_extracted(self):
        with patch("litellm.acompletion",
                   new=AsyncMock(return_value=llm_response(signal_payload()))):
            ex = LiveExtractor(LLM_CFG)
            await ex.on_segment(seg(0, "dekho market side me hai"))
            out = await ex.on_segment(
                seg(5, "nifty 25000 ka call le lo 150 ke upar sl 130 target 170"))
        assert out is not None
        assert out.parsed.strike == 25000
        assert out.excerpt.startswith("nifty 25000")
        assert out.latency["llm_ms"] >= 0

    async def test_repeated_signal_deduped(self):
        with patch("litellm.acompletion",
                   new=AsyncMock(return_value=llm_response(signal_payload()))):
            ex = LiveExtractor(LLM_CFG)
            first = await ex.on_segment(seg(0, "buy nifty 25000 CE above 150 sl 130 tgt 170"))
            second = await ex.on_segment(seg(30, "phir bol raha hu buy nifty 25000 CE above 150 sl 130 tgt 170"))
        assert first is not None
        assert second is None  # same trade, one signal

    async def test_lexicon_pattern_arms_tier1(self):
        cal = {"lexicon": [r"\bWALA\b.*\bLE\b"]}
        with patch("litellm.acompletion",
                   new=AsyncMock(return_value=llm_response(signal_payload()))):
            ex = LiveExtractor(LLM_CFG, cal)
            # no default trade keywords, but matches the channel's calibrated phrase
            out = await ex.on_segment(seg(0, "25000 wala le lena bhai"))
        assert out is not None

    async def test_not_a_signal_from_llm(self):
        content = {"not_a_signal": True, "reason": "recap of earlier trade"}
        with patch("litellm.acompletion", new=AsyncMock(return_value=llm_response(content))):
            ex = LiveExtractor(LLM_CFG)
            out = await ex.on_segment(seg(0, "humne nifty 25000 CE 150 pe liya tha kal"))
        assert out is None


class TestCalibration:
    def test_lexicon_from_excerpt(self):
        p = lexicon_pattern_from_excerpt("nifty ka 25000 wala call le lo abhi")
        assert p is not None
        import re
        assert re.search(p, "NIFTY KA 26000 WALA CALL LE LO".upper())

    async def test_rebuild_profile(self, db):
        async with db.session_factory()() as session:
            ch = YtChannel(url="u", channel_id="UC1", title="Trader X")
            session.add(ch)
            await session.flush()
            st = YtStream(yt_channel_id=ch.id, video_id="v1", kind="vod", status="review")
            session.add(st)
            await session.flush()
            x1 = YtExtractedSignal(stream_id=st.id, transcript_excerpt="nifty 25000 call le lo",
                                   parsed=signal_payload(), confidence=0.9)
            x2 = YtExtractedSignal(stream_id=st.id, transcript_excerpt="market up jayega",
                                   parsed=signal_payload(strike=26000), confidence=0.6)
            session.add_all([x1, x2])
            await session.flush()
            session.add_all([
                YtFeedback(stream_id=st.id, yt_extracted_signal_id=x1.id, label="correct"),
                YtFeedback(stream_id=st.id, yt_extracted_signal_id=x2.id, label="wrong"),
                YtFeedback(stream_id=st.id, label="missed",
                           excerpt="banknifty 51000 put lena",
                           corrected=signal_payload(symbol="BANKNIFTY", strike=51000,
                                                    option_type="PE")),
            ])
            await session.commit()
            profile = await rebuild_profile(session, ch.id)

        assert profile["examples"] == 3
        assert profile["precision"] == 0.5   # 1 correct / (1 correct + 1 wrong)
        assert profile["recall"] == 0.5      # 1 correct / (1 correct + 1 missed)
        assert len(profile["few_shot"]) == 3
        assert profile["lexicon"]            # patterns from correct + missed


class TestRelayEmit:
    async def test_emit_persists_and_awaits_approval(self, db):
        from app.parsing.schema import ParsedSignal
        from app.youtube.extractor import ExtractedCandidate
        from app.youtube.relay import emit

        async with db.session_factory()() as session:
            ch = YtChannel(url="u", channel_id="UC1", title="Trader X",
                           execution_mode="manual", broker="paper")
            session.add(ch)
            await session.flush()
            st = YtStream(yt_channel_id=ch.id, video_id="v1", kind="live", status="capturing")
            session.add(st)
            await session.commit()
            stream_id = st.id

        cand = ExtractedCandidate(
            parsed=ParsedSignal.model_validate(signal_payload()),
            excerpt="nifty 25000 call le lo 150 ke upar",
            t_in_stream=125.0,
            latency={"llm_ms": 800.0, "parser": "llm:test"},
        )
        signal_id = await emit(stream_id, cand)
        assert signal_id is not None

        async with db.session_factory()() as session:
            signal = await session.get(Signal, signal_id)
            extracted = (await session.execute(select(YtExtractedSignal))).scalar_one()
        assert signal.source == "youtube"
        # manual mode: awaits one-click approval, nothing executed yet
        assert signal.state == SignalState.AWAITING_APPROVAL.value
        assert extracted.transcript_excerpt.startswith("nifty")
        assert signal.yt_extracted_signal_id == extracted.id

    async def test_emit_auto_mode_executes_on_paper(self, db):
        from app.db.models import Order
        from app.parsing.schema import ParsedSignal
        from app.youtube.extractor import ExtractedCandidate
        from app.youtube.relay import emit

        async with db.session_factory()() as session:
            ch = YtChannel(url="u", channel_id="UC2", title="Fast Trader",
                           execution_mode="auto", broker="paper",
                           sizing={"mode": "units", "value": 75})
            session.add(ch)
            await session.flush()
            st = YtStream(yt_channel_id=ch.id, video_id="v2", kind="live", status="capturing")
            session.add(st)
            await session.commit()
            stream_id = st.id

        signal_id = await emit(stream_id, ExtractedCandidate(
            parsed=ParsedSignal.model_validate(signal_payload()),
            excerpt="buy karo", t_in_stream=10.0,
        ))
        async with db.session_factory()() as session:
            signal = await session.get(Signal, signal_id)
            order = (await session.execute(select(Order))).scalar_one()
        assert signal.state == SignalState.EXECUTING.value
        assert order.broker == "paper"
        assert order.qty == 75


class TestVodExtraction:
    async def test_windowed_extraction_with_dedup(self):
        from app.youtube.vod import extract_from_transcript

        segments = [
            seg(0, "namaste doston welcome to the stream"),
            seg(10, "nifty 25000 call le lo 150 ke upar sl 130 target 170"),
            seg(60, "maine bola tha buy nifty 25000 CE 150 upar sl 130 tgt 170"),  # repeat
        ]
        with patch("litellm.acompletion",
                   new=AsyncMock(return_value=llm_response(signal_payload()))):
            out = await extract_from_transcript(segments, LLM_CFG, {})
        assert len(out) == 1
        assert out[0]["parsed"]["strike"] == 25000


class TestSttFactory:
    def test_unconfigured_provider_raises(self):
        with pytest.raises(STTConfigError):
            get_engine({})

    def test_deepgram_engine_selected(self):
        engine = get_engine({"provider": "deepgram", "api_key": "k", "language": "hi"})
        assert engine.name == "deepgram"


class TestYoutubeApi:
    @pytest.fixture
    async def client(self, db):
        from httpx import ASGITransport, AsyncClient

        from app.core import security
        from app.db.session import set_setting
        from app.main import app

        async with db.session_factory()() as session:
            await set_setting(session, "auth",
                              {"password_hash": security.hash_password("pw12345678")})
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/auth/login", json={"password": "pw12345678"})
            c.headers["Authorization"] = f"Bearer {resp.json()['token']}"
            yield c

    async def test_subscribe_and_configure_channel(self, client):
        resolved = {"channel_id": "UCabc", "title": "Some Trader", "handle": "sometrader"}
        with patch("app.youtube.capture.resolve_channel", new=AsyncMock(return_value=resolved)):
            resp = await client.post("/api/youtube/channels",
                                     json={"url": "https://youtube.com/@sometrader"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["execution_mode"] == "manual"  # voice signals gated by default
        cid = data["id"]

        # duplicate subscription blocked
        with patch("app.youtube.capture.resolve_channel", new=AsyncMock(return_value=resolved)):
            dup = await client.post("/api/youtube/channels", json={"url": "x"})
        assert dup.status_code == 409

        resp = await client.patch(f"/api/youtube/channels/{cid}",
                                  json={"execution_mode": "auto", "broker": "delta"})
        assert resp.json()["execution_mode"] == "auto"

    async def test_vod_queue_and_feedback_flow(self, db, client):
        resolved = {"channel_id": "UCabc", "title": "T", "handle": "t"}
        with patch("app.youtube.capture.resolve_channel", new=AsyncMock(return_value=resolved)):
            ch = (await client.post("/api/youtube/channels", json={"url": "u"})).json()

        vod_info = {"video_id": "vid123", "title": "Yesterday's stream", "channel_id": "UCabc"}
        with patch("app.youtube.capture.resolve_vod", new=AsyncMock(return_value=vod_info)):
            resp = await client.post("/api/youtube/vod",
                                     json={"yt_channel_id": ch["id"], "url": "https://yt/v"})
        assert resp.status_code == 200
        stream_id = resp.json()["stream_id"]

        streams = (await client.get(f"/api/youtube/streams?channel_id={ch['id']}")).json()
        assert streams[0]["status"] == "pending"

        # simulate processed extraction then label it
        async with db.session_factory()() as session:
            x = YtExtractedSignal(stream_id=stream_id, transcript_excerpt="nifty call lo",
                                  parsed=signal_payload(), confidence=0.8)
            session.add(x)
            await session.commit()
            xid = x.id

        resp = await client.post("/api/youtube/feedback", json={
            "stream_id": stream_id, "yt_extracted_signal_id": xid, "label": "correct",
        })
        assert resp.status_code == 200

        detail = (await client.get(f"/api/youtube/streams/{stream_id}")).json()
        assert detail["extracted"][0]["feedback"] == "correct"

        profile = (await client.post(f"/api/youtube/channels/{ch['id']}/rebuild-profile")).json()
        assert profile["examples"] == 1
        assert profile["precision"] == 1.0
