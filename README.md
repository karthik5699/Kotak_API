# Kotak Neo Option Chain & Greeks Engine (WhatsApp LLM Agent Ready)

A complete option chain & quantitative Greeks engine built on the official [`kotak-neo-python` (`kotakneoapi` v3.0.13)](https://github.com/Kotak-Neo/kotak-neo-python) SDK.

## Key Capabilities

1. **Permanent Setup + Daily TOTP Lifecycle (`AuthManager`)**:
   - **Permanent Credentials**: `consumer_key`, `mobile_number`, `ucc`, and `mpin` are asked **once** and stored permanently in `data/credentials.json` (`chmod 600`).
   - **Daily TOTP**: Kotak Neo sessions expire every morning at **06:00 AM IST**. On the first request of each day, the toolkit returns `status: "TOTP_REQUIRED"` instructing your WhatsApp LLM agent to ask you for today's 6-digit TOTP code. Once validated via `submit_daily_totp()`, the session tokens are cached in `data/session.json` for the entire day.
2. **All Aspects of the Option Chain (`OptionChainService`)**:
   - **Expiries (`get_expiries`)**: All active weekly/monthly ISO expiry dates (`YYYY-MM-DD`).
   - **Per-Strike Call & Put Data**: `ltp`, `open`, `high`, `low`, `prev_close`, `net_change`, `pct_change`, `volume`, `oi_current`, `oi_previous`, `oi_change`, `oi_change_pct`, `oi_lots`, `moneyness` (`ITM`/`ATM`/`OTM`).
   - **Underlying Spot & Futures Discovery**: Automatically fetches both the Index/Cash Spot quote (`client.quotes`) and the Underlying Futures Chain (`client.option_chain(..., instrument_type="fut")`), with Put-Call Parity implied forward fallback.
   - **5-Level Market Depth (`include_depth=True`)**: Best bid/ask, spread, VWAP, total buy/sell quantity, circuit limits, and 5-level order book.
   - **Chain-Level Quant Analytics**:
     - **PCR**: Open Interest PCR & Volume PCR with sentiment classification
     - **Max Pain Strike**: Strike price minimizing aggregate option writer payout
     - **OI Support & Resistance Walls**: Top 3 Call OI (Resistance) & Top 3 Put OI (Support) strikes + top intraday OI additions
     - **ATM Straddle**: Combined CE+PE premium, upper/lower breakevens, implied move %, and ATM average IV
3. **Implied Volatility (IV) & Option Greeks Engine (`greeks.py`)**:
   - Supports **Black-76** (Futures-based, NSE standard) and **Black-Scholes-Merton** (Spot-based)
   - Exact intraday time-to-expiry ($T$) anchored to **15:30:00 IST** on the expiry date
   - Computes **IV (%)**, **Delta ($\Delta$)**, **Gamma ($\Gamma$)**, **Vega ($\mathcal{V}$)**, **Theta ($\Theta$)**, and **Rho ($\Rho$)** for every Call and Put strike.

---

## Quick Start (CLI)

```bash
cd /Users/karthiksingh/Desktop/Projects/Kotak_API

# 1. Check current auth status
python3 cli.py status

# 2. Save permanent credentials once (or run `python3 cli.py setup` interactively)
python3 cli.py setup \
  --consumer-key "YOUR_CONSUMER_KEY" \
  --mobile "+919876543210" \
  --ucc "YOUR_UCC" \
  --mpin "123456"

# 3. Authenticate each morning with your 6-digit TOTP
python3 cli.py totp 123456

# 4. Fetch active expiries
python3 cli.py expiries NIFTY

# 5. Fetch Option Chain + Greeks (Rich table, WhatsApp summary, or full JSON)
python3 cli.py chain NIFTY --count 20
python3 cli.py chain NIFTY --count 20 --whatsapp
python3 cli.py chain NIFTY --count 20 --depth --json

# 6. Offline Greeks calculator (no API call needed)
python3 cli.py greeks --ltp 152.0 --underlying-price 24250 --strike 24250 --expiry 2026-11-12 --type CE
```

---

## WhatsApp LLM Agent Integration (`agent_tools.py`)

Your WhatsApp agent can import `TOOL_SCHEMAS` and `execute_agent_tool` directly:

```python
from kotak_option_chain import TOOL_SCHEMAS, execute_agent_tool

# 1. Pass `TOOL_SCHEMAS` to your LLM (Gemini / OpenAI / Claude)
# 2. When the LLM invokes a tool call, dispatch it:
result = execute_agent_tool("get_option_chain_with_greeks", {"underlying": "NIFTY", "strikes_count": 20})

if result["status"] in ("SETUP_REQUIRED", "TOTP_REQUIRED"):
    # The LLM reads `result["agent_instruction"]` and asks the user on WhatsApp
    print(result["agent_instruction"])
elif result["status"] == "OK":
    # Send `result["whatsapp_summary"]` directly to WhatsApp or let the LLM analyze `result["strikes"]`
    print(result["whatsapp_summary"])
```

---

## Kite "Scalper" Options Simulator (`Scalping/`)

Inside [`Scalping/`](./Scalping), you'll find a **Zerodha Kite "Scalper" Terminal** web application powered by real-time Kotak Neo option chains, historical OHLCV, and live 2-second quote ticks:

- **3-Pane TradingView Lightweight Charts**: Spot Index + MA20/EMA50 (left), Active Call + Volume (top-right), Active Put + Volume (bottom-right) with **live forming candles**, free 2D cursor panning, and **TradingView right-side Live P&L badges** directly inside the charts.
- **ATM ± 5 Strike Option Chain**: Dynamic multi-expiry support (`🔗` linked or independent Call/Put expiries), hysteresis-protected ATM pinning, and one-click strike selection.
- **Paper Money Engine (`₹10,00,000`)**: Position-aware margin (`Req. ₹0 (Exit Long/Short)` when closing an open leg), instant 1-click reversal (`⇅`), SL/Target protections, and a **Trade Log** with a `Clear All` reset button.

```bash
python3 Scalping/server.py
# Open http://127.0.0.1:8765 in your browser
```
