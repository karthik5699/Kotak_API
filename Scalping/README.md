# Kite Scalper Terminal — Options Scalping Simulator (`Scalping/`)

A high-density, single-viewport (`100vw × 100dvh`) dark-mode **Options Scalping Simulator** modeled after the **Zerodha Kite "Scalper" Terminal**, powered by live Kotak Neo API option chain & intraday candlestick data and a built-in Paper Trading Engine with TradingView-style right-axis live P&L overlays.

---

## Quick Start

From `/Users/karthiksingh/Desktop/Projects/Kotak_API`:

```bash
python3 Scalping/server.py
```

Then open **[http://127.0.0.1:8765](http://127.0.0.1:8765)** in your browser.

> **Prerequisite**: Ensure today's Kotak Neo TOTP session is active (`python3 cli.py status` or `python3 cli.py totp <6_DIGIT_TOTP>`).

---

## Key Features

1. **Dynamic Multi-Expiry Fetching (`🔗` Linked or Independent)**:
   - Switch expiries independently in the **Option Chain Panel** and **Scalper Order Window**, or lock them together via the `🔗` chain-link toggle.
   - Displays relative time labels (`9 Nov (3 days)`, `17 Nov (11 days)`, `23 Nov (2 weeks)`, `1 Dec (3 weeks)`, etc.).

2. **Restricted ATM ± 5 Strike Window (11 Strikes Total) with Hysteresis**:
   - Dynamically calculates the ATM strike from the live spot price and renders strictly **5 strikes below ATM, 1 ATM strike, and 5 strikes above ATM** (`11` rows total).
   - Includes a `0.6 × strikeStep` hysteresis guard so the strike ladder does not flicker when Spot hovers near a midpoint.

3. **3-Pane Synchronized Candlestick Chart Grid (TradingView Lightweight Charts)**:
   - **Left Pane (50% W × 100% H)**: `NIFTY 50` (or selected index) Spot Candlestick chart with **MA 20** (blue) and **EMA 50** (purple) overlays.
   - **Top-Right Pane (50% W × 50% H)**: Active **Call (CE)** Option Candlestick + Volume Histogram + floating 1-click `Buy CE` / `Sell CE` buttons.
   - **Bottom-Right Pane (50% W × 50% H)**: Active **Put (PE)** Option Candlestick + Volume Histogram + floating 1-click `Buy PE` / `Sell PE` buttons.

4. **TradingView Right-Side Live P&L Overlays & Paper Money Wallet**:
   - Starts with **₹10,00,000** virtual paper capital (configurable/resettable anytime).
   - Whenever you have an open paper position on the active Call or Put contract, the chart renders:
     - A dashed **Entry Price Line** across the candlestick canvas.
     - An interactive **TradingView Right-Axis Position & Live P&L Pill** (`[ +65 | @ ₹162.85 | +₹1,462.50 (+13.8%) | ⇅ | × ]`) dynamically tracked to the entry price Y-coordinate on the right-hand side of the chart.
     - **1-Click Reverse (`⇅`)** and **1-Click Close (`×`)** directly inside the candlestick chart overlay.
   - A **Portfolio Live MTM Badge** on the right side of the Spot chart displays aggregate Unrealized P&L, Realized P&L, and Net MTM across all open legs.

5. **Configurable Auto-Refresh Polling (`5s` / `7s` / `10s` / `15s` / `Paused`)**:
   - Default `7s` polling updates the 11-strike Option Chain, Black-76 IV & Greeks ($\Delta, \Gamma, \mathcal{V}, \Theta$), Paper MTM P&L, and the live forming candlestick bars without throttling the Kotak Neo API.
