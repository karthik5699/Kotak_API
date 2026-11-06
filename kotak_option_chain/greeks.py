"""
Implied Volatility (IV) and Option Greeks Calculator for Kotak Neo Option Chain.

Supports:
1. Black-76 (Futures/Forward pricing model - standard for NSE/BSE index & stock options
   when the matching-expiry futures price F is known, as it naturally embeds implied
   dividends and cost of carry).
2. Black-Scholes-Merton (Spot pricing model with risk-free rate `r` and dividend yield `q`).

Computes:
- Implied Volatility (IV, annualized % e.g. 14.52)
- Delta (Δ): ∂V/∂S  (Call ∈ [0, 1], Put ∈ [-1, 0])
- Gamma (Γ): ∂²V/∂S² (Delta change per 1-point move in underlying)
- Vega  (ν): ∂V/∂σ per 1% (+0.01) change in IV
- Theta (Θ): ∂V/∂t per 1 calendar day (time decay in ₹/day)
- Rho   (ρ): ∂V/∂r per 1% (+0.01) change in interest rate
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from scipy.optimize import brentq
from scipy.stats import norm

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# NSE / BSE F&O market close time (15:30:00 IST)
MARKET_CLOSE_TIME_IST = time(15, 30, 0)

# Default India risk-free rate (approx 91-day RBI T-Bill yield ~ 6.70%)
DEFAULT_RISK_FREE_RATE = 0.067

SECONDS_PER_YEAR = 365.0 * 86400.0


@dataclass
class OptionGreeks:
    iv: float | None = None              # Annualized Implied Volatility in % (e.g., 14.85)
    delta: float | None = None           # Option Delta
    gamma: float | None = None           # Option Gamma
    vega: float | None = None            # Option Vega (per 1% IV change)
    theta: float | None = None           # Option Theta (per 1 calendar day)
    rho: float | None = None             # Option Rho (per 1% interest rate change)
    intrinsic_value: float = 0.0         # Intrinsic value in ₹
    time_value: float = 0.0              # Extrinsic / time value in ₹
    model: str = "black76"               # "black76" or "bsm"
    iv_status: str = "ok"                # "ok", "below_intrinsic", "no_ltp", "out_of_bounds"

    def to_dict(self) -> dict[str, float | str | None]:
        return asdict(self)


def compute_time_to_expiry_years(
    expiry: str | date | datetime,
    now: datetime | None = None,
) -> float:
    """
    Calculate exact time to expiry (T) in years, anchored to 15:30:00 IST on the expiry date.
    Floors at 60 seconds (1 minute) so expiry-afternoon options never divide by zero.
    """
    now_ist = (now or datetime.now(IST)).astimezone(IST)

    if isinstance(expiry, datetime):
        expiry_dt_ist = expiry.astimezone(IST)
    elif isinstance(expiry, date):
        expiry_dt_ist = datetime.combine(expiry, MARKET_CLOSE_TIME_IST, tzinfo=IST)
    else:
        exp_str = str(expiry).strip()
        parsed_date: date | None = None
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y", "%Y/%m/%d"):
            try:
                parsed_date = datetime.strptime(exp_str.upper(), fmt).date()
                break
            except ValueError:
                continue
        if parsed_date is None:
            raise ValueError(f"Unsupported expiry date format: {expiry!r}")
        expiry_dt_ist = datetime.combine(parsed_date, MARKET_CLOSE_TIME_IST, tzinfo=IST)

    remaining_seconds = (expiry_dt_ist - now_ist).total_seconds()
    # Floor at 60 seconds so on expiry day up to 15:30 IST (or post-market inspection) T > 0
    safe_seconds = max(remaining_seconds, 60.0)
    return safe_seconds / SECONDS_PER_YEAR


def bsm_option_price(
    underlying_price: float,
    strike_price: float,
    time_to_expiry: float,
    sigma: float,
    option_type: Literal["CE", "PE"],
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yield: float = 0.0,
    is_futures_model: bool = False,
) -> float:
    """
    Compute theoretical European Call ('CE') or Put ('PE') price using either:
    - Black-76 (`is_futures_model=True`, where `underlying_price` is Futures price F, so q = r)
    - Black-Scholes-Merton (`is_futures_model=False`, where `underlying_price` is Spot price S)
    """
    S = float(underlying_price)
    K = float(strike_price)
    T = max(float(time_to_expiry), 1e-8)
    r = float(risk_free_rate)
    q = r if is_futures_model else float(dividend_yield)

    if sigma <= 0.0:
        if option_type.upper() == "CE":
            return max(0.0, S * math.exp(-q * T) - K * math.exp(-r * T))
        return max(0.0, K * math.exp(-r * T) - S * math.exp(-q * T))

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    df_r = math.exp(-r * T)
    df_q = math.exp(-q * T)

    if option_type.upper() == "CE":
        return S * df_q * float(norm.cdf(d1)) - K * df_r * float(norm.cdf(d2))
    else:
        return K * df_r * float(norm.cdf(-d2)) - S * df_q * float(norm.cdf(-d1))


def solve_implied_volatility(
    market_price: float,
    underlying_price: float,
    strike_price: float,
    time_to_expiry: float,
    option_type: Literal["CE", "PE"],
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yield: float = 0.0,
    is_futures_model: bool = False,
    min_sigma: float = 1e-4,
    max_sigma: float = 5.0,
) -> tuple[float | None, str]:
    """
    Solve for annualized Implied Volatility (sigma as a decimal, e.g., 0.15 for 15%)
    given an observed market option price using Brent's root-finding algorithm.

    Returns:
        (sigma_decimal_or_None, status_str)
    """
    if market_price is None or market_price <= 0 or underlying_price <= 0 or strike_price <= 0:
        return None, "no_ltp"

    opt = option_type.upper()
    if opt not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")

    T = max(float(time_to_expiry), 1e-8)
    r = float(risk_free_rate)
    q = r if is_futures_model else float(dividend_yield)

    # Discounted lower bound (no-arbitrage theoretical floor)
    if opt == "CE":
        discounted_intrinsic = max(0.0, underlying_price * math.exp(-q * T) - strike_price * math.exp(-r * T))
    else:
        discounted_intrinsic = max(0.0, strike_price * math.exp(-r * T) - underlying_price * math.exp(-q * T))

    if market_price <= discounted_intrinsic + 1e-6:
        return None, "below_intrinsic"

    def objective(sigma: float) -> float:
        return (
            bsm_option_price(
                underlying_price=underlying_price,
                strike_price=strike_price,
                time_to_expiry=T,
                sigma=sigma,
                option_type=opt,  # type: ignore[arg-type]
                risk_free_rate=r,
                dividend_yield=q,
                is_futures_model=is_futures_model,
            )
            - market_price
        )

    low_val = objective(min_sigma)
    high_val = objective(max_sigma)

    if low_val > 0:
        return None, "below_intrinsic"
    if high_val < 0:
        return None, "out_of_bounds"

    try:
        sigma = float(brentq(objective, min_sigma, max_sigma, xtol=1e-6, maxiter=100))
        return sigma, "ok"
    except (ValueError, RuntimeError):
        return None, "out_of_bounds"


def calculate_option_greeks(
    market_price: float | None,
    underlying_price: float,
    strike_price: float,
    time_to_expiry: float,
    option_type: Literal["CE", "PE"],
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yield: float = 0.0,
    is_futures_model: bool = True,
) -> OptionGreeks:
    """
    Calculate Implied Volatility (IV) and all Option Greeks (Delta, Gamma, Vega, Theta, Rho)
    for a single Call ('CE') or Put ('PE') strike.
    """
    opt = option_type.upper()
    model_name = "black76" if is_futures_model else "bsm"

    if underlying_price <= 0 or strike_price <= 0:
        return OptionGreeks(model=model_name, iv_status="no_ltp")

    if opt == "CE":
        intrinsic = max(0.0, underlying_price - strike_price)
    else:
        intrinsic = max(0.0, strike_price - underlying_price)

    if market_price is None or market_price <= 0:
        return OptionGreeks(
            intrinsic_value=round(intrinsic, 4),
            time_value=0.0,
            model=model_name,
            iv_status="no_ltp",
        )

    time_val = max(0.0, market_price - intrinsic)
    T = max(float(time_to_expiry), 1e-8)
    r = float(risk_free_rate)
    q = r if is_futures_model else float(dividend_yield)

    sigma, status = solve_implied_volatility(
        market_price=market_price,
        underlying_price=underlying_price,
        strike_price=strike_price,
        time_to_expiry=T,
        option_type=opt,  # type: ignore[arg-type]
        risk_free_rate=r,
        dividend_yield=q,
        is_futures_model=is_futures_model,
    )

    if sigma is None:
        # Even if deep ITM option trades at a tiny discount to intrinsic value,
        # provide limiting intrinsic Delta so portfolio/chain delta remains usable.
        limiting_delta = None
        if status == "below_intrinsic":
            df_q = math.exp(-q * T)
            if opt == "CE":
                limiting_delta = round(df_q if underlying_price > strike_price else 0.0, 4)
            else:
                limiting_delta = round(-df_q if underlying_price < strike_price else 0.0, 4)
        return OptionGreeks(
            iv=0.0 if status == "below_intrinsic" else None,
            delta=limiting_delta,
            gamma=0.0 if status == "below_intrinsic" else None,
            vega=0.0 if status == "below_intrinsic" else None,
            theta=0.0 if status == "below_intrinsic" else None,
            rho=0.0 if status == "below_intrinsic" else None,
            intrinsic_value=round(intrinsic, 4),
            time_value=round(time_val, 4),
            model=model_name,
            iv_status=status,
        )

    S = float(underlying_price)
    K = float(strike_price)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    df_r = math.exp(-r * T)
    df_q = math.exp(-q * T)
    pdf_d1 = float(norm.pdf(d1))
    cdf_d1 = float(norm.cdf(d1))
    cdf_d2 = float(norm.cdf(d2))
    cdf_neg_d1 = float(norm.cdf(-d1))
    cdf_neg_d2 = float(norm.cdf(-d2))

    # Gamma and Vega are identical for Calls and Puts at the same (S, K, T, sigma)
    gamma = (df_q * pdf_d1) / (S * sigma * sqrt_T)
    vega = (S * df_q * pdf_d1 * sqrt_T) / 100.0  # per 1% (+0.01) change in IV

    if opt == "CE":
        delta = df_q * cdf_d1
        theta_annual = (
            -(S * df_q * pdf_d1 * sigma) / (2.0 * sqrt_T)
            - r * K * df_r * cdf_d2
            + q * S * df_q * cdf_d1
        )
        if is_futures_model:
            rho = -T * df_r * (S * cdf_d1 - K * cdf_d2) / 100.0
        else:
            rho = (K * T * df_r * cdf_d2) / 100.0
    else:
        delta = -df_q * cdf_neg_d1
        theta_annual = (
            -(S * df_q * pdf_d1 * sigma) / (2.0 * sqrt_T)
            + r * K * df_r * cdf_neg_d2
            - q * S * df_q * cdf_neg_d1
        )
        if is_futures_model:
            rho = -T * df_r * (K * cdf_neg_d2 - S * cdf_neg_d1) / 100.0
        else:
            rho = -(K * T * df_r * cdf_neg_d2) / 100.0

    theta_daily = theta_annual / 365.0

    return OptionGreeks(
        iv=round(sigma * 100.0, 2),
        delta=round(delta, 4),
        gamma=round(gamma, 6),
        vega=round(vega, 4),
        theta=round(theta_daily, 4),
        rho=round(rho, 4),
        intrinsic_value=round(intrinsic, 4),
        time_value=round(time_val, 4),
        model=model_name,
        iv_status="ok",
    )
