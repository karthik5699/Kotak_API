"""
Formatters for WhatsApp LLM Agent messages and Rich terminal tables.
"""

from __future__ import annotations

from typing import Any


def _fmt_num(val: float | int | None, decimals: int = 2, default: str = "-") -> str:
    if val is None:
        return default
    return f"{val:,.{decimals}f}"


def _fmt_oi_lakhs(oi: int | None) -> str:
    if not oi:
        return "0"
    if abs(oi) >= 100_000:
        return f"{oi / 100_000:.2f}L"
    if abs(oi) >= 1_000:
        return f"{oi / 1_000:.1f}K"
    return str(oi)


def format_whatsapp_option_chain_summary(
    chain_data: dict[str, Any],
    max_strikes_around_atm: int = 9,
) -> str:
    """
    Format a complete Option Chain + Greeks response into a concise, mobile-friendly
    WhatsApp message (using WhatsApp bold/monospace conventions).
    """
    status = chain_data.get("status")
    if status != "OK":
        msg = chain_data.get("message") or "Action required."
        instr = chain_data.get("agent_instruction") or ""
        return f"⚠️ *{status}*\n{msg}\n\n{instr}".strip()

    symbol = chain_data.get("underlying", "")
    expiry = chain_data.get("expiry", "")
    dte = chain_data.get("days_to_expiry", "")
    lot_size = chain_data.get("lot_size", "")
    up = chain_data.get("underlying_prices") or {}
    ref_price = up.get("reference_price")
    ref_src = up.get("reference_source")
    spot_price = up.get("spot_price")
    fut_price = up.get("futures_price")

    analytics = chain_data.get("analytics") or {}
    atm_strike = analytics.get("atm_strike")
    straddle = analytics.get("atm_straddle") or {}
    pcr = analytics.get("pcr") or {}
    max_pain = analytics.get("max_pain_strike")
    walls = analytics.get("oi_walls") or {}

    lines: list[str] = [
        f"📊 *{symbol} Option Chain* ({expiry} | {dte}d | Lot: {lot_size})",
    ]

    price_parts = []
    if spot_price:
        price_parts.append(f"Spot: *{_fmt_num(spot_price)}*")
    if fut_price:
        price_parts.append(f"Fut: *{_fmt_num(fut_price)}*")
    if not price_parts and ref_price:
        price_parts.append(f"Ref ({ref_src}): *{_fmt_num(ref_price)}*")
    if price_parts:
        lines.append(" | ".join(price_parts))

    lines.append(
        f"🎯 *ATM*: {_fmt_num(atm_strike, 0)} | "
        f"*Straddle*: ₹{_fmt_num(straddle.get('combined_premium'))} "
        f"(±{_fmt_num(straddle.get('implied_move_pct'))}%) | "
        f"*ATM IV*: {_fmt_num(straddle.get('atm_avg_iv'))}%"
    )
    lines.append(
        f"⚖️ *OI PCR*: {_fmt_num(pcr.get('oi_pcr'), 2)} ({pcr.get('sentiment', '-')}) | "
        f"*Max Pain*: {_fmt_num(max_pain, 0)}"
    )

    res_list = walls.get("call_resistance") or []
    sup_list = walls.get("put_support") or []
    if res_list:
        res_str = ", ".join(f"{int(r['strike'])} ({_fmt_oi_lakhs(r['oi'])})" for r in res_list[:2])
        lines.append(f"🧱 *Call Resistance (OI)*: {res_str}")
    if sup_list:
        sup_str = ", ".join(f"{int(s['strike'])} ({_fmt_oi_lakhs(s['oi'])})" for s in sup_list[:2])
        lines.append(f"🛡️ *Put Support (OI)*: {sup_str}")

    strikes = chain_data.get("strikes") or []
    if strikes and atm_strike is not None:
        atm_idx = min(
            range(len(strikes)),
            key=lambda i: abs(strikes[i]["strike_price"] - atm_strike),
        )
        half = max(1, max_strikes_around_atm // 2)
        start_idx = max(0, atm_idx - half)
        end_idx = min(len(strikes), start_idx + max_strikes_around_atm)
        window = strikes[start_idx:end_idx]

        lines.append("")
        lines.append("```")
        lines.append("STRIKE | CE LTP (IV/Δ)    | PE LTP (IV/Δ)")
        lines.append("-------+------------------+------------------")
        for row in window:
            k = int(row["strike_price"]) if row["strike_price"] == int(row["strike_price"]) else row["strike_price"]
            marker = "*" if row["strike_price"] == atm_strike else " "
            ce = row.get("CE") or {}
            pe = row.get("PE") or {}
            ce_g = ce.get("greeks") or {}
            pe_g = pe.get("greeks") or {}

            ce_ltp = _fmt_num(ce.get("ltp"), 1)
            ce_iv = _fmt_num(ce_g.get("iv"), 1)
            ce_d = _fmt_num(ce_g.get("delta"), 2)

            pe_ltp = _fmt_num(pe.get("ltp"), 1)
            pe_iv = _fmt_num(pe_g.get("iv"), 1)
            pe_d = _fmt_num(pe_g.get("delta"), 2)

            lines.append(
                f"{k:>5}{marker} | {ce_ltp:>6} ({ce_iv}%/{ce_d}) | {pe_ltp:>6} ({pe_iv}%/{pe_d})"
            )
        lines.append("```")

    return "\n".join(lines)
