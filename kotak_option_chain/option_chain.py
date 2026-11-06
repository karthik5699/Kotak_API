"""
Complete Option Chain & Market Data Service for Kotak Neo API.

Fetches and unifies:
1. Active Expiries (`client.expiries`)
2. Option Chain Strikes (`client.option_chain(..., instrument_type="option")`)
3. Underlying Futures Chain (`client.option_chain(..., instrument_type="fut")`)
4. Underlying Spot Quotes & 5-Level Option Market Depth (`client.quotes`)
5. Per-Strike Implied Volatility (IV) and Greeks (Delta, Gamma, Vega, Theta, Rho)
6. Chain-Level Quant Analytics (PCR, Max Pain, OI Support/Resistance Walls, ATM Straddle)
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any, Literal

from kotak_option_chain.auth import AuthManager, AuthStateError
from kotak_option_chain.config import IST
from kotak_option_chain.greeks import (
    DEFAULT_RISK_FREE_RATE,
    calculate_option_greeks,
    compute_time_to_expiry_years,
)

# Standard NSE/BSE Index Spot Tokens for `client.quotes()`
INDEX_SPOT_TOKENS: dict[str, dict[str, str]] = {
    "NIFTY": {"exchange_segment": "nse_cm", "instrument_token": "Nifty 50"},
    "NIFTY50": {"exchange_segment": "nse_cm", "instrument_token": "Nifty 50"},
    "BANKNIFTY": {"exchange_segment": "nse_cm", "instrument_token": "Nifty Bank"},
    "FINNIFTY": {"exchange_segment": "nse_cm", "instrument_token": "Nifty Fin Service"},
    "MIDCPNIFTY": {"exchange_segment": "nse_cm", "instrument_token": "NIFTY MID SELECT"},
    "NXT50": {"exchange_segment": "nse_cm", "instrument_token": "Nifty Next 50"},
    "SENSEX": {"exchange_segment": "bse_cm", "instrument_token": "SENSEX"},
    "BANKEX": {"exchange_segment": "bse_cm", "instrument_token": "BANKEX"},
}


def _to_float(val: Any, default: float | None = None) -> float | None:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _to_int(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def normalize_strike_count(count: int | None, default: int = 20) -> int:
    """
    Kotak's `option_chain` API expects `count` to be a positive multiple of 10 (e.g. 10, 20, 40).
    """
    if count is None or count <= 0:
        return default
    rounded = int(math.ceil(count / 10.0) * 10)
    return max(10, min(rounded, 100))


def _is_session_expired_response(resp: Any) -> bool:
    """Detect 401/403 session expiration responses returned by the Kotak Neo SDK."""
    if not isinstance(resp, dict):
        return False
    status_code = resp.get("StatusCode") or resp.get("code")
    if str(status_code) in ("401", "403"):
        return True
    err = resp.get("error") or resp.get("Error") or resp.get("message") or ""
    err_str = str(err).lower()
    return "invalid session" in err_str or "invalid jwt" in err_str or "unauthorised" in err_str


class OptionChainService:
    """
    High-level service for querying all aspects of the Kotak Neo Option Chain and calculating Greeks.
    """

    def __init__(self, auth_manager: AuthManager | None = None) -> None:
        self.auth = auth_manager or AuthManager()

    def get_expiries(
        self,
        underlying: str,
        exchange: str = "nse_fo",
        instrument_type: str = "option",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Fetch all active ISO expiry dates (`YYYY-MM-DD`) for an underlying symbol.
        """
        symbol = underlying.strip().upper()
        exch = exchange.strip().lower()
        try:
            client, _, _ = self.auth.require_authenticated_client(now=now)
        except AuthStateError as exc:
            return exc.payload

        resp = client.expiries(
            exchange=exch,
            underlying=symbol,
            instrument_type=instrument_type,
        )

        if _is_session_expired_response(resp):
            self.auth.invalidate_daily_session()
            return self.auth.get_auth_status(now=now)

        expiries_list: list[str] = []
        if isinstance(resp, dict):
            raw_exp = resp.get("expiries") or (resp.get("data") or {}).get("expiries") or []
            if isinstance(raw_exp, list):
                expiries_list = sorted(str(e) for e in raw_exp if e)

        return {
            "status": "OK",
            "exchange": exch,
            "underlying": symbol,
            "instrument_type": instrument_type,
            "nearest_expiry": expiries_list[0] if expiries_list else None,
            "expiries": expiries_list,
            "count": len(expiries_list),
            "raw_rate_limit": resp.get("rateLimit") if isinstance(resp, dict) else None,
        }

    def get_futures_chain(
        self,
        underlying: str,
        exchange: str = "nse_fo",
        expiry: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Fetch the underlying Futures contracts (`instrument_type='fut'`) for an underlying.
        """
        symbol = underlying.strip().upper()
        exch = exchange.strip().lower()
        try:
            client, _, _ = self.auth.require_authenticated_client(now=now)
        except AuthStateError as exc:
            return exc.payload

        resp = client.option_chain(
            exchange=exch,
            underlying=symbol,
            expiry=expiry,
            instrument_type="fut",
        )

        if _is_session_expired_response(resp):
            self.auth.invalidate_daily_session()
            return self.auth.get_auth_status(now=now)

        data = (
            (resp.get("data") if isinstance(resp, dict) and "data" in resp else resp)
            if isinstance(resp, dict)
            else None
        ) or {}
        common = data.get("common_data") or {}
        fut_raw = data.get("fut") or data.get("future_contracts") or []
        if not fut_raw and isinstance(data.get("future"), dict):
            f_obj = data["future"]
            fut_raw = [
                {
                    "inst": {
                        "neoSymbol": f"nse_fo|{f_obj.get('exchId')}",
                        "symbol": f_obj.get("symbol"),
                        "expiryDt": f_obj.get("expiry"),
                    },
                    "quote": {"ltp": f_obj.get("ltp"), "pc": f_obj.get("prevClose")},
                    "oi": {},
                }
            ]

        contracts = []
        for item in fut_raw:
            inst = item.get("inst") or item.get("instrument") or {}
            quote = item.get("quote") or {}
            oi = item.get("oi") or item.get("openInterest") or {}

            ltp = _to_float(quote.get("ltp"))
            prev_close = _to_float(quote.get("pc") or quote.get("prevClose"))
            net_chg = round(ltp - prev_close, 4) if (ltp is not None and prev_close) else None
            pct_chg = (
                round((net_chg / prev_close) * 100.0, 2)
                if (net_chg is not None and prev_close)
                else None
            )

            contracts.append(
                {
                    "neo_symbol": inst.get("neoSymbol"),
                    "trading_symbol": inst.get("symbol"),
                    "expiry_date": inst.get("expiryDt"),
                    "ltp": ltp,
                    "open": _to_float(quote.get("o") or quote.get("open")),
                    "high": _to_float(quote.get("h") or quote.get("high")),
                    "low": _to_float(quote.get("l") or quote.get("low")),
                    "close": _to_float(quote.get("c") or quote.get("close")),
                    "prev_close": prev_close,
                    "net_change": net_chg,
                    "pct_change": pct_chg,
                    "volume": _to_int(quote.get("vol") or quote.get("volume")),
                    "oi_current": _to_int(oi.get("cur") or oi.get("current")),
                    "oi_previous": _to_int(oi.get("prev") or oi.get("previous")),
                    "oi_change": _to_int(oi.get("chg") or oi.get("change")),
                    "oi_change_pct": _to_float(oi.get("chgPct") or oi.get("changePct")),
                }
            )

        return {
            "status": "OK",
            "exchange": exch,
            "underlying": symbol,
            "lot_size": _to_int(common.get("mktLot"), 1),
            "multiplier": _to_int(common.get("multiplier"), 1),
            "contracts": contracts,
            "nearest_futures_ltp": contracts[0]["ltp"] if contracts else None,
        }

    def _fetch_spot_quote(self, client: Any, symbol: str) -> dict[str, Any] | None:
        """
        Best-effort lookup of the cash/index spot quote via `client.quotes()`.
        """
        token_info = INDEX_SPOT_TOKENS.get(symbol.upper())
        if not token_info:
            return None
        try:
            quotes_resp = client.quotes(
                instrument_tokens=[token_info],
                quote_type="all",
            )
            if isinstance(quotes_resp, list) and quotes_resp:
                q = quotes_resp[0]
                ltp = _to_float(q.get("ltp"))
                if ltp and ltp > 0:
                    return {
                        "display_symbol": q.get("display_symbol") or symbol,
                        "exchange": q.get("exchange") or token_info["exchange_segment"],
                        "ltp": ltp,
                        "change": _to_float(q.get("change")),
                        "per_change": _to_float(q.get("per_change")),
                        "ohlc": q.get("ohlc"),
                        "year_high": _to_float(q.get("year_high")),
                        "year_low": _to_float(q.get("year_low")),
                    }
        except Exception:
            pass
        return None

    def _fetch_depth_by_neosymbol(
        self,
        client: Any,
        neo_symbols: list[str],
    ) -> dict[str, dict[str, Any]]:
        """
        Fetch 5-level market depth and full quotes for a list of `neoSymbol` strings
        (e.g. `nse_fo|71472`), respecting Kotak's 50-instruments-per-call limit.
        """
        tokens: list[dict[str, str]] = []
        for ns in neo_symbols:
            if not ns or "|" not in ns:
                continue
            seg, tok = ns.split("|", 1)
            tokens.append({"exchange_segment": seg, "instrument_token": tok})

        depth_map: dict[str, dict[str, Any]] = {}
        batch_size = 25
        for i in range(0, len(tokens), batch_size):
            batch = tokens[i : i + batch_size]
            try:
                resp = client.quotes(instrument_tokens=batch, quote_type="all")
                if isinstance(resp, list):
                    for q in resp:
                        exch = q.get("exchange", "")
                        etok = str(q.get("exchange_token", ""))
                        key = f"{exch}|{etok}"
                        depth = q.get("depth") or {}
                        buys = depth.get("buy") or []
                        sells = depth.get("sell") or []
                        best_bid = _to_float(buys[0].get("price")) if buys else None
                        best_ask = _to_float(sells[0].get("price")) if sells else None
                        spread = (
                            round(best_ask - best_bid, 4)
                            if (best_bid is not None and best_ask is not None and best_ask >= best_bid > 0)
                            else None
                        )
                        depth_map[key] = {
                            "best_bid": best_bid,
                            "best_ask": best_ask,
                            "bid_ask_spread": spread,
                            "vwap": _to_float(q.get("avg_cost")),
                            "total_buy_qty": _to_int(q.get("total_buy")),
                            "total_sell_qty": _to_int(q.get("total_sell")),
                            "lower_circuit": _to_float(q.get("low_price_range")),
                            "upper_circuit": _to_float(q.get("high_price_range")),
                            "depth": depth,
                        }
            except Exception:
                pass
            if i + batch_size < len(tokens):
                time.sleep(0.04)  # Stay under 25 req/sec limit
        return depth_map

    @staticmethod
    def _parse_option_leg(
        raw_leg: dict[str, Any],
        lot_size: int,
    ) -> dict[str, Any]:
        inst = raw_leg.get("instrument") or raw_leg.get("inst") or {}
        quote = raw_leg.get("quote") or {}
        oi = raw_leg.get("openInterest") or raw_leg.get("oi") or {}

        ltp = _to_float(quote.get("ltp"))
        prev_close = _to_float(quote.get("prevClose") or quote.get("pc"))
        net_change = (
            round(ltp - prev_close, 4)
            if (ltp is not None and prev_close is not None and prev_close > 0)
            else None
        )
        pct_change = (
            round((net_change / prev_close) * 100.0, 2)
            if (net_change is not None and prev_close and prev_close > 0)
            else None
        )

        oi_cur = _to_int(oi.get("current") if "current" in oi else oi.get("cur"))
        oi_prev = _to_int(oi.get("previous") if "previous" in oi else oi.get("prev"))
        # Kotak's raw `oi.chg` on some endpoints echoes price change instead of OI delta;
        # always compute true OI change from `oi_cur - oi_prev` when `oi_prev` is available.
        if oi_cur > 0 or oi_prev > 0:
            oi_chg = oi_cur - oi_prev
            oi_chg_pct = round((oi_chg / oi_prev) * 100.0, 2) if oi_prev > 0 else None
        else:
            oi_chg = _to_int(oi.get("change") if "change" in oi else oi.get("chg"))
            oi_chg_pct = _to_float(oi.get("changePct") if "changePct" in oi else oi.get("chgPct"))

        raw_moneyness = inst.get("moneyness")
        moneyness = str(raw_moneyness).upper() if raw_moneyness else None
        strike_val = _to_float(inst.get("strikePrice") or inst.get("strkPrc"), 0.0) or 0.0
        opt_type = inst.get("optionType") or inst.get("optType")

        safe_lot = max(1, lot_size)
        return {
            "neo_symbol": inst.get("neoSymbol"),
            "trading_symbol": inst.get("symbol"),
            "option_type": opt_type,
            "strike_price": strike_val,
            "moneyness": moneyness,
            "ltp": ltp,
            "open": _to_float(quote.get("open") or quote.get("o")),
            "high": _to_float(quote.get("high") or quote.get("h")),
            "low": _to_float(quote.get("low") or quote.get("l")),
            "prev_close": prev_close,
            "close": _to_float(quote.get("close") or quote.get("c")),
            "net_change": net_change,
            "pct_change": pct_change,
            "volume": _to_int(quote.get("volume") or quote.get("vol")),
            "oi_current": oi_cur,
            "oi_previous": oi_prev,
            "oi_change": oi_chg,
            "oi_change_pct": oi_chg_pct,
            "oi_lots": round(oi_cur / safe_lot, 1),
            "oi_change_lots": round(oi_chg / safe_lot, 1),
        }

    @staticmethod
    def _compute_max_pain(strikes_data: list[dict[str, Any]]) -> float | None:
        """
        Compute the Max Pain strike price: the expiry settlement price among listed strikes
        where the total intrinsic value of all open Call and Put options is minimized.
        """
        if not strikes_data:
            return None
        candidate_strikes = [row["strike_price"] for row in strikes_data if row.get("strike_price")]
        if not candidate_strikes:
            return None

        min_payout: float | None = None
        max_pain_strike: float | None = None

        for settlement in candidate_strikes:
            total_payout = 0.0
            for row in strikes_data:
                k = row["strike_price"]
                ce_oi = (row.get("CE") or {}).get("oi_current") or 0
                pe_oi = (row.get("PE") or {}).get("oi_current") or 0
                if settlement > k and ce_oi > 0:
                    total_payout += (settlement - k) * ce_oi
                if settlement < k and pe_oi > 0:
                    total_payout += (k - settlement) * pe_oi
            if min_payout is None or total_payout < min_payout:
                min_payout = total_payout
                max_pain_strike = settlement

        return max_pain_strike

    def get_full_option_chain(
        self,
        underlying: str,
        exchange: str = "nse_fo",
        expiry: str | None = None,
        count: int = 20,
        underlying_price_override: float | None = None,
        pricing_model: Literal["black76", "bsm"] = "black76",
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        dividend_yield: float = 0.0,
        include_depth: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Fetch all aspects of the Option Chain for an underlying, compute IV and Greeks
        (Delta, Gamma, Vega, Theta, Rho) for every Call and Put strike, and compute
        chain-level analytics (PCR, Max Pain, OI Walls, ATM Straddle).
        """
        symbol = underlying.strip().upper()
        exch = exchange.strip().lower()
        normalized_count = normalize_strike_count(count, default=20)

        try:
            client, _, _ = self.auth.require_authenticated_client(now=now)
        except AuthStateError as exc:
            return exc.payload

        # 1. Fetch Option Chain from Kotak Neo API
        oc_resp = client.option_chain(
            exchange=exch,
            underlying=symbol,
            expiry=expiry,
            instrument_type="option",
            count=normalized_count,
        )

        if _is_session_expired_response(oc_resp):
            self.auth.invalidate_daily_session()
            return self.auth.get_auth_status(now=now)

        data = (
            (oc_resp.get("data") if isinstance(oc_resp, dict) and "data" in oc_resp else oc_resp)
            if isinstance(oc_resp, dict)
            else None
        ) or {}
        if not data or (isinstance(oc_resp, dict) and ("Error" in oc_resp or "error" in oc_resp)):
            return {
                "status": "API_ERROR",
                "exchange": exch,
                "underlying": symbol,
                "error": oc_resp,
            }

        common = data.get("common_data") or {}
        lot_size = _to_int(common.get("mktLot"), 1)
        multiplier = _to_int(common.get("multiplier"), 1)
        resolved_expiry = common.get("expiryDt") or expiry

        raw_calls = data.get("call") or []
        raw_puts = data.get("put") or []
        inline_spot = data.get("spot") if isinstance(data.get("spot"), dict) else None
        inline_future = data.get("future") if isinstance(data.get("future"), dict) else None

        # 2. Group Calls & Puts by Strike Price
        strike_map: dict[float, dict[str, Any]] = {}
        all_neo_symbols: list[str] = []

        for c in raw_calls:
            parsed_ce = self._parse_option_leg(c, lot_size=lot_size)
            k = parsed_ce["strike_price"]
            strike_map.setdefault(k, {"strike_price": k, "CE": None, "PE": None})["CE"] = parsed_ce
            if parsed_ce.get("neo_symbol"):
                all_neo_symbols.append(parsed_ce["neo_symbol"])

        for p in raw_puts:
            parsed_pe = self._parse_option_leg(p, lot_size=lot_size)
            k = parsed_pe["strike_price"]
            strike_map.setdefault(k, {"strike_price": k, "CE": None, "PE": None})["PE"] = parsed_pe
            if parsed_pe.get("neo_symbol"):
                all_neo_symbols.append(parsed_pe["neo_symbol"])

        sorted_strikes = [strike_map[k] for k in sorted(strike_map.keys())]

        # 3. Discover Underlying Spot Price & Futures Price
        spot_price: float | None = _to_float(inline_spot.get("ltp")) if inline_spot else None
        spot_quote: dict[str, Any] | None = None
        if spot_price and inline_spot:
            spot_prev = _to_float(inline_spot.get("prevClose"))
            spot_chg = round(spot_price - spot_prev, 2) if spot_prev else None
            spot_quote = {
                "display_symbol": inline_spot.get("symbol") or symbol,
                "exchange": inline_spot.get("exSeg") or "nse_cm",
                "ltp": spot_price,
                "prev_close": spot_prev,
                "change": spot_chg,
                "per_change": round((spot_chg / spot_prev) * 100.0, 2) if (spot_chg is not None and spot_prev) else None,
            }
        else:
            spot_quote = self._fetch_spot_quote(client, symbol)
            spot_price = spot_quote["ltp"] if spot_quote else None

        futures_price: float | None = _to_float(inline_future.get("ltp")) if inline_future else None
        futures_contracts: list[dict[str, Any]] = []
        if futures_price and inline_future:
            futures_contracts = [
                {
                    "neo_symbol": f"{exch}|{inline_future.get('exchId')}",
                    "trading_symbol": inline_future.get("symbol"),
                    "expiry_date": inline_future.get("expiry"),
                    "ltp": futures_price,
                }
            ]
        else:
            futures_info = self.get_futures_chain(
                underlying=symbol,
                exchange=exch,
                now=now,
            )
            futures_contracts = (
                futures_info.get("contracts", [])
                if futures_info.get("status") == "OK"
                else []
            )
            futures_price = (
                futures_info.get("nearest_futures_ltp")
                if futures_info.get("status") == "OK"
                else None
            )

        # Compute time to expiry (in years)
        if resolved_expiry:
            time_to_expiry_years = compute_time_to_expiry_years(resolved_expiry, now=now)
        else:
            time_to_expiry_years = 7.0 / 365.0
        days_to_expiry = round(time_to_expiry_years * 365.0, 2)

        # Fallback: Implied Forward from ATM Put-Call Parity F = K + exp(r*T)*(C - P)
        implied_forward: float | None = None
        atm_candidates = [
            row
            for row in sorted_strikes
            if (row.get("CE") and row["CE"].get("moneyness") == "ATM")
            or (row.get("PE") and row["PE"].get("moneyness") == "ATM")
        ]
        if not atm_candidates and sorted_strikes:
            # Find strike where |CE_ltp - PE_ltp| is smallest among traded strikes
            valid_pairs = [
                row
                for row in sorted_strikes
                if (row.get("CE") or {}).get("ltp") and (row.get("PE") or {}).get("ltp")
            ]
            if valid_pairs:
                atm_candidates = [
                    min(
                        valid_pairs,
                        key=lambda r: abs((r["CE"]["ltp"] or 0.0) - (r["PE"]["ltp"] or 0.0)),
                    )
                ]

        if atm_candidates:
            atm_row = atm_candidates[0]
            ce_ltp = (atm_row.get("CE") or {}).get("ltp")
            pe_ltp = (atm_row.get("PE") or {}).get("ltp")
            if ce_ltp is not None and pe_ltp is not None:
                implied_forward = round(
                    atm_row["strike_price"]
                    + math.exp(risk_free_rate * time_to_expiry_years) * (ce_ltp - pe_ltp),
                    2,
                )

        # Select reference underlying price for Greeks
        futures_expiry = inline_future.get("expiry") if inline_future else None
        futures_matches_expiry = bool(
            futures_price and futures_expiry and resolved_expiry and futures_expiry == resolved_expiry
        )

        if underlying_price_override is not None and underlying_price_override > 0:
            reference_price = float(underlying_price_override)
            reference_source = "override"
            use_futures_model = pricing_model == "black76"
        elif pricing_model == "bsm" and spot_price and spot_price > 0:
            reference_price = float(spot_price)
            reference_source = "spot_ltp"
            use_futures_model = False
        elif pricing_model == "black76" and futures_matches_expiry and futures_price:
            reference_price = float(futures_price)
            reference_source = "futures_ltp"
            use_futures_model = True
        elif pricing_model == "black76" and implied_forward and implied_forward > 0:
            reference_price = float(implied_forward)
            reference_source = "put_call_parity_forward"
            use_futures_model = True
        elif pricing_model == "black76" and futures_price and futures_price > 0:
            reference_price = float(futures_price)
            reference_source = "futures_ltp"
            use_futures_model = True
        elif spot_price and spot_price > 0:
            reference_price = float(spot_price)
            reference_source = "spot_ltp"
            use_futures_model = False
        elif implied_forward and implied_forward > 0:
            reference_price = float(implied_forward)
            reference_source = "put_call_parity_forward"
            use_futures_model = True
        elif sorted_strikes:
            reference_price = float(sorted_strikes[len(sorted_strikes) // 2]["strike_price"])
            reference_source = "median_strike_fallback"
            use_futures_model = True
        else:
            reference_price = 0.0
            reference_source = "unavailable"
            use_futures_model = True

        # 4. Optional Market Depth Enrichment
        depth_by_symbol: dict[str, dict[str, Any]] = {}
        if include_depth and all_neo_symbols:
            depth_by_symbol = self._fetch_depth_by_neosymbol(client, all_neo_symbols)

        # 5. Compute IV & Greeks for Every Strike
        for row in sorted_strikes:
            k = row["strike_price"]
            for side in ("CE", "PE"):
                leg = row.get(side)
                if not leg:
                    continue
                if include_depth and leg.get("neo_symbol") in depth_by_symbol:
                    leg["market_depth"] = depth_by_symbol[leg["neo_symbol"]]
                greeks_obj = calculate_option_greeks(
                    market_price=leg.get("ltp"),
                    underlying_price=reference_price,
                    strike_price=k,
                    time_to_expiry=time_to_expiry_years,
                    option_type=side,  # type: ignore[arg-type]
                    risk_free_rate=risk_free_rate,
                    dividend_yield=dividend_yield,
                    is_futures_model=use_futures_model,
                )
                leg["greeks"] = greeks_obj.to_dict()

        # 6. Compute Chain-Wide Analytics (ATM, Straddle, PCR, Max Pain, OI Walls)
        analytics = self._compute_chain_analytics(
            sorted_strikes=sorted_strikes,
            reference_price=reference_price,
        )

        now_ist = (now or datetime.now(IST)).astimezone(IST)
        return {
            "status": "OK",
            "fetched_at_ist": now_ist.isoformat(timespec="seconds"),
            "exchange": exch,
            "underlying": symbol,
            "expiry": resolved_expiry,
            "days_to_expiry": days_to_expiry,
            "time_to_expiry_years": round(time_to_expiry_years, 6),
            "lot_size": lot_size,
            "multiplier": multiplier,
            "underlying_prices": {
                "reference_price": reference_price,
                "reference_source": reference_source,
                "pricing_model": "black76" if use_futures_model else "bsm",
                "risk_free_rate": risk_free_rate,
                "spot_price": spot_price,
                "spot_quote": spot_quote,
                "futures_price": futures_price,
                "implied_forward_price": implied_forward,
                "futures_contracts": futures_contracts,
            },
            "analytics": analytics,
            "strikes_count": len(sorted_strikes),
            "strikes": sorted_strikes,
            "raw_rate_limit": oc_resp.get("rateLimit") if isinstance(oc_resp, dict) else None,
        }

    def _compute_chain_analytics(
        self,
        sorted_strikes: list[dict[str, Any]],
        reference_price: float,
    ) -> dict[str, Any]:
        if not sorted_strikes:
            return {}

        total_ce_oi = sum((r.get("CE") or {}).get("oi_current", 0) for r in sorted_strikes)
        total_pe_oi = sum((r.get("PE") or {}).get("oi_current", 0) for r in sorted_strikes)
        total_ce_oi_chg = sum((r.get("CE") or {}).get("oi_change", 0) for r in sorted_strikes)
        total_pe_oi_chg = sum((r.get("PE") or {}).get("oi_change", 0) for r in sorted_strikes)
        total_ce_vol = sum((r.get("CE") or {}).get("volume", 0) for r in sorted_strikes)
        total_pe_vol = sum((r.get("PE") or {}).get("volume", 0) for r in sorted_strikes)

        oi_pcr = round(total_pe_oi / total_ce_oi, 4) if total_ce_oi > 0 else None
        vol_pcr = round(total_pe_vol / total_ce_vol, 4) if total_ce_vol > 0 else None

        if oi_pcr is None:
            sentiment = "Unknown"
        elif oi_pcr >= 1.2:
            sentiment = "Bullish (High Put Writing)"
        elif oi_pcr <= 0.8:
            sentiment = "Bearish (High Call Writing)"
        else:
            sentiment = "Neutral / Rangebound"

        # Find ATM strike
        atm_row = min(
            sorted_strikes,
            key=lambda r: abs(r["strike_price"] - reference_price) if reference_price > 0 else 0.0,
        )
        atm_strike = atm_row["strike_price"]
        atm_ce = atm_row.get("CE") or {}
        atm_pe = atm_row.get("PE") or {}
        atm_ce_ltp = atm_ce.get("ltp")
        atm_pe_ltp = atm_pe.get("ltp")
        atm_ce_iv = (atm_ce.get("greeks") or {}).get("iv")
        atm_pe_iv = (atm_pe.get("greeks") or {}).get("iv")

        straddle_premium: float | None = None
        if atm_ce_ltp is not None and atm_pe_ltp is not None:
            straddle_premium = round(atm_ce_ltp + atm_pe_ltp, 2)

        avg_iv: float | None = None
        if atm_ce_iv is not None and atm_pe_iv is not None:
            avg_iv = round((atm_ce_iv + atm_pe_iv) / 2.0, 2)
        elif atm_ce_iv is not None or atm_pe_iv is not None:
            avg_iv = atm_ce_iv or atm_pe_iv

        # Top 3 Call OI (Resistance) & Top 3 Put OI (Support)
        ce_sorted_by_oi = sorted(
            [r for r in sorted_strikes if (r.get("CE") or {}).get("oi_current", 0) > 0],
            key=lambda r: r["CE"]["oi_current"],
            reverse=True,
        )[:3]
        pe_sorted_by_oi = sorted(
            [r for r in sorted_strikes if (r.get("PE") or {}).get("oi_current", 0) > 0],
            key=lambda r: r["PE"]["oi_current"],
            reverse=True,
        )[:3]

        ce_sorted_by_chg = sorted(
            [r for r in sorted_strikes if (r.get("CE") or {}).get("oi_change", 0) > 0],
            key=lambda r: r["CE"]["oi_change"],
            reverse=True,
        )[:3]
        pe_sorted_by_chg = sorted(
            [r for r in sorted_strikes if (r.get("PE") or {}).get("oi_change", 0) > 0],
            key=lambda r: r["PE"]["oi_change"],
            reverse=True,
        )[:3]

        max_pain = self._compute_max_pain(sorted_strikes)

        return {
            "atm_strike": atm_strike,
            "atm_straddle": {
                "strike": atm_strike,
                "ce_ltp": atm_ce_ltp,
                "pe_ltp": atm_pe_ltp,
                "combined_premium": straddle_premium,
                "lower_breakeven": round(atm_strike - straddle_premium, 2) if straddle_premium else None,
                "upper_breakeven": round(atm_strike + straddle_premium, 2) if straddle_premium else None,
                "implied_move_pct": (
                    round((straddle_premium / reference_price) * 100.0, 2)
                    if (straddle_premium and reference_price > 0)
                    else None
                ),
                "atm_ce_iv": atm_ce_iv,
                "atm_pe_iv": atm_pe_iv,
                "atm_avg_iv": avg_iv,
            },
            "pcr": {
                "oi_pcr": oi_pcr,
                "volume_pcr": vol_pcr,
                "sentiment": sentiment,
            },
            "max_pain_strike": max_pain,
            "totals": {
                "total_call_oi": total_ce_oi,
                "total_put_oi": total_pe_oi,
                "total_call_oi_change": total_ce_oi_chg,
                "total_put_oi_change": total_pe_oi_chg,
                "total_call_volume": total_ce_vol,
                "total_put_volume": total_pe_vol,
            },
            "oi_walls": {
                "call_resistance": [
                    {
                        "strike": r["strike_price"],
                        "oi": r["CE"]["oi_current"],
                        "oi_lots": r["CE"]["oi_lots"],
                        "oi_change": r["CE"]["oi_change"],
                        "ltp": r["CE"]["ltp"],
                    }
                    for r in ce_sorted_by_oi
                ],
                "put_support": [
                    {
                        "strike": r["strike_price"],
                        "oi": r["PE"]["oi_current"],
                        "oi_lots": r["PE"]["oi_lots"],
                        "oi_change": r["PE"]["oi_change"],
                        "ltp": r["PE"]["ltp"],
                    }
                    for r in pe_sorted_by_oi
                ],
                "top_call_oi_additions": [
                    {
                        "strike": r["strike_price"],
                        "oi_change": r["CE"]["oi_change"],
                        "oi_change_lots": r["CE"]["oi_change_lots"],
                        "ltp": r["CE"]["ltp"],
                    }
                    for r in ce_sorted_by_chg
                ],
                "top_put_oi_additions": [
                    {
                        "strike": r["strike_price"],
                        "oi_change": r["PE"]["oi_change"],
                        "oi_change_lots": r["PE"]["oi_change_lots"],
                        "ltp": r["PE"]["ltp"],
                    }
                    for r in pe_sorted_by_chg
                ],
            },
        }
