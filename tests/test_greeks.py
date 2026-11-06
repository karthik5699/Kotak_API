"""
Unit tests for Black-Scholes-Merton & Black-76 Implied Volatility and Option Greeks Engine.
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from kotak_option_chain.greeks import (
    IST,
    bsm_option_price,
    calculate_option_greeks,
    compute_time_to_expiry_years,
    solve_implied_volatility,
)


def test_bsm_and_black76_put_call_parity() -> None:
    F = 24500.0
    K = 24400.0
    T = 10.0 / 365.0
    r = 0.067
    sigma = 0.145

    call_price = bsm_option_price(F, K, T, sigma, "CE", risk_free_rate=r, is_futures_model=True)
    put_price = bsm_option_price(F, K, T, sigma, "PE", risk_free_rate=r, is_futures_model=True)

    # Black-76 Put-Call Parity: C - P = exp(-r*T) * (F - K)
    expected_diff = math.exp(-r * T) * (F - K)
    assert math.isclose(call_price - put_price, expected_diff, rel_tol=1e-6)


def test_iv_solver_roundtrip_and_greeks() -> None:
    F = 24500.0
    K = 24500.0
    T = 7.0 / 365.0
    r = 0.067
    true_sigma = 0.1525  # 15.25% IV

    ce_ltp = bsm_option_price(F, K, T, true_sigma, "CE", risk_free_rate=r, is_futures_model=True)
    pe_ltp = bsm_option_price(F, K, T, true_sigma, "PE", risk_free_rate=r, is_futures_model=True)

    ce_greeks = calculate_option_greeks(
        market_price=ce_ltp,
        underlying_price=F,
        strike_price=K,
        time_to_expiry=T,
        option_type="CE",
        risk_free_rate=r,
        is_futures_model=True,
    )
    pe_greeks = calculate_option_greeks(
        market_price=pe_ltp,
        underlying_price=F,
        strike_price=K,
        time_to_expiry=T,
        option_type="PE",
        risk_free_rate=r,
        is_futures_model=True,
    )

    assert ce_greeks.iv_status == "ok"
    assert pe_greeks.iv_status == "ok"
    assert ce_greeks.iv == pytest.approx(15.25, abs=0.02)
    assert pe_greeks.iv == pytest.approx(15.25, abs=0.02)

    # ATM Call delta ~ +0.50, ATM Put delta ~ -0.50, and |Δ_CE| + |Δ_PE| = exp(-r*T)
    assert 0.48 <= (ce_greeks.delta or 0.0) <= 0.53
    assert -0.52 <= (pe_greeks.delta or 0.0) <= -0.47
    assert (ce_greeks.delta or 0.0) - (pe_greeks.delta or 0.0) == pytest.approx(math.exp(-r * T), abs=1e-3)

    # Gamma and Vega must be identical for CE and PE at same strike & IV
    assert ce_greeks.gamma == pytest.approx(pe_greeks.gamma, rel=1e-4)
    assert ce_greeks.vega == pytest.approx(pe_greeks.vega, rel=1e-4)
    assert (ce_greeks.gamma or 0.0) > 0
    assert (ce_greeks.vega or 0.0) > 0

    # Time decay (Theta) for ATM options is negative
    assert (ce_greeks.theta or 0.0) < 0
    assert (pe_greeks.theta or 0.0) < 0


def test_below_intrinsic_and_zero_ltp_edge_cases() -> None:
    # Deep ITM Call trading slightly below intrinsic value
    g_disc = calculate_option_greeks(
        market_price=480.0,
        underlying_price=24500.0,
        strike_price=24000.0,
        time_to_expiry=5.0 / 365.0,
        option_type="CE",
        is_futures_model=True,
    )
    assert g_disc.iv_status == "below_intrinsic"
    assert g_disc.intrinsic_value == 500.0
    assert (g_disc.delta or 0.0) > 0.99

    # Zero LTP
    g_zero = calculate_option_greeks(
        market_price=0.0,
        underlying_price=24500.0,
        strike_price=25000.0,
        time_to_expiry=5.0 / 365.0,
        option_type="CE",
    )
    assert g_zero.iv_status == "no_ltp"
    assert g_zero.iv is None


def test_time_to_expiry_intraday_calculation() -> None:
    now = datetime(2026, 11, 6, 12, 30, 0, tzinfo=IST)
    # Expiry on the same day at 15:30 IST -> exactly 3 hours = 10800 seconds
    t_same_day = compute_time_to_expiry_years("2026-11-06", now=now)
    assert t_same_day == pytest.approx(10800.0 / (365.0 * 86400.0), rel=1e-5)

    # Past expiry floors safely at 60 seconds
    t_past = compute_time_to_expiry_years("2026-11-01", now=now)
    assert t_past == pytest.approx(60.0 / (365.0 * 86400.0), rel=1e-5)
