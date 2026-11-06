"""
LLM Agent Tool Interface for WhatsApp Bot Integration.

Exposes:
- High-level Python functions that return JSON-serializable dicts (plus `whatsapp_summary` text)
- `TOOL_SCHEMAS`: OpenAI / Gemini / Claude compatible function-calling tool definitions
- `execute_agent_tool(tool_name, arguments)`: Unified tool dispatcher for an LLM agent loop
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from kotak_option_chain.auth import AuthManager
from kotak_option_chain.config import ConfigStore
from kotak_option_chain.formatters import format_whatsapp_option_chain_summary
from kotak_option_chain.greeks import (
    DEFAULT_RISK_FREE_RATE,
    calculate_option_greeks,
    compute_time_to_expiry_years,
)
from kotak_option_chain.option_chain import OptionChainService


class KotakWhatsAppAgentToolkit:
    """
    Stateful or stateless toolkit that a WhatsApp LLM agent can invoke to manage
    permanent credentials, daily TOTP login, and Option Chain + Greeks queries.
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.store = ConfigStore(data_dir=data_dir)
        self.auth = AuthManager(store=self.store)
        self.service = OptionChainService(auth_manager=self.auth)

    def check_kotak_auth_status(self) -> dict[str, Any]:
        """
        Check whether permanent credentials (`consumer_key`, `mobile_number`, `ucc`, `mpin`)
        are saved and whether today's TOTP session is active.
        Call this at the start of a morning conversation or before market queries.
        """
        return self.auth.get_auth_status()

    def save_kotak_permanent_credentials(
        self,
        consumer_key: str | None = None,
        mobile_number: str | None = None,
        ucc: str | None = None,
        mpin: str | None = None,
    ) -> dict[str, Any]:
        """
        Permanently save the user's static Kotak Neo credentials so they never have to
        be asked again. Only fields passed as non-empty strings are updated.
        """
        return self.auth.save_permanent_credentials(
            consumer_key=consumer_key,
            mobile_number=mobile_number,
            ucc=ucc,
            mpin=mpin,
        )

    def submit_daily_totp(self, totp: str) -> dict[str, Any]:
        """
        Authenticate for the current trading day using the 6-digit TOTP code provided by
        the user on WhatsApp. Caches the resulting session tokens until tomorrow morning (06:00 AM IST).
        """
        return self.auth.login_with_daily_totp(totp=totp)

    def get_option_expiries(
        self,
        underlying: str,
        exchange: str = "nse_fo",
    ) -> dict[str, Any]:
        """
        Get all active option expiry dates (`YYYY-MM-DD`) for an underlying (e.g. `NIFTY`,
        `BANKNIFTY`, `FINNIFTY`, `RELIANCE`).
        """
        return self.service.get_expiries(underlying=underlying, exchange=exchange)

    def get_option_chain_with_greeks(
        self,
        underlying: str,
        expiry: str | None = None,
        exchange: str = "nse_fo",
        strikes_count: int = 20,
        include_depth: bool = False,
        pricing_model: Literal["black76", "bsm"] = "black76",
    ) -> dict[str, Any]:
        """
        Fetch the full Option Chain for `underlying`, compute Implied Volatility (IV) and
        Greeks (Delta, Gamma, Vega, Theta, Rho) for every Call and Put strike, and compute
        PCR, Max Pain, OI Support/Resistance walls, and ATM Straddle pricing.
        Also includes a mobile-formatted `whatsapp_summary` string.
        """
        result = self.service.get_full_option_chain(
            underlying=underlying,
            exchange=exchange,
            expiry=expiry,
            count=strikes_count,
            pricing_model=pricing_model,
            include_depth=include_depth,
        )
        result["whatsapp_summary"] = format_whatsapp_option_chain_summary(result)
        return result

    def get_strike_greeks_detail(
        self,
        underlying: str,
        strike_price: float,
        option_type: Literal["CE", "PE"] = "CE",
        expiry: str | None = None,
        exchange: str = "nse_fo",
        include_depth: bool = True,
    ) -> dict[str, Any]:
        """
        Fetch detailed quote, Open Interest, 5-level Bid/Ask market depth, Implied Volatility (IV),
        and Greeks (Delta, Gamma, Vega, Theta, Rho) for a specific strike price (`strike_price`).
        """
        chain = self.service.get_full_option_chain(
            underlying=underlying,
            exchange=exchange,
            expiry=expiry,
            count=40,
            include_depth=False,
        )
        if chain.get("status") != "OK":
            chain["whatsapp_summary"] = format_whatsapp_option_chain_summary(chain)
            return chain

        target = float(strike_price)
        opt = option_type.upper()
        matched_row = next(
            (r for r in chain.get("strikes", []) if abs(r["strike_price"] - target) < 1e-4),
            None,
        )
        if not matched_row or not matched_row.get(opt):
            available = [r["strike_price"] for r in chain.get("strikes", [])]
            return {
                "status": "STRIKE_NOT_FOUND",
                "underlying": underlying.upper(),
                "expiry": chain.get("expiry"),
                "requested_strike": target,
                "option_type": opt,
                "available_strikes": available,
                "message": f"Strike {target} {opt} not found in the nearest 40 strikes around ATM.",
            }

        leg = matched_row[opt]
        if include_depth and leg.get("neo_symbol"):
            try:
                client, _, _ = self.auth.require_authenticated_client()
                depth_map = self.service._fetch_depth_by_neosymbol(client, [leg["neo_symbol"]])
                if leg["neo_symbol"] in depth_map:
                    leg["market_depth"] = depth_map[leg["neo_symbol"]]
            except Exception:
                pass
        return {
            "status": "OK",
            "underlying": chain.get("underlying"),
            "expiry": chain.get("expiry"),
            "days_to_expiry": chain.get("days_to_expiry"),
            "lot_size": chain.get("lot_size"),
            "underlying_prices": chain.get("underlying_prices"),
            "strike_price": target,
            "option_type": opt,
            "contract": leg,
        }

    @staticmethod
    def calculate_custom_greeks(
        market_price: float,
        underlying_price: float,
        strike_price: float,
        expiry: str,
        option_type: Literal["CE", "PE"] = "CE",
        pricing_model: Literal["black76", "bsm"] = "black76",
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        dividend_yield: float = 0.0,
    ) -> dict[str, Any]:
        """
        Pure mathematical calculator: compute IV, Delta, Gamma, Vega, Theta, and Rho
        directly from any given option LTP, underlying price, strike price, and expiry date
        without needing an API call.
        """
        T = compute_time_to_expiry_years(expiry)
        greeks = calculate_option_greeks(
            market_price=market_price,
            underlying_price=underlying_price,
            strike_price=strike_price,
            time_to_expiry=T,
            option_type=option_type,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            is_futures_model=(pricing_model == "black76"),
        )
        return {
            "status": "OK",
            "inputs": {
                "market_price": market_price,
                "underlying_price": underlying_price,
                "strike_price": strike_price,
                "expiry": expiry,
                "days_to_expiry": round(T * 365.0, 2),
                "option_type": option_type.upper(),
                "pricing_model": pricing_model,
                "risk_free_rate": risk_free_rate,
            },
            "greeks": greeks.to_dict(),
        }


# Declarative Function-Calling Schemas for LLM Agents (OpenAI / Gemini / Anthropic compatible)
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "check_kotak_auth_status",
        "description": (
            "Check if permanent Kotak Neo credentials (mobile_number, ucc, mpin, consumer_key) "
            "are stored and if today's daily TOTP session is active. If status is SETUP_REQUIRED, "
            "ask the user for permanent credentials once. If status is TOTP_REQUIRED, ask the user "
            "for today's 6-digit TOTP code."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_kotak_permanent_credentials",
        "description": (
            "Save permanent Kotak Neo credentials (`consumer_key`, `mobile_number`, `ucc`, `mpin`) "
            "to permanent storage so the user never has to provide them again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "consumer_key": {
                    "type": "string",
                    "description": "Consumer Key token from Kotak Neo App (More -> Trade API)",
                },
                "mobile_number": {
                    "type": "string",
                    "description": "Registered mobile number (e.g. +919876543210 or 10-digit number)",
                },
                "ucc": {
                    "type": "string",
                    "description": "Unique Client Code (UCC) from Kotak Neo profile",
                },
                "mpin": {
                    "type": "string",
                    "description": "6-digit Trading Account MPIN",
                },
            },
            "required": [],
        },
    },
    {
        "name": "submit_daily_totp",
        "description": (
            "Submit the user's daily 6-digit TOTP code from their Authenticator app to authenticate "
            "the Kotak Neo session for today. Sessions expire every morning at 06:00 AM IST."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "totp": {
                    "type": "string",
                    "description": "6-digit numeric TOTP code from Authenticator app (e.g. '123456')",
                }
            },
            "required": ["totp"],
        },
    },
    {
        "name": "get_option_expiries",
        "description": "Fetch all available option expiry dates (YYYY-MM-DD) for an underlying symbol.",
        "parameters": {
            "type": "object",
            "properties": {
                "underlying": {
                    "type": "string",
                    "description": "Underlying symbol, e.g. 'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'RELIANCE'",
                },
                "exchange": {
                    "type": "string",
                    "description": "Exchange segment: 'nse_fo' (default), 'bse_fo', 'mcx_fo'",
                    "default": "nse_fo",
                },
            },
            "required": ["underlying"],
        },
    },
    {
        "name": "get_option_chain_with_greeks",
        "description": (
            "Fetch all aspects of the Option Chain for an underlying (CE/PE strikes, LTP, OHLC, Volume, "
            "Open Interest, OI Change, Spot & Futures price, PCR, Max Pain, OI Support/Resistance walls, "
            "ATM Straddle) AND calculate Implied Volatility (IV), Delta, Gamma, Vega, Theta, and Rho."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "underlying": {
                    "type": "string",
                    "description": "Underlying symbol, e.g. 'NIFTY', 'BANKNIFTY', 'RELIANCE'",
                },
                "expiry": {
                    "type": "string",
                    "description": "Optional ISO expiry date (YYYY-MM-DD). Defaults to nearest active expiry.",
                },
                "exchange": {
                    "type": "string",
                    "description": "Exchange segment: 'nse_fo' (default), 'bse_fo', 'mcx_fo'",
                    "default": "nse_fo",
                },
                "strikes_count": {
                    "type": "integer",
                    "description": "Number of strikes around ATM (multiple of 10, default 20)",
                    "default": 20,
                },
                "include_depth": {
                    "type": "boolean",
                    "description": "Set true to also fetch 5-level bid/ask market depth for each strike",
                    "default": False,
                },
            },
            "required": ["underlying"],
        },
    },
    {
        "name": "get_strike_greeks_detail",
        "description": (
            "Fetch complete contract details, 5-level Bid/Ask order book depth, Open Interest, "
            "IV, Delta, Gamma, Vega, Theta, and Rho for a single specific strike price."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "underlying": {"type": "string", "description": "Underlying symbol, e.g. 'NIFTY'"},
                "strike_price": {"type": "number", "description": "Strike price, e.g. 24500"},
                "option_type": {
                    "type": "string",
                    "enum": ["CE", "PE"],
                    "description": "'CE' for Call or 'PE' for Put",
                },
                "expiry": {
                    "type": "string",
                    "description": "Optional ISO expiry date (YYYY-MM-DD). Defaults to nearest expiry.",
                },
                "exchange": {"type": "string", "default": "nse_fo"},
            },
            "required": ["underlying", "strike_price", "option_type"],
        },
    },
    {
        "name": "calculate_custom_greeks",
        "description": (
            "Calculate Implied Volatility (IV) and Option Greeks (Delta, Gamma, Vega, Theta, Rho) "
            "directly from a given option price, underlying price, strike, and expiry date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "market_price": {"type": "number", "description": "Option LTP / premium"},
                "underlying_price": {"type": "number", "description": "Underlying Spot or Futures price"},
                "strike_price": {"type": "number", "description": "Option Strike price"},
                "expiry": {"type": "string", "description": "Expiry date (YYYY-MM-DD)"},
                "option_type": {"type": "string", "enum": ["CE", "PE"], "default": "CE"},
            },
            "required": ["market_price", "underlying_price", "strike_price", "expiry"],
        },
    },
]


def execute_agent_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    toolkit: KotakWhatsAppAgentToolkit | None = None,
) -> dict[str, Any]:
    """
    Dispatch a tool call by name from an LLM agent loop.
    """
    tk = toolkit or KotakWhatsAppAgentToolkit()
    args = arguments or {}
    fn = getattr(tk, tool_name, None)
    if fn is None or not callable(fn):
        return {
            "status": "UNKNOWN_TOOL",
            "message": f"Unknown tool {tool_name!r}. Available tools: {[t['name'] for t in TOOL_SCHEMAS]}",
        }
    return fn(**args)  # type: ignore[no-any-return]
