"""Scrip-master parsing and signal → instrument resolution."""
from datetime import UTC, datetime, timedelta

from app.db.models import Instrument
from app.instruments.delta_products import parse_product
from app.instruments.dhan_scrip_master import parse_scrip_row
from app.instruments.resolver import ResolutionError, resolve
from app.parsing.rule_engine import parse_with_rules


def days(n: int) -> str:
    return (datetime.now(UTC) + timedelta(days=n)).strftime("%Y-%m-%d")


class TestScripRowParsing:
    def test_option_row(self):
        row = {
            "SEM_EXM_EXCH_ID": "NSE", "SEM_SEGMENT": "D",
            "SEM_SMST_SECURITY_ID": "43492", "SEM_INSTRUMENT_NAME": "OPTIDX",
            "SEM_EXPIRY_DATE": "2026-07-09 14:30:00", "SEM_STRIKE_PRICE": "25000.000000",
            "SEM_OPTION_TYPE": "CE", "SEM_TICK_SIZE": "0.05",
            "SEM_TRADING_SYMBOL": "NIFTY-Jul2026-25000-CE", "SEM_LOT_UNITS": "75",
            "SEM_CUSTOM_SYMBOL": "NIFTY 09 JUL 25000 CALL",
        }
        parsed = parse_scrip_row(row)
        assert parsed["instrument_type"] == "CE"
        assert parsed["underlying"] == "NIFTY"
        assert parsed["strike"] == 25000
        assert parsed["expiry"] == "2026-07-09"
        assert parsed["exchange_segment"] == "NSE_FNO"
        assert parsed["lot_size"] == 75

    def test_currency_options_skipped(self):
        assert parse_scrip_row({
            "SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "OPTCUR",
            "SEM_SMST_SECURITY_ID": "1",
        }) is None

    def test_mcx_skipped(self):
        assert parse_scrip_row({
            "SEM_EXM_EXCH_ID": "MCX", "SEM_INSTRUMENT_NAME": "FUTCOM",
            "SEM_SMST_SECURITY_ID": "1",
        }) is None


class TestDeltaProductParsing:
    def test_perp(self):
        p = {"id": 27, "symbol": "BTCUSD", "state": "live", "contract_value": "0.001",
             "tick_size": "0.5", "description": "Bitcoin Perpetual",
             "underlying_asset": {"symbol": "BTC"}}
        parsed = parse_product(p)
        assert parsed["security_id"] == "27"
        assert parsed["instrument_type"] == "PERP"
        assert parsed["underlying"] == "BTC"

    def test_expired_product_skipped(self):
        assert parse_product({"id": 1, "symbol": "X", "state": "expired"}) is None


async def seed(db, rows):
    async with db.session_factory()() as session:
        for r in rows:
            session.add(Instrument(**r))
        await session.commit()


BASE_OPT = dict(broker="dhan", exchange_segment="NSE_FNO", instrument_type="CE",
                underlying="NIFTY", strike=25000.0, lot_size=75, tick_size=0.05)


class TestResolver:
    async def test_option_nearest_expiry(self, db):
        await seed(db, [
            {**BASE_OPT, "security_id": "1", "symbol": "NIFTY-W1", "expiry": days(7)},
            {**BASE_OPT, "security_id": "2", "symbol": "NIFTY-W2", "expiry": days(14)},
            {**BASE_OPT, "security_id": "3", "symbol": "NIFTY-EXP", "expiry": days(-1)},
        ])
        sig = parse_with_rules("BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130").signal
        async with db.session_factory()() as session:
            inst = await resolve(session, sig, "dhan")
        assert inst.security_id == "1"  # nearest future expiry, never the expired one

    async def test_option_expiry_hint(self, db):
        target_day = datetime.now(UTC) + timedelta(days=14)
        await seed(db, [
            {**BASE_OPT, "security_id": "1", "symbol": "W1", "expiry": days(7)},
            {**BASE_OPT, "security_id": "2", "symbol": "W2",
             "expiry": target_day.strftime("%Y-%m-%d")},
        ])
        sig = parse_with_rules("BUY NIFTY 25000 CE ABOVE 150 TGT 170 SL 130").signal
        sig.expiry_hint = target_day.strftime("%d%b").upper()  # e.g. "16JUL"
        async with db.session_factory()() as session:
            inst = await resolve(session, sig, "dhan")
        assert inst.security_id == "2"

    async def test_crypto_symbol_normalization(self, db):
        await seed(db, [dict(broker="delta", security_id="27", exchange_segment="DELTA",
                             symbol="BTCUSD", instrument_type="PERP", underlying="BTC",
                             lot_size=0.001, tick_size=0.5)])
        sig = parse_with_rules("LONG BTC @ 65000 TP 67000 SL 63500").signal
        async with db.session_factory()() as session:
            inst = await resolve(session, sig, "delta")
        assert inst.security_id == "27"

    async def test_missing_instrument_raises(self, db):
        sig = parse_with_rules("BUY NIFTY 99999 CE ABOVE 150 TGT 170 SL 130").signal
        async with db.session_factory()() as session:
            try:
                await resolve(session, sig, "dhan")
                raise AssertionError("expected ResolutionError")
            except ResolutionError as exc:
                assert "99999" in str(exc)
