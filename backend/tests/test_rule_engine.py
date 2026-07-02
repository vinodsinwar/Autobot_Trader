"""Golden tests for the deterministic parser over real-world signal formats."""
import pytest

from app.parsing.rule_engine import RulePack, get_pack, looks_like_signal, parse_with_rules
from app.parsing.schema import Action, EntryType, InstrumentKind, OptionType, SignalKind


def parse(text: str, pack: RulePack | None = None):
    result = parse_with_rules(text, pack)
    assert result.ok, f"{text!r} failed: {result.error}"
    return result.signal


class TestIndexOptions:
    def test_classic_above_format(self):
        s = parse("BUY NIFTY 25000 CE ABOVE 150 TGT 170/190 SL 130")
        assert s.action == Action.BUY
        assert s.symbol == "NIFTY"
        assert s.instrument == InstrumentKind.OPTION
        assert s.strike == 25000
        assert s.option_type == OptionType.CE
        assert s.entry_type == EntryType.ABOVE
        assert s.entry_price == 150
        assert s.targets == [170, 190]
        assert s.stop_loss == 130

    def test_at_symbol_entry(self):
        s = parse("NIFTY 25000 PE BUY @ 120 TARGET 140 160 SL 100")
        assert s.option_type == OptionType.PE
        assert s.entry_type == EntryType.LIMIT
        assert s.entry_price == 120
        assert s.targets == [140, 160]

    def test_comma_separated_targets(self):
        s = parse("BUY BANKNIFTY 51000 CE ABOVE 155 TARGETS 170, 190, 210 STOPLOSS 130")
        assert s.targets == [170, 190, 210]
        assert s.stop_loss == 130

    def test_entry_range(self):
        s = parse("BUY FINNIFTY 23500 CE ENTRY 90-95 TGT 110 SL 80")
        assert s.entry_range_low == 90
        assert s.entry_range_high == 95

    def test_call_put_words(self):
        s = parse("BUY NIFTY 24800 PUT AT 110 TGT 130 SL 95")
        assert s.option_type == OptionType.PE

    def test_alias_pack(self):
        s = parse("BUY BNF 51000 CE ABOVE 200 TGT 250 SL 170", get_pack("index-options"))
        assert s.symbol == "BANKNIFTY"

    def test_messy_emoji_message(self):
        s = parse("🚀🚀 JACKPOT CALL 🚀🚀\nBUY NIFTY 25100 CE ABOVE 88\nTGT 105/125/150 💰\nSL 70 ⛔")
        assert s.strike == 25100
        assert s.targets == [105, 125, 150]
        assert s.stop_loss == 70


class TestFuturesAndEquity:
    def test_future_sell(self):
        s = parse("SELL BANKNIFTY FUT @ 51200 TGT 50800 SL 51450")
        assert s.action == Action.SELL
        assert s.instrument == InstrumentKind.FUTURE
        assert s.targets == [50800]
        assert s.stop_loss == 51450

    def test_equity(self):
        s = parse("BUY RELIANCE @ 2900 TARGET 2950 SL 2870")
        assert s.instrument == InstrumentKind.EQUITY
        assert s.entry_price == 2900

    def test_lots_hint(self):
        s = parse("BUY NIFTY FUT @ 25000 TGT 25200 SL 24900 2 LOTS")
        assert s.quantity_hint == "2 lots"


class TestCrypto:
    def test_long_short_words(self):
        s = parse("LONG BTCUSD @ 65000 TP 67000/68000 SL 63500", get_pack("crypto"))
        assert s.action == Action.BUY
        assert s.instrument == InstrumentKind.CRYPTO_PERP
        assert s.targets == [67000, 68000]

    def test_short(self):
        s = parse("SHORT ETHUSD ENTRY 3500 TARGET 3400 STOP 3600", get_pack("crypto"))
        assert s.action == Action.SELL
        assert s.stop_loss == 3600

    def test_crypto_base_detected_without_pack(self):
        s = parse("BUY BTC ABOVE 65200 TGT 66000 SL 64500")
        assert s.instrument == InstrumentKind.CRYPTO_PERP


class TestRejections:
    @pytest.mark.parametrize(
        "text",
        [
            "Good morning traders! Market looking bullish today 📈",
            "Yesterday our NIFTY call gave 40 points profit 🔥 join premium",
            "What a session!",
        ],
    )
    def test_chatter_rejected(self, text):
        assert not parse_with_rules(text).ok

    def test_inconsistent_sl_rejected(self):
        # SL above entry on a BUY = mis-parsed/mis-typed number, must refuse
        r = parse_with_rules("BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 160")
        assert not r.ok
        assert "inconsistent" in r.error

    def test_inconsistent_target_rejected(self):
        r = parse_with_rules("BUY NIFTY 25000 CE ABOVE 150 TGT 140 SL 130")
        assert not r.ok

    def test_both_buy_and_sell_rejected(self):
        r = parse_with_rules("BUY or SELL NIFTY 25000 CE 150 today?")
        assert not r.ok


class TestExit:
    def test_exit_message(self):
        r = parse_with_rules("EXIT NIFTY 25000 CE NOW")
        assert r.ok
        assert r.signal.kind == SignalKind.EXIT


class TestLlmGate:
    def test_signalish_text_passes_gate(self):
        assert looks_like_signal("nifty 25000 ka call lelo sl 130 tgt 170")

    def test_chatter_fails_gate(self):
        assert not looks_like_signal("good morning everyone, have a great day")


class TestDedupHash:
    def test_same_signal_same_hash(self):
        a = parse("BUY NIFTY 25000 CE ABOVE 150 TGT 170/190 SL 130")
        b = parse("🚀 BUY NIFTY 25000 CE ABOVE 150 TARGETS 170 190 STOPLOSS 130 🚀")
        assert a.dedup_hash() == b.dedup_hash()

    def test_different_strike_different_hash(self):
        a = parse("BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130")
        b = parse("BUY NIFTY 25100 CE ABOVE 150 TGT 170 SL 130")
        assert a.dedup_hash() != b.dedup_hash()
