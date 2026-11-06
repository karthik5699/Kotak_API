"""
Unit tests for OptionChainService: parsing, Greeks enrichment, PCR, Max Pain, OI walls, and WhatsApp formatting.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from kotak_option_chain.auth import AuthManager
from kotak_option_chain.config import IST, ConfigStore
from kotak_option_chain.formatters import format_whatsapp_option_chain_summary
from kotak_option_chain.option_chain import OptionChainService
from tests.test_auth_session import FakeNeoAPI


class MockMarketNeoAPI(FakeNeoAPI):
    def expiries(self, exchange: str, underlying: str, instrument_type: str | None = None) -> dict[str, Any]:
        return {
            "exchange": exchange,
            "underlying": underlying,
            "expiries": ["2026-11-12", "2026-11-19", "2026-11-26"],
        }

    def option_chain(
        self,
        exchange: str,
        underlying: str,
        expiry: str | None = None,
        instrument_type: str | None = None,
        count: int | None = None,
    ) -> dict[str, Any]:
        if instrument_type == "fut":
            return {
                "data": {
                    "common_data": {
                        "mktLot": "75",
                        "multiplier": "1",
                        "unlSymbol": underlying,
                        "exSeg": exchange,
                        "expiryDt": None,
                    },
                    "call": [],
                    "put": [],
                    "fut": [
                        {
                            "inst": {
                                "neoSymbol": "nse_fo|53001",
                                "symbol": "NIFTY26NOVFUT",
                                "expiryDt": "26-NOV-2026",
                            },
                            "quote": {
                                "ltp": "24250.00",
                                "o": "24200.00",
                                "h": "24310.00",
                                "l": "24180.00",
                                "c": "24250.00",
                                "pc": "24150.00",
                                "vol": "6500000",
                            },
                            "oi": {
                                "cur": "12500000",
                                "prev": "12000000",
                                "chg": "500000",
                                "chgPct": "4.17",
                            },
                        }
                    ],
                }
            }

        return {
            "data": {
                "common_data": {
                    "mktLot": "75",
                    "multiplier": "1",
                    "unlSymbol": underlying,
                    "exSeg": exchange,
                    "expiryDt": expiry or "2026-11-12",
                },
                "call": [
                    {
                        "instrument": {
                            "neoSymbol": "nse_fo|71001",
                            "symbol": "NIFTY26NOV24200CE",
                            "optionType": "CE",
                            "strikePrice": "24200",
                            "moneyness": "ITM",
                        },
                        "quote": {
                            "ltp": "185.50",
                            "open": "160.00",
                            "high": "195.00",
                            "low": "155.00",
                            "prevClose": "150.00",
                            "close": None,
                            "volume": 12500000,
                        },
                        "openInterest": {
                            "current": 3000000,
                            "previous": 2800000,
                            "change": 200000,
                            "changePct": 7.14,
                        },
                    },
                    {
                        "instrument": {
                            "neoSymbol": "nse_fo|71002",
                            "symbol": "NIFTY26NOV24250CE",
                            "optionType": "CE",
                            "strikePrice": "24250",
                            "moneyness": "ATM",
                        },
                        "quote": {
                            "ltp": "152.00",
                            "open": "130.00",
                            "high": "165.00",
                            "low": "125.00",
                            "prevClose": "128.00",
                            "close": None,
                            "volume": 45000000,
                        },
                        "openInterest": {
                            "current": 8500000,
                            "previous": 7500000,
                            "change": 1000000,
                            "changePct": 13.33,
                        },
                    },
                    {
                        "instrument": {
                            "neoSymbol": "nse_fo|71003",
                            "symbol": "NIFTY26NOV24300CE",
                            "optionType": "CE",
                            "strikePrice": "24300",
                            "moneyness": "OTM",
                        },
                        "quote": {
                            "ltp": "124.00",
                            "open": "110.00",
                            "high": "135.00",
                            "low": "102.00",
                            "prevClose": "105.00",
                            "close": None,
                            "volume": 38000000,
                        },
                        "openInterest": {
                            "current": 12000000,
                            "previous": 10000000,
                            "change": 2000000,
                            "changePct": 20.0,
                        },
                    },
                ],
                "put": [
                    {
                        "instrument": {
                            "neoSymbol": "nse_fo|72001",
                            "symbol": "NIFTY26NOV24200PE",
                            "optionType": "PE",
                            "strikePrice": "24200",
                            "moneyness": "OTM",
                        },
                        "quote": {
                            "ltp": "128.00",
                            "open": "145.00",
                            "high": "150.00",
                            "low": "120.00",
                            "prevClose": "142.00",
                            "close": None,
                            "volume": 32000000,
                        },
                        "openInterest": {
                            "current": 14000000,
                            "previous": 11000000,
                            "change": 3000000,
                            "changePct": 27.27,
                        },
                    },
                    {
                        "instrument": {
                            "neoSymbol": "nse_fo|72002",
                            "symbol": "NIFTY26NOV24250PE",
                            "optionType": "PE",
                            "strikePrice": "24250",
                            "moneyness": "ATM",
                        },
                        "quote": {
                            "ltp": "149.00",
                            "open": "165.00",
                            "high": "172.00",
                            "low": "140.00",
                            "prevClose": "160.00",
                            "close": None,
                            "volume": 41000000,
                        },
                        "openInterest": {
                            "current": 9500000,
                            "previous": 8500000,
                            "change": 1000000,
                            "changePct": 11.76,
                        },
                    },
                    {
                        "instrument": {
                            "neoSymbol": "nse_fo|72003",
                            "symbol": "NIFTY26NOV24300PE",
                            "optionType": "PE",
                            "strikePrice": "24300",
                            "moneyness": "ITM",
                        },
                        "quote": {
                            "ltp": "176.00",
                            "open": "190.00",
                            "high": "198.00",
                            "low": "168.00",
                            "prevClose": "188.00",
                            "close": None,
                            "volume": 15000000,
                        },
                        "openInterest": {
                            "current": 4000000,
                            "previous": 4200000,
                            "change": -200000,
                            "changePct": -4.76,
                        },
                    },
                ],
            }
        }

    def quotes(self, instrument_tokens: list[dict[str, str]], quote_type: str = "all") -> list[dict[str, Any]]:
        results = []
        for item in instrument_tokens:
            tok = item["instrument_token"]
            seg = item["exchange_segment"]
            if tok == "Nifty 50":
                results.append(
                    {
                        "exchange_token": "Nifty 50",
                        "display_symbol": "NIFTY 50",
                        "exchange": seg,
                        "ltp": "24235.50",
                        "change": "85.50",
                        "per_change": "0.35",
                        "ohlc": {"open": "24180.0", "high": "24260.0", "low": "24150.0", "close": "24150.0"},
                    }
                )
            else:
                results.append(
                    {
                        "exchange_token": tok,
                        "exchange": seg,
                        "ltp": "152.00",
                        "avg_cost": "148.50",
                        "total_buy": "250000",
                        "total_sell": "240000",
                        "low_price_range": "5.00",
                        "high_price_range": "450.00",
                        "depth": {
                            "buy": [{"price": "151.80", "quantity": "750", "orders": "5"}],
                            "sell": [{"price": "152.20", "quantity": "600", "orders": "4"}],
                        },
                    }
                )
        return results


def test_full_option_chain_with_greeks_and_analytics(tmp_path: Path, monkeypatch: Any) -> None:
    for env_var in ("NEO_CONSUMER_KEY", "NEO_MOBILE_NUMBER", "NEO_UCC", "NEO_MPIN"):
        monkeypatch.delenv(env_var, raising=False)

    now = datetime(2026, 11, 6, 12, 30, 0, tzinfo=IST)
    store = ConfigStore(data_dir=tmp_path)
    auth = AuthManager(store=store, client_factory=MockMarketNeoAPI)
    auth.save_permanent_credentials(
        consumer_key="ck-test",
        mobile_number="+919876543210",
        ucc="TESTUCC",
        mpin="123456",
        now=now,
    )
    auth.login_with_daily_totp("123456", now=now)

    service = OptionChainService(auth_manager=auth)

    exp_res = service.get_expiries("NIFTY", now=now)
    assert exp_res["status"] == "OK"
    assert exp_res["nearest_expiry"] == "2026-11-12"

    chain = service.get_full_option_chain(
        underlying="NIFTY",
        expiry="2026-11-12",
        count=20,
        include_depth=True,
        now=now,
    )
    assert chain["status"] == "OK"
    assert chain["lot_size"] == 75
    assert chain["underlying_prices"]["spot_price"] == 24235.50
    assert chain["underlying_prices"]["futures_price"] == 24250.00
    assert chain["underlying_prices"]["reference_price"] == 24250.00

    analytics = chain["analytics"]
    assert analytics["atm_strike"] == 24250.0
    assert analytics["atm_straddle"]["combined_premium"] == 301.0  # 152 + 149
    assert analytics["pcr"]["oi_pcr"] == round(27500000 / 23500000, 4)
    assert analytics["max_pain_strike"] == 24250.0
    assert analytics["oi_walls"]["call_resistance"][0]["strike"] == 24300.0
    assert analytics["oi_walls"]["put_support"][0]["strike"] == 24200.0

    # Verify Greeks and Market Depth on ATM strike (24250)
    atm_row = next(r for r in chain["strikes"] if r["strike_price"] == 24250.0)
    ce_greeks = atm_row["CE"]["greeks"]
    pe_greeks = atm_row["PE"]["greeks"]
    assert ce_greeks["iv"] is not None and ce_greeks["iv"] > 0
    assert pe_greeks["iv"] is not None and pe_greeks["iv"] > 0
    assert 0.45 <= ce_greeks["delta"] <= 0.55
    assert -0.55 <= pe_greeks["delta"] <= -0.45
    assert ce_greeks["gamma"] > 0
    assert ce_greeks["vega"] > 0
    assert atm_row["CE"]["market_depth"]["best_bid"] == 151.80
    assert atm_row["CE"]["market_depth"]["best_ask"] == 152.20

    # Verify WhatsApp summary formatting
    wa_text = format_whatsapp_option_chain_summary(chain)
    assert "*NIFTY Option Chain*" in wa_text
    assert "24250*" in wa_text
    assert "Straddle" in wa_text
