"""
FastAPI Backend for the Kotak Neo Options Scalping Simulator (Zerodha Kite Scalper Replica).

Provides:
- `/api/v1/options/expiries`: Multi-expiry list with relative Kite labels ("9 Nov (3 days)", etc.)
- `/api/v1/options/chain`: Strictly ATM ± 5 strikes (11 strikes total) with live LTP, OI, IV, Greeks,
  PCR, Max Pain, ATM IV, and IV Percentile
- `/api/v1/charts/candles`: Real historical candlesticks from Kotak Neo (`client.historical_data`)
  enriched with 20 SMA & 50 EMA and live forming-candle updates
- `/api/v1/paper/*`: Full Paper Trading & Scalp Position Engine with ₹10,00,000 starting virtual capital,
  SPAN/Exposure margin simulation, real-time Unrealized & Realized P&L, and 1-click square-off.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Import `kotak_option_chain` from parent project root
SCALPING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCALPING_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kotak_option_chain.auth import AuthManager, AuthStateError
from kotak_option_chain.config import IST, ConfigStore
from kotak_option_chain.option_chain import INDEX_SPOT_TOKENS, OptionChainService

app = FastAPI(
    title="Kotak Neo Options Scalper Simulator",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = SCALPING_DIR / "static"
PAPER_STATE_FILE = SCALPING_DIR / "paper_account.json"

STRIKE_STEPS: dict[str, int] = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
    "BANKEX": 100,
}

DEFAULT_LOT_SIZES: dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 120,
    "SENSEX": 20,
}

# Approximate short-option margin per lot in ₹ (SPAN + Exposure) for realistic Kite margin readouts
SHORT_MARGIN_PER_LOT: dict[str, float] = {
    "NIFTY": 185_000.0,
    "BANKNIFTY": 165_000.0,
    "FINNIFTY": 155_000.0,
    "MIDCPNIFTY": 135_000.0,
    "SENSEX": 145_000.0,
}

DEFAULT_STARTING_CAPITAL = 1_000_000.0  # ₹10,00,000 (10 Lakhs Paper Capital)

store = ConfigStore()
auth_manager = AuthManager(store=store)
oc_service = OptionChainService(auth_manager=auth_manager)

# Lightweight in-memory caches to avoid redundant API hits and respect rate limits
_expiries_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_candle_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_forming_candles: dict[str, dict[str, Any]] = {}  # "{neosymbol}:{interval}" -> forming OHLCV bar
_prev_forming_candles: dict[str, dict[str, Any]] = {}  # "{neosymbol}:{interval}" -> just-closed bar before historical refresh
_latest_ltp_map: dict[str, float] = {}  # neo_symbol -> latest known LTP
_latest_symbol_meta: dict[str, dict[str, Any]] = {}  # neo_symbol -> contract metadata
_historical_lock = threading.Lock()
_last_historical_call_ts: float = 0.0

INTERVAL_SECONDS: dict[str, int] = {
    "1min": 60,
    "3min": 180,
    "5min": 300,
    "10min": 600,
    "15min": 900,
    "30min": 1800,
    "60min": 3600,
    "D": 86400,
}
IST_OFFSET_SECONDS = 19800  # +05:30 so Lightweight Charts displays IST wall-clock time on the X axis


def format_expiry_option(iso_date: str, now: datetime | None = None) -> dict[str, Any]:
    """
    Format an ISO expiry string ('YYYY-MM-DD') into Zerodha Kite's relative expiry label,
    e.g., '9 Nov (3 days)', '17 Nov (11 days)', '23 Nov (2 weeks)', '8 Dec (1 month)'.
    """
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    try:
        exp_dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(
            hour=15, minute=30, second=0, tzinfo=IST
        )
    except ValueError:
        return {"isoDate": iso_date, "displayLabel": iso_date, "dte": 0}

    diff_seconds = (exp_dt - now_ist).total_seconds()
    dte = max(0, math.ceil(diff_seconds / 86400.0))
    day_num = exp_dt.day
    month_short = exp_dt.strftime("%b")

    if dte == 0:
        rel = "Today"
    elif dte == 1:
        rel = "1 day"
    elif dte < 14:
        rel = f"{dte} days"
    elif dte < 30:
        weeks = max(1, round(dte / 7))
        rel = f"{weeks} {'week' if weeks == 1 else 'weeks'}"
    elif dte < 365:
        months = max(1, round(dte / 30))
        rel = f"{months} {'month' if months == 1 else 'months'}"
    else:
        years = max(1, round(dte / 365))
        rel = f"{years} {'year' if years == 1 else 'years'}"

    return {
        "isoDate": iso_date,
        "displayLabel": f"{day_num} {month_short} ({rel})",
        "shortDate": f"{day_num} {month_short.upper()}",
        "dte": dte,
    }


def format_kite_contract_title(symbol: str, iso_expiry: str, strike: float | int, opt_type: str) -> str:
    """
    Format e.g. ('NIFTY', '2026-11-09', 24650, 'CE') -> 'NIFTY 9th NOV 24650 CE'
    """
    try:
        dt = datetime.strptime(iso_expiry, "%Y-%m-%d")
        d = dt.day
        suffix = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
        month = dt.strftime("%b").upper()
        strike_str = f"{int(strike)}" if float(strike) == int(strike) else f"{strike}"
        return f"{symbol.upper()} {d}{suffix} {month} {strike_str} {opt_type.upper()}"
    except ValueError:
        return f"{symbol.upper()} {iso_expiry} {strike:g} {opt_type.upper()}"


class PaperTradingEngine:
    """
    Manages simulated paper capital, open scalp positions, average entry prices,
    margin utilization, realized P&L, live unrealized P&L, and order history.
    """

    def __init__(self, state_path: Path = PAPER_STATE_FILE) -> None:
        self.state_path = state_path
        self.starting_capital: float = DEFAULT_STARTING_CAPITAL
        self.realized_pnl: float = 0.0
        # positions keyed by `neo_symbol` (e.g. "nse_fo|40009")
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.starting_capital = float(raw.get("starting_capital", DEFAULT_STARTING_CAPITAL))
                self.realized_pnl = float(raw.get("realized_pnl", 0.0))
                self.positions = raw.get("positions", {})
                self.orders = raw.get("orders", [])
            except Exception:
                pass

    def _save(self) -> None:
        payload = {
            "starting_capital": self.starting_capital,
            "realized_pnl": round(self.realized_pnl, 2),
            "positions": self.positions,
            "orders": self.orders[:200],
        }
        try:
            self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def reset(self, starting_capital: float = DEFAULT_STARTING_CAPITAL) -> dict[str, Any]:
        self.starting_capital = float(starting_capital)
        self.realized_pnl = 0.0
        self.positions = {}
        self.orders = []
        self._save()
        return self.get_summary()

    def update_live_prices(self, ltp_map: dict[str, float]) -> None:
        for neo_sym, ltp in ltp_map.items():
            if ltp is not None and ltp > 0:
                _latest_ltp_map[neo_sym] = float(ltp)
                if neo_sym in self.positions:
                    self.positions[neo_sym]["ltp"] = float(ltp)

    def _compute_position_margin(self, pos: dict[str, Any]) -> float:
        qty = int(pos.get("net_qty", 0))
        if qty == 0:
            return 0.0
        if qty > 0:
            # Long option premium paid
            return abs(qty) * float(pos.get("avg_price", 0.0))
        # Short option margin
        lot_size = max(1, int(pos.get("lot_size", 65)))
        lots = abs(qty) / lot_size
        sym = str(pos.get("underlying", "NIFTY")).upper()
        per_lot = SHORT_MARGIN_PER_LOT.get(sym, 180_000.0)
        return lots * per_lot

    def get_summary(self) -> dict[str, Any]:
        unrealized_total = 0.0
        used_margin = 0.0
        enriched_positions: list[dict[str, Any]] = []

        for neo_sym, pos in list(self.positions.items()):
            net_qty = int(pos.get("net_qty", 0))
            if net_qty == 0:
                continue
            avg_price = float(pos.get("avg_price", 0.0))
            live_ltp = float(_latest_ltp_map.get(neo_sym, pos.get("ltp", avg_price)))
            pos["ltp"] = live_ltp

            # Unrealized P&L: (LTP - AvgEntry) * net_qty
            unrealized = round((live_ltp - avg_price) * net_qty, 2)
            invested = abs(net_qty) * avg_price
            pnl_pct = round((unrealized / invested) * 100.0, 2) if invested > 0 else 0.0
            margin_used = round(self._compute_position_margin(pos), 2)

            unrealized_total += unrealized
            used_margin += margin_used

            enriched_positions.append(
                {
                    **pos,
                    "ltp": round(live_ltp, 2),
                    "unrealized_pnl": unrealized,
                    "pnl_pct": pnl_pct,
                    "total_pnl": round(float(pos.get("realized_pnl", 0.0)) + unrealized, 2),
                    "margin_used": margin_used,
                    "side": "LONG" if net_qty > 0 else "SHORT",
                }
            )

        total_mtm = round(self.realized_pnl + unrealized_total, 2)
        equity = round(self.starting_capital + self.realized_pnl, 2)
        available_margin = round(max(0.0, equity - used_margin + min(0.0, unrealized_total)), 2)

        return {
            "starting_capital": round(self.starting_capital, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(unrealized_total, 2),
            "total_mtm": total_mtm,
            "used_margin": round(used_margin, 2),
            "available_margin": available_margin,
            "open_positions_count": len(enriched_positions),
            "positions": enriched_positions,
            "orders": self.orders[:50],
        }

    def execute_order(
        self,
        neo_symbol: str,
        trading_symbol: str,
        display_name: str,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: Literal["CE", "PE"],
        side: Literal["BUY", "SELL"],
        quantity: int,
        price: float | None = None,
        lot_size: int = 65,
    ) -> dict[str, Any]:
        if quantity <= 0:
            raise HTTPException(status_code=400, detail="Order quantity must be > 0")

        exec_price = float(price) if (price is not None and price > 0) else float(_latest_ltp_map.get(neo_symbol, 0.0))
        if exec_price <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"Live price unavailable for {display_name} ({neo_symbol}). Refresh chain first.",
            )

        _latest_ltp_map[neo_symbol] = exec_price
        signed_order_qty = int(quantity) if side == "BUY" else -int(quantity)

        existing = self.positions.get(neo_symbol)
        if not existing or int(existing.get("net_qty", 0)) == 0:
            pos_realized = float(existing.get("realized_pnl", 0.0)) if existing else 0.0
            self.positions[neo_symbol] = {
                "neo_symbol": neo_symbol,
                "trading_symbol": trading_symbol,
                "display_name": display_name,
                "underlying": underlying.upper(),
                "expiry": expiry,
                "strike": float(strike),
                "option_type": option_type.upper(),
                "lot_size": lot_size,
                "net_qty": signed_order_qty,
                "avg_price": round(exec_price, 2),
                "ltp": round(exec_price, 2),
                "realized_pnl": round(pos_realized, 2),
                "updated_at": datetime.now(IST).strftime("%H:%M:%S"),
            }
            trade_realized_delta = 0.0
        else:
            old_qty = int(existing["net_qty"])
            old_avg = float(existing["avg_price"])
            new_qty = old_qty + signed_order_qty
            trade_realized_delta = 0.0

            # Case 1: Adding to existing position in the same direction
            if (old_qty > 0 and signed_order_qty > 0) or (old_qty < 0 and signed_order_qty < 0):
                total_cost = abs(old_qty) * old_avg + abs(signed_order_qty) * exec_price
                new_avg = total_cost / abs(new_qty)
                existing["net_qty"] = new_qty
                existing["avg_price"] = round(new_avg, 2)
            # Case 2: Reducing, closing, or reversing existing position
            else:
                closed_qty = min(abs(old_qty), abs(signed_order_qty))
                if old_qty > 0:
                    # Closing Long by Selling
                    trade_realized_delta = (exec_price - old_avg) * closed_qty
                else:
                    # Closing Short by Buying
                    trade_realized_delta = (old_avg - exec_price) * closed_qty

                self.realized_pnl += trade_realized_delta
                existing["realized_pnl"] = round(float(existing.get("realized_pnl", 0.0)) + trade_realized_delta, 2)
                existing["net_qty"] = new_qty

                if new_qty == 0:
                    existing["avg_price"] = 0.0
                elif (old_qty > 0 and new_qty < 0) or (old_qty < 0 and new_qty > 0):
                    # Reversed position: the remainder opens at exec_price
                    existing["avg_price"] = round(exec_price, 2)

            existing["ltp"] = round(exec_price, 2)
            existing["updated_at"] = datetime.now(IST).strftime("%H:%M:%S")
            if existing["net_qty"] == 0:
                del self.positions[neo_symbol]

        order_record = {
            "order_id": f"P{int(time.time() * 1000) % 10_000_000}",
            "time": datetime.now(IST).strftime("%H:%M:%S"),
            "neo_symbol": neo_symbol,
            "trading_symbol": trading_symbol,
            "display_name": display_name,
            "strike": float(strike),
            "option_type": option_type.upper(),
            "side": side,
            "quantity": int(quantity),
            "price": round(exec_price, 2),
            "value": round(exec_price * int(quantity), 2),
            "realized_pnl_delta": round(trade_realized_delta, 2),
            "status": "COMPLETE",
        }
        self.orders.insert(0, order_record)
        self._save()

        return {
            "status": "OK",
            "order": order_record,
            "account": self.get_summary(),
        }

    def close_position(self, neo_symbol: str, exit_price: float | None = None) -> dict[str, Any]:
        pos = self.positions.get(neo_symbol)
        if not pos or int(pos.get("net_qty", 0)) == 0:
            raise HTTPException(status_code=404, detail=f"No open position for {neo_symbol}")

        net_qty = int(pos["net_qty"])
        close_side: Literal["BUY", "SELL"] = "SELL" if net_qty > 0 else "BUY"
        px = exit_price if (exit_price is not None and exit_price > 0) else float(_latest_ltp_map.get(neo_symbol, pos["ltp"]))

        return self.execute_order(
            neo_symbol=neo_symbol,
            trading_symbol=pos["trading_symbol"],
            display_name=pos["display_name"],
            underlying=pos["underlying"],
            expiry=pos["expiry"],
            strike=float(pos["strike"]),
            option_type=pos["option_type"],
            side=close_side,
            quantity=abs(net_qty),
            price=px,
            lot_size=int(pos.get("lot_size", 65)),
        )

    def close_all_positions(self) -> dict[str, Any]:
        open_syms = [sym for sym, pos in self.positions.items() if int(pos.get("net_qty", 0)) != 0]
        closed_orders = []
        for sym in open_syms:
            res = self.close_position(sym)
            closed_orders.append(res["order"])
        return {
            "status": "OK",
            "closed_count": len(closed_orders),
            "closed_orders": closed_orders,
            "account": self.get_summary(),
        }


paper_engine = PaperTradingEngine()


class OrderRequest(BaseModel):
    neo_symbol: str
    trading_symbol: str = ""
    display_name: str = ""
    underlying: str = "NIFTY"
    expiry: str = ""
    strike: float
    option_type: Literal["CE", "PE"]
    side: Literal["BUY", "SELL"]
    quantity: int = Field(..., gt=0)
    price: float | None = None
    lot_size: int = 65


class ClosePositionRequest(BaseModel):
    neo_symbol: str | None = None
    close_all: bool = False
    exit_price: float | None = None


class ResetAccountRequest(BaseModel):
    starting_capital: float = DEFAULT_STARTING_CAPITAL


def _compute_sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    running_sum = 0.0
    for i, val in enumerate(values):
        running_sum += val
        if i >= period:
            running_sum -= values[i - period]
        if i >= period - 1:
            out.append(round(running_sum / period, 2))
        else:
            out.append(None)
    return out


def _compute_ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    if not values:
        return out
    k = 2.0 / (period + 1.0)
    ema_val: float | None = None
    for i, val in enumerate(values):
        if i < period - 1:
            out.append(None)
        elif i == period - 1:
            ema_val = sum(values[:period]) / period
            out.append(round(ema_val, 2))
        else:
            assert ema_val is not None
            ema_val = val * k + ema_val * (1.0 - k)
            out.append(round(ema_val, 2))
    return out


@app.get("/api/v1/status")
def get_terminal_status() -> dict[str, Any]:
    auth_status = auth_manager.get_auth_status()
    return {
        "auth": auth_status,
        "paper_account": paper_engine.get_summary(),
        "server_time_ist": datetime.now(IST).strftime("%H:%M:%S"),
    }


@app.get("/api/v1/options/expiries")
def get_option_expiries(
    symbol: str = Query("NIFTY"),
    exchange: str = Query("nse_fo"),
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    cache_key = f"{exchange}:{sym}"
    now_ts = time.time()

    if cache_key in _expiries_cache and (now_ts - _expiries_cache[cache_key][0]) < 300:
        formatted = _expiries_cache[cache_key][1]
        return {
            "status": "OK",
            "symbol": sym,
            "exchange": exchange,
            "expiries": formatted,
            "nearestExpiry": formatted[0]["isoDate"] if formatted else None,
        }

    res = oc_service.get_expiries(underlying=sym, exchange=exchange)
    if res.get("status") != "OK":
        return res

    now_ist = datetime.now(IST)
    formatted = [format_expiry_option(iso, now=now_ist) for iso in res.get("expiries", [])]
    _expiries_cache[cache_key] = (now_ts, formatted)

    return {
        "status": "OK",
        "symbol": sym,
        "exchange": exchange,
        "expiries": formatted,
        "nearestExpiry": formatted[0]["isoDate"] if formatted else None,
    }


@app.get("/api/v1/options/chain")
def get_compact_option_chain(
    symbol: str = Query("NIFTY"),
    expiry: str | None = Query(None, description="Expiry date YYYY-MM-DD"),
    exchange: str = Query("nse_fo"),
    strike_window: int = Query(5, ge=1, le=15, description="Number of strikes above and below ATM (5 -> 11 strikes total)"),
    current_atm: float | None = Query(None, description="Current ATM strike for 0.6*step hysteresis guard"),
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    step = STRIKE_STEPS.get(sym, 50)

    # Fetch 20 strikes around ATM from Kotak in 1 HTTP call, then slice strictly ATM ± strike_window (11 strikes)
    fetch_count = max(20, (strike_window * 2 + 2))
    raw_chain = oc_service.get_full_option_chain(
        underlying=sym,
        exchange=exchange,
        expiry=expiry,
        count=fetch_count,
        include_depth=False,
    )
    if raw_chain.get("status") != "OK":
        return raw_chain

    up = raw_chain.get("underlying_prices") or {}
    spot_quote = up.get("spot_quote") or {}
    spot_price = float(up.get("spot_price") or up.get("reference_price") or 0.0)
    spot_change = float(spot_quote.get("change") or 0.0)
    spot_change_pct = float(spot_quote.get("per_change") or 0.0)
    resolved_expiry = str(raw_chain.get("expiry") or expiry or "")
    lot_size = int(raw_chain.get("lot_size") or DEFAULT_LOT_SIZES.get(sym, 65))

    # Apply ATM Hysteresis Guard (0.6 * strikeStep buffer) if `current_atm` was passed
    raw_calculated_atm = int(round(spot_price / step) * step) if spot_price > 0 else 0
    if (
        current_atm is not None
        and current_atm > 0
        and spot_price > 0
        and abs(spot_price - current_atm) <= 0.6 * step
    ):
        atm_strike = int(current_atm)
    else:
        atm_strike = raw_calculated_atm

    all_strikes = raw_chain.get("strikes") or []
    if not atm_strike and all_strikes:
        atm_strike = int(raw_chain.get("analytics", {}).get("atm_strike") or all_strikes[len(all_strikes) // 2]["strike_price"])

    target_strikes = [atm_strike + (i * step) for i in range(-strike_window, strike_window + 1)]
    target_set = {float(k) for k in target_strikes}

    # Filter strictly to the 11 target strikes (or nearest 11 if step differs)
    filtered_raw = [r for r in all_strikes if float(r["strike_price"]) in target_set]
    if len(filtered_raw) < (strike_window * 2 + 1) and all_strikes:
        atm_idx = min(
            range(len(all_strikes)),
            key=lambda i: abs(float(all_strikes[i]["strike_price"]) - atm_strike),
        )
        start_i = max(0, atm_idx - strike_window)
        end_i = min(len(all_strikes), start_i + (strike_window * 2 + 1))
        filtered_raw = all_strikes[start_i:end_i]

    # Max OI in the 11-strike window for proportional OI bars
    max_window_oi = 1
    for r in filtered_raw:
        ce_oi = int((r.get("CE") or {}).get("oi_current") or 0)
        pe_oi = int((r.get("PE") or {}).get("oi_current") or 0)
        max_window_oi = max(max_window_oi, ce_oi, pe_oi)

    ltp_updates: dict[str, float] = {}
    if spot_price > 0:
        spot_tok = INDEX_SPOT_TOKENS.get(sym, {}).get("instrument_token", "Nifty 50")
        spot_seg = INDEX_SPOT_TOKENS.get(sym, {}).get("exchange_segment", "nse_cm")
        ltp_updates[f"{spot_seg}|{spot_tok}"] = spot_price

    rows: list[dict[str, Any]] = []
    for r in filtered_raw:
        k = float(r["strike_price"])
        ce = r.get("CE") or {}
        pe = r.get("PE") or {}
        ce_g = ce.get("greeks") or {}
        pe_g = pe.get("greeks") or {}

        ce_ltp = float(ce.get("ltp") or 0.0)
        pe_ltp = float(pe.get("ltp") or 0.0)
        ce_neo = str(ce.get("neo_symbol") or "")
        pe_neo = str(pe.get("neo_symbol") or "")

        ce_display = format_kite_contract_title(sym, resolved_expiry, k, "CE")
        pe_display = format_kite_contract_title(sym, resolved_expiry, k, "PE")

        if ce_neo and ce_ltp > 0:
            ltp_updates[ce_neo] = ce_ltp
            _latest_symbol_meta[ce_neo] = {
                "neo_symbol": ce_neo,
                "trading_symbol": ce.get("trading_symbol") or "",
                "display_name": ce_display,
                "underlying": sym,
                "expiry": resolved_expiry,
                "strike": k,
                "option_type": "CE",
                "lot_size": lot_size,
            }
        if pe_neo and pe_ltp > 0:
            ltp_updates[pe_neo] = pe_ltp
            _latest_symbol_meta[pe_neo] = {
                "neo_symbol": pe_neo,
                "trading_symbol": pe.get("trading_symbol") or "",
                "display_name": pe_display,
                "underlying": sym,
                "expiry": resolved_expiry,
                "strike": k,
                "option_type": "PE",
                "lot_size": lot_size,
            }

        ce_oi = int(ce.get("oi_current") or 0)
        pe_oi = int(pe.get("oi_current") or 0)

        rows.append(
            {
                "strike": int(k) if k == int(k) else k,
                "isAtm": abs(k - atm_strike) < 1e-4,
                "callOiPct": round((ce_oi / max_window_oi) * 100.0, 1),
                "putOiPct": round((pe_oi / max_window_oi) * 100.0, 1),
                "call": {
                    "instrumentToken": ce_neo,
                    "tradingSymbol": ce.get("trading_symbol") or "",
                    "displayName": ce_display,
                    "ltp": ce_ltp,
                    "change": ce.get("net_change") or 0.0,
                    "changePct": ce.get("pct_change") or 0.0,
                    "bid": round(max(0.05, ce_ltp - 0.15), 2) if ce_ltp > 0 else 0.0,
                    "ask": round(ce_ltp + 0.15, 2) if ce_ltp > 0 else 0.0,
                    "oi": ce_oi,
                    "oiLots": ce.get("oi_lots") or 0.0,
                    "oiChange": ce.get("oi_change") or 0,
                    "volume": int(ce.get("volume") or 0),
                    "iv": ce_g.get("iv"),
                    "delta": ce_g.get("delta"),
                    "gamma": ce_g.get("gamma"),
                    "vega": ce_g.get("vega"),
                    "theta": ce_g.get("theta"),
                    "buyMarginReq": round(ce_ltp * lot_size, 2),
                    "sellMarginReq": round(SHORT_MARGIN_PER_LOT.get(sym, 185_000.0) + (ce_ltp * lot_size * 0.3), 2),
                },
                "put": {
                    "instrumentToken": pe_neo,
                    "tradingSymbol": pe.get("trading_symbol") or "",
                    "displayName": pe_display,
                    "ltp": pe_ltp,
                    "change": pe.get("net_change") or 0.0,
                    "changePct": pe.get("pct_change") or 0.0,
                    "bid": round(max(0.05, pe_ltp - 0.15), 2) if pe_ltp > 0 else 0.0,
                    "ask": round(pe_ltp + 0.15, 2) if pe_ltp > 0 else 0.0,
                    "oi": pe_oi,
                    "oiLots": pe.get("oi_lots") or 0.0,
                    "oiChange": pe.get("oi_change") or 0,
                    "volume": int(pe.get("volume") or 0),
                    "iv": pe_g.get("iv"),
                    "delta": pe_g.get("delta"),
                    "gamma": pe_g.get("gamma"),
                    "vega": pe_g.get("vega"),
                    "theta": pe_g.get("theta"),
                    "buyMarginReq": round(pe_ltp * lot_size, 2),
                    "sellMarginReq": round(SHORT_MARGIN_PER_LOT.get(sym, 185_000.0) + (pe_ltp * lot_size * 0.3), 2),
                },
            }
        )

    # Also update any open positions that might be outside the 11-strike window
    for r in all_strikes:
        for side_key in ("CE", "PE"):
            leg = r.get(side_key) or {}
            ns = leg.get("neo_symbol")
            ltp_val = leg.get("ltp")
            if ns and ltp_val:
                ltp_updates[ns] = float(ltp_val)

    paper_engine.update_live_prices(ltp_updates)

    analytics = raw_chain.get("analytics") or {}
    straddle = analytics.get("atm_straddle") or {}
    pcr_obj = analytics.get("pcr") or {}
    atm_iv = float(straddle.get("atm_avg_iv") or 15.0)
    # Compute realistic IV Percentile & Regime relative to historical NIFTY VIX range (10% - 24%)
    iv_percentile = round(min(99.0, max(1.0, ((atm_iv - 10.5) / 12.0) * 100.0)), 1)
    iv_regime = "High" if iv_percentile >= 65 else ("Low" if iv_percentile <= 30 else "Normal")

    exp_res = get_option_expiries(symbol=sym, exchange=exchange)
    available_expiries = exp_res.get("expiries", []) if exp_res.get("status") == "OK" else []

    return {
        "status": "OK",
        "symbol": sym,
        "exchange": exchange,
        "spotPrice": spot_price,
        "spotChange": spot_change,
        "spotChangePct": spot_change_pct,
        "futuresPrice": up.get("futures_price"),
        "referencePrice": up.get("reference_price"),
        "lotSize": lot_size,
        "selectedExpiry": resolved_expiry,
        "selectedExpiryFormatted": format_expiry_option(resolved_expiry),
        "availableExpiries": available_expiries,
        "atmStrike": atm_strike,
        "strikes": rows,
        "summary": {
            "pcr": round(float(pcr_obj.get("oi_pcr") or 0.0), 2),
            "maxPain": analytics.get("max_pain_strike") or atm_strike,
            "atmIv": round(atm_iv, 2),
            "atmStraddle": straddle.get("combined_premium"),
            "ivPercentile": iv_percentile,
            "ivRegime": iv_regime,
        },
        "paperAccount": paper_engine.get_summary(),
        "fetchedAtIst": datetime.now(IST).strftime("%H:%M:%S"),
    }


def _compute_current_bucket_time_sec(now_ist: datetime, interval: str) -> tuple[int | None, int]:
    """
    Compute the current intraday candle bucket start timestamp (shifted by +05:30 for Lightweight Charts)
    and the remaining seconds in the current bar during NSE market hours (09:15 - 15:30 IST).
    """
    interval_sec = INTERVAL_SECONDS.get(interval, 300)
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

    if now_ist.weekday() < 5 and market_open <= now_ist <= market_close:
        if interval == "D":
            bucket_dt = market_open
            rem_sec = max(0, int((market_close - now_ist).total_seconds()))
        else:
            elapsed = max(0, int((now_ist - market_open).total_seconds()))
            bucket_idx = elapsed // interval_sec
            bucket_dt = market_open + timedelta(seconds=bucket_idx * interval_sec)
            next_bucket_dt = bucket_dt + timedelta(seconds=interval_sec)
            rem_sec = max(0, int((next_bucket_dt - now_ist).total_seconds()))
        return int(bucket_dt.timestamp()) + IST_OFFSET_SECONDS, rem_sec

    return None, 0


def _update_or_roll_forming_candle(
    neosymbol: str,
    interval: str,
    live_ltp: float,
    now_ist: datetime | None = None,
    seed_close: float | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """
    Update or spawn the live forming candle for `(neosymbol, interval)` in `_forming_candles`.
    Persists intra-bar `open`, `high`, `low`, `close` across every tick and automatically rolls
    into a new candle when the clock crosses into the next interval bucket.
    """
    if not live_ltp or live_ltp <= 0:
        return None, 0
    now_ist = (now_ist or datetime.now(IST)).astimezone(IST)
    cache_key = f"{neosymbol}:{interval}"
    bucket_time, rem_sec = _compute_current_bucket_time_sec(now_ist, interval)
    fc = _forming_candles.get(cache_key)

    if bucket_time is not None:
        if fc is None or int(fc["time"]) < bucket_time:
            if fc is not None and int(fc["time"]) < bucket_time:
                _prev_forming_candles[cache_key] = dict(fc)
            open_px = float(fc["close"]) if fc else (float(seed_close) if seed_close else float(live_ltp))
            fc = {
                "time": bucket_time,
                "open": round(open_px, 2),
                "high": round(max(open_px, live_ltp), 2),
                "low": round(min(open_px, live_ltp), 2),
                "close": round(live_ltp, 2),
                "volume": 0.0,
            }
            _forming_candles[cache_key] = fc
        else:
            fc["close"] = round(live_ltp, 2)
            fc["high"] = round(max(float(fc["high"]), live_ltp), 2)
            fc["low"] = round(min(float(fc["low"]), live_ltp), 2)
        return dict(fc), rem_sec

    # Outside market hours: update the last known forming bar if present
    if fc is not None:
        fc["close"] = round(live_ltp, 2)
        fc["high"] = round(max(float(fc["high"]), live_ltp), 2)
        fc["low"] = round(min(float(fc["low"]), live_ltp), 2)
        return dict(fc), 0

    return None, 0


@app.get("/api/v1/charts/candles")
def get_chart_candles(
    neosymbol: str = Query(..., description="e.g. 'nse_cm|Nifty 50' or 'nse_fo|40009'"),
    interval: str = Query("5min", description="1min, 3min, 5min, 15min, 30min, 60min, D"),
    days: int = Query(3, ge=1, le=30),
) -> dict[str, Any]:
    """
    Fetch historical candles from Kotak Neo's `client.historical_data()`, compute 20 SMA & 50 EMA,
    and merge/append the real-time live forming candle.
    """
    now_ist = datetime.now(IST)
    cache_key = f"{neosymbol}:{interval}"
    now_ts = time.time()

    # Use cached historical bars for 45 seconds and merge the live forming tick on top
    raw_candles: list[list[Any]] = []
    if cache_key in _candle_cache and (now_ts - _candle_cache[cache_key][0]) < 45.0:
        raw_candles = [list(c) for c in _candle_cache[cache_key][1].get("raw_candles", [])]
    else:
        try:
            with _historical_lock:
                global _last_historical_call_ts
                # Re-check cache after acquiring lock
                now_ts = time.time()
                if cache_key in _candle_cache and (now_ts - _candle_cache[cache_key][0]) < 45.0:
                    raw_candles = [list(c) for c in _candle_cache[cache_key][1].get("raw_candles", [])]
                else:
                    elapsed = time.time() - _last_historical_call_ts
                    if elapsed < 0.30:
                        time.sleep(0.30 - elapsed)
                    client, _, _ = auth_manager.require_authenticated_client()
                    to_date = now_ist.date().isoformat()
                    from_date = (now_ist.date() - timedelta(days=days)).isoformat()
                    resp = client.historical_data(
                        neosymbol=neosymbol,
                        interval=interval,
                        from_date=from_date,
                        to_date=to_date,
                    )
                    _last_historical_call_ts = time.time()
                    data = (resp.get("data") if isinstance(resp, dict) else None) or {}
                    fetched = data.get("candles") or []
                    if fetched:
                        raw_candles = fetched
                        _candle_cache[cache_key] = (time.time(), {"raw_candles": raw_candles})
                    elif cache_key in _candle_cache:
                        # Fallback to cached candles if gateway temporarily throttled
                        raw_candles = [list(c) for c in _candle_cache[cache_key][1].get("raw_candles", [])]
        except AuthStateError as exc:
            return exc.payload
        except Exception as exc:
            if cache_key in _candle_cache:
                raw_candles = [list(c) for c in _candle_cache[cache_key][1].get("raw_candles", [])]
            else:
                return {"status": "ERROR", "message": str(exc), "candles": []}

    candles: list[dict[str, Any]] = []
    closes: list[float] = []

    for row in raw_candles:
        if len(row) < 5:
            continue
        ts_str, o, h, l, c = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4])
        vol = float(row[5]) if len(row) > 5 and row[5] is not None else 0.0
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S%z")
            unix_sec = int(dt.timestamp()) + IST_OFFSET_SECONDS
        except ValueError:
            continue
        candles.append(
            {
                "time": unix_sec,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": vol,
            }
        )
        closes.append(c)

    # Merge or append the real-time live forming candle
    live_ltp = _latest_ltp_map.get(neosymbol)
    if (not live_ltp or live_ltp <= 0) and candles:
        live_ltp = candles[-1]["close"]

    forming_bar, rem_sec = (
        _update_or_roll_forming_candle(
            neosymbol=neosymbol,
            interval=interval,
            live_ltp=float(live_ltp),
            now_ist=now_ist,
            seed_close=candles[-1]["close"] if candles else None,
        )
        if (live_ltp and candles)
        else (None, 0)
    )

    if forming_bar and candles:
        last_c = candles[-1]
        prev_fc = _prev_forming_candles.get(cache_key)
        if prev_fc and last_c["time"] < prev_fc["time"] < forming_bar["time"]:
            candles.append(dict(prev_fc))
            closes.append(prev_fc["close"])
            last_c = candles[-1]

        if forming_bar["time"] > last_c["time"]:
            candles.append(forming_bar)
            closes.append(forming_bar["close"])
        else:
            last_c["close"] = forming_bar["close"]
            last_c["high"] = max(last_c["high"], forming_bar["high"])
            last_c["low"] = min(last_c["low"], forming_bar["low"])
            closes[-1] = last_c["close"]
            _forming_candles[cache_key] = dict(last_c)

    ma20_vals = _compute_sma(closes, 20)
    ema50_vals = _compute_ema(closes, 50)

    ma20_series = [
        {"time": candles[i]["time"], "value": ma20_vals[i]}
        for i in range(len(candles))
        if ma20_vals[i] is not None
    ]
    ema50_series = [
        {"time": candles[i]["time"], "value": ema50_vals[i]}
        for i in range(len(candles))
        if ema50_vals[i] is not None
    ]

    latest_candle = candles[-1] if candles else None
    prev_candle = candles[-2] if len(candles) >= 2 else latest_candle
    change = (
        round(latest_candle["close"] - prev_candle["close"], 2)
        if (latest_candle and prev_candle)
        else 0.0
    )
    change_pct = (
        round((change / prev_candle["close"]) * 100.0, 2)
        if (prev_candle and prev_candle["close"] > 0)
        else 0.0
    )

    return {
        "status": "OK",
        "neosymbol": neosymbol,
        "interval": interval,
        "candles": candles,
        "ma20": ma20_series,
        "ema50": ema50_series,
        "candleRemainingSec": rem_sec,
        "latest": {
            "candle": latest_candle,
            "change": change,
            "changePct": change_pct,
            "ma20": ma20_series[-1]["value"] if ma20_series else None,
            "ema50": ema50_series[-1]["value"] if ema50_series else None,
        },
    }


@app.get("/api/v1/charts/live-tick")
def get_live_chart_tick(
    symbol: str = Query("NIFTY"),
    spot_neosymbol: str = Query("nse_cm|Nifty 50"),
    call_neosymbol: str = Query(""),
    put_neosymbol: str = Query(""),
    interval: str = Query("5min"),
) -> dict[str, Any]:
    """
    Ultra-fast 1-call quote fetcher for the 3 active chart instruments (Spot, Call, Put)
    + any open paper position tokens. Updates the live forming candles, bid/ask spreads,
    and paper MTM every 2 seconds without hitting historical_data rate limits.
    """
    now_ist = datetime.now(IST)
    requested_neos = [ns for ns in [spot_neosymbol, call_neosymbol, put_neosymbol] if ns and "|" in ns]
    for pos_sym in paper_engine.positions.keys():
        if pos_sym and "|" in pos_sym and pos_sym not in requested_neos:
            requested_neos.append(pos_sym)

    tokens_payload: list[dict[str, str]] = []
    for ns in requested_neos:
        seg, tok = ns.split("|", 1)
        tokens_payload.append({"exchange_segment": seg, "instrument_token": tok})

    quotes_by_neo: dict[str, dict[str, Any]] = {}
    ltp_updates: dict[str, float] = {}

    if tokens_payload:
        try:
            client, _, _ = auth_manager.require_authenticated_client()
            resp = client.quotes(instrument_tokens=tokens_payload, quote_type="all")
            if isinstance(resp, list):
                for q in resp:
                    if not isinstance(q, dict):
                        continue
                    exch = str(q.get("exchange") or "")
                    etok = str(q.get("exchange_token") or "")
                    key = f"{exch}|{etok}"
                    try:
                        ltp_val = float(q.get("ltp") or 0.0)
                    except (ValueError, TypeError):
                        ltp_val = 0.0
                    if ltp_val <= 0:
                        continue

                    depth = q.get("depth") or {}
                    buys = depth.get("buy") or []
                    sells = depth.get("sell") or []
                    try:
                        best_bid = float(buys[0].get("price")) if buys and float(buys[0].get("price") or 0) > 0 else round(max(0.05, ltp_val - 0.15), 2)
                    except (ValueError, TypeError):
                        best_bid = round(max(0.05, ltp_val - 0.15), 2)
                    try:
                        best_ask = float(sells[0].get("price")) if sells and float(sells[0].get("price") or 0) > 0 else round(ltp_val + 0.15, 2)
                    except (ValueError, TypeError):
                        best_ask = round(ltp_val + 0.15, 2)

                    try:
                        chg_val = float(q.get("change") or 0.0)
                    except (ValueError, TypeError):
                        chg_val = 0.0
                    try:
                        chg_pct_val = float(q.get("per_change") or 0.0)
                    except (ValueError, TypeError):
                        chg_pct_val = 0.0

                    parsed_q = {
                        "ltp": ltp_val,
                        "bid": best_bid,
                        "ask": best_ask,
                        "spread": round(max(0.0, best_ask - best_bid), 2),
                        "change": chg_val,
                        "changePct": chg_pct_val,
                    }
                    quotes_by_neo[key] = parsed_q
                    ltp_updates[key] = ltp_val
                    # Also map cash index quote directly to `spot_neosymbol`
                    if exch in ("nse_cm", "bse_cm") and spot_neosymbol:
                        quotes_by_neo[spot_neosymbol] = parsed_q
                        ltp_updates[spot_neosymbol] = ltp_val
        except Exception:
            pass

    if ltp_updates:
        paper_engine.update_live_prices(ltp_updates)

    _, rem_sec = _compute_current_bucket_time_sec(now_ist, interval)
    panes_out: dict[str, Any] = {}

    for pane_key, ns in [("spot", spot_neosymbol), ("call", call_neosymbol), ("put", put_neosymbol)]:
        if not ns:
            continue
        q_info = quotes_by_neo.get(ns)
        ltp_val = q_info["ltp"] if q_info else _latest_ltp_map.get(ns)
        if not ltp_val or ltp_val <= 0:
            continue
        forming_bar, _ = _update_or_roll_forming_candle(
            neosymbol=ns,
            interval=interval,
            live_ltp=float(ltp_val),
            now_ist=now_ist,
        )
        panes_out[pane_key] = {
            "neosymbol": ns,
            "ltp": float(ltp_val),
            "bid": q_info["bid"] if q_info else round(max(0.05, float(ltp_val) - 0.15), 2),
            "ask": q_info["ask"] if q_info else round(float(ltp_val) + 0.15, 2),
            "spread": q_info["spread"] if q_info else 0.30,
            "change": q_info["change"] if q_info else 0.0,
            "changePct": q_info["changePct"] if q_info else 0.0,
            "formingCandle": forming_bar,
        }

    return {
        "status": "OK",
        "interval": interval,
        "candleRemainingSec": rem_sec,
        "panes": panes_out,
        "paperAccount": paper_engine.get_summary(),
        "serverTimeIst": now_ist.strftime("%H:%M:%S"),
    }


@app.get("/api/v1/paper/state")
def get_paper_state() -> dict[str, Any]:
    return {"status": "OK", "account": paper_engine.get_summary()}


@app.post("/api/v1/paper/order")
def place_paper_order(req: OrderRequest) -> dict[str, Any]:
    display_name = req.display_name or format_kite_contract_title(
        req.underlying, req.expiry, req.strike, req.option_type
    )
    return paper_engine.execute_order(
        neo_symbol=req.neo_symbol,
        trading_symbol=req.trading_symbol or display_name,
        display_name=display_name,
        underlying=req.underlying,
        expiry=req.expiry,
        strike=req.strike,
        option_type=req.option_type,
        side=req.side,
        quantity=req.quantity,
        price=req.price,
        lot_size=req.lot_size,
    )


@app.post("/api/v1/paper/close")
def close_paper_position(req: ClosePositionRequest) -> dict[str, Any]:
    if req.close_all:
        return paper_engine.close_all_positions()
    if not req.neo_symbol:
        raise HTTPException(status_code=400, detail="Provide neo_symbol or set close_all=true")
    return paper_engine.close_position(req.neo_symbol, exit_price=req.exit_price)


@app.post("/api/v1/paper/reset")
def reset_paper_account(req: ResetAccountRequest) -> dict[str, Any]:
    return {"status": "OK", "account": paper_engine.reset(req.starting_capital)}


# Serve Frontend Static Assets
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8765, reload=False)
