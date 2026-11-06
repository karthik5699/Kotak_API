/**
 * Kite Scalper Terminal — Options Scalping Simulator Frontend
 *
 * Features:
 * - Dynamic Multi-Expiry Fetching (Independent or Linked via 🔗 between Option Chain & Order Pad)
 * - Strictly ATM ± 5 Strikes (11 strikes total) with 0.6*step Hysteresis Guard
 * - 3-Pane TradingView Lightweight Charts (Spot + MA20/EMA50, Active Call + Volume, Active Put + Volume)
 * - TradingView Best-Practice Right-Side Position & Live P&L Price-Line Overlays inside the Candlestick Charts
 * - Full Paper Money Execution Engine (Buy, Sell, Reverse ⇅, 1-Click Close ×, Flatten All)
 */

const INDEX_NEOSYMBOLS = {
  NIFTY: 'nse_cm|Nifty 50',
  BANKNIFTY: 'nse_cm|Nifty Bank',
  FINNIFTY: 'nse_cm|Nifty Fin Service',
  MIDCPNIFTY: 'nse_cm|NIFTY MID SELECT',
};

const state = {
  symbol: 'NIFTY',
  exchange: 'nse_fo',
  chainExpiry: null,
  orderExpiry: null,
  syncExpiries: true,
  displayMode: 'oi', // 'oi' | 'greeks'
  timeframe: '5min',
  daysRange: 2,
  pollIntervalMs: 7000,
  pollTimer: null,
  liveTickTimer: null,
  candleRemainingSec: 0,
  lotMultiplier: 1,
  lotSize: 65,
  atmStrike: null,
  selectedCallStrike: null,
  selectedPutStrike: null,
  chainData: null,
  orderChainData: null,
  paperAccount: null,
};

// Lightweight Charts instances & series references
const charts = {
  spot: { chart: null, candleSeries: null, ma20Series: null, ema50Series: null, loadedKey: null, lastBar: null, prevBarClose: null },
  call: { chart: null, candleSeries: null, volSeries: null, entryPriceLine: null, loadedKey: null, lastBar: null, prevBarClose: null },
  put: { chart: null, candleSeries: null, volSeries: null, entryPriceLine: null, loadedKey: null, lastBar: null, prevBarClose: null },
};

/* ==========================================================================
   FORMATTING HELPERS
   ========================================================================== */
function fmtNum(val, decimals = 2) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return '--';
  return Number(val).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtSignedMoney(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return '₹0.00';
  const n = Number(val);
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}₹${Math.abs(n).toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtCompactINR(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return '₹--';
  const n = Math.abs(Number(val));
  const sign = Number(val) < 0 ? '-' : '';
  if (n >= 100000) return `${sign}₹${(n / 100000).toFixed(2)}L`;
  if (n >= 1000) return `${sign}₹${(n / 1000).toFixed(1)}k`;
  return `${sign}₹${n.toFixed(0)}`;
}

function showToast(msg, isError = false) {
  const el = document.getElementById('order-toast');
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? '#ef5350' : '#26a69a';
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => {
    el.textContent = '';
  }, 4000);
}

/* ==========================================================================
   INITIALIZE TRADINGVIEW LIGHTWEIGHT CHARTS (3 PANES)
   ========================================================================== */
function createChartCommonOptions(container) {
  return {
    width: container.clientWidth || 400,
    height: container.clientHeight || 260,
    layout: {
      background: { type: 'solid', color: '#1b1b1b' },
      textColor: '#8c8c8c',
      fontSize: 11,
      fontFamily: "'Inter', -apple-system, sans-serif",
    },
    grid: {
      vertLines: { color: '#242424' },
      horzLines: { color: '#242424' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: '#555', width: 1, style: 2, labelBackgroundColor: '#2962ff' },
      horzLine: { color: '#555', width: 1, style: 2, labelBackgroundColor: '#2962ff' },
    },
    rightPriceScale: {
      borderColor: '#2a2a2a',
      scaleMargins: { top: 0.12, bottom: 0.18 },
      autoScale: true,
    },
    timeScale: {
      borderColor: '#2a2a2a',
      timeVisible: true,
      secondsVisible: false,
      barSpacing: 11,
      minBarSpacing: 3,
      rightOffset: 6,
    },
    handleScroll: {
      mouseWheel: true,
      pressedMouseMove: true,
      horzTouchDrag: true,
      vertTouchDrag: true,
    },
    handleScale: {
      axisPressedMouseMove: {
        time: true,
        price: true,
      },
      mouseWheel: true,
      pinch: true,
    },
  };
}

/**
 * Enable TradingView-style 2D (vertical + horizontal) cursor dragging inside a candlestick chart pane.
 *
 * In Lightweight Charts, `startScrollPrice` inside the main pane is skipped when `priceScale.isAutoScale()`
 * is true at `mousedown`. By unlocking `autoScale: false` in the capture phase of `mousedown`/`touchstart`
 * when clicking inside the plot area (while leaving the right price axis intact for zoom-drag and double-click
 * auto-reset), the user can freely drag the candlesticks down, up, left, and right with the cursor.
 */
function enableFreeVerticalPan(paneKey, containerEl) {
  let isDraggingPlot = false;

  const unlockVerticalPanIfPlotArea = (clientX, clientY) => {
    const chartInstance = charts[paneKey]?.chart;
    if (!chartInstance || !containerEl) return;
    const rect = containerEl.getBoundingClientRect();
    const relX = clientX - rect.left;
    const relY = clientY - rect.top;
    // Only unlock when clicking inside the main candle canvas (exclude right price scale & bottom time axis)
    if (relX >= 0 && relX < rect.width - 62 && relY >= 0 && relY < rect.height - 26) {
      chartInstance.priceScale('right').applyOptions({ autoScale: false });
      isDraggingPlot = true;
    }
  };

  containerEl.addEventListener(
    'mousedown',
    (e) => {
      if (e.button === 0) {
        unlockVerticalPanIfPlotArea(e.clientX, e.clientY);
      }
    },
    { capture: true }
  );

  containerEl.addEventListener(
    'touchstart',
    (e) => {
      if (e.touches && e.touches.length === 1) {
        unlockVerticalPanIfPlotArea(e.touches[0].clientX, e.touches[0].clientY);
      }
    },
    { capture: true, passive: true }
  );

  // Double-clicking inside the plot canvas or right price scale re-fits & re-enables autoScale
  containerEl.addEventListener('dblclick', () => {
    const chartInstance = charts[paneKey]?.chart;
    if (!chartInstance) return;
    chartInstance.priceScale('right').applyOptions({ autoScale: true });
    requestAnimationFrame(() => updateTvPositionOverlays());
  });

  window.addEventListener(
    'mousemove',
    () => {
      if (isDraggingPlot) {
        requestAnimationFrame(() => updateTvPositionOverlays());
      }
    },
    { passive: true }
  );

  window.addEventListener(
    'mouseup',
    () => {
      if (isDraggingPlot) {
        isDraggingPlot = false;
        requestAnimationFrame(() => updateTvPositionOverlays());
      }
    },
    { passive: true }
  );

  containerEl.addEventListener(
    'wheel',
    () => {
      requestAnimationFrame(() => updateTvPositionOverlays());
    },
    { passive: true }
  );
}

function zoomChartToRecentBars(paneKey, totalBars) {
  const target = charts[paneKey];
  if (!target || !target.chart) return;
  const count = Number(totalBars) || target.totalBars || 60;
  target.totalBars = count;
  const visibleBars = paneKey === 'spot' ? 36 : 32;

  target.chart.priceScale('right').applyOptions({ autoScale: true });
  target.chart.timeScale().applyOptions({
    barSpacing: 11,
    rightOffset: 6,
  });
  target.chart.timeScale().setVisibleLogicalRange({
    from: Math.max(0, count - visibleBars),
    to: count + 5,
  });
}

function resetAllChartScales() {
  for (const key of ['spot', 'call', 'put']) {
    const c = charts[key]?.chart;
    if (c) {
      zoomChartToRecentBars(key);
      requestAnimationFrame(() => {
        c.priceScale('right').applyOptions({ autoScale: false });
        updateTvPositionOverlays();
      });
    }
  }
}

function initCharts() {
  const spotEl = document.getElementById('chart-spot-canvas');
  const callEl = document.getElementById('chart-call-canvas');
  const putEl = document.getElementById('chart-put-canvas');

  // 1. Underlying Spot Chart + MA20 + EMA50
  charts.spot.chart = LightweightCharts.createChart(spotEl, createChartCommonOptions(spotEl));
  charts.spot.candleSeries = charts.spot.chart.addCandlestickSeries({
    upColor: '#26a69a',
    downColor: '#ef5350',
    borderUpColor: '#26a69a',
    borderDownColor: '#ef5350',
    wickUpColor: '#26a69a',
    wickDownColor: '#ef5350',
  });
  charts.spot.ma20Series = charts.spot.chart.addLineSeries({
    color: '#2196f3',
    lineWidth: 1.5,
    priceLineVisible: false,
    lastValueVisible: true,
  });
  charts.spot.ema50Series = charts.spot.chart.addLineSeries({
    color: '#ab47bc',
    lineWidth: 1.5,
    priceLineVisible: false,
    lastValueVisible: true,
  });

  // 2. Active Call Option Chart + Volume Histogram
  charts.call.chart = LightweightCharts.createChart(callEl, createChartCommonOptions(callEl));
  charts.call.candleSeries = charts.call.chart.addCandlestickSeries({
    upColor: '#26a69a',
    downColor: '#ef5350',
    borderUpColor: '#26a69a',
    borderDownColor: '#ef5350',
    wickUpColor: '#26a69a',
    wickDownColor: '#ef5350',
  });
  charts.call.volSeries = charts.call.chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: '',
  });
  charts.call.volSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.78, bottom: 0 },
  });

  // 3. Active Put Option Chart + Volume Histogram
  charts.put.chart = LightweightCharts.createChart(putEl, createChartCommonOptions(putEl));
  charts.put.candleSeries = charts.put.chart.addCandlestickSeries({
    upColor: '#26a69a',
    downColor: '#ef5350',
    borderUpColor: '#26a69a',
    borderDownColor: '#ef5350',
    wickUpColor: '#26a69a',
    wickDownColor: '#ef5350',
  });
  charts.put.volSeries = charts.put.chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: '',
  });
  charts.put.volSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.78, bottom: 0 },
  });

  // Enable free 2D vertical + horizontal cursor dragging on all 3 candlestick charts
  enableFreeVerticalPan('spot', spotEl);
  enableFreeVerticalPan('call', callEl);
  enableFreeVerticalPan('put', putEl);

  // Reposition TradingView P&L overlays whenever user pans/zooms the charts
  charts.call.chart.timeScale().subscribeVisibleTimeRangeChange(() => updateTvPositionOverlays());
  charts.put.chart.timeScale().subscribeVisibleTimeRangeChange(() => updateTvPositionOverlays());

  // Responsive ResizeObserver for all 3 chart panes
  const ro = new ResizeObserver(() => {
    for (const [key, el] of [
      ['spot', spotEl],
      ['call', callEl],
      ['put', putEl],
    ]) {
      if (charts[key].chart && el.clientWidth > 0 && el.clientHeight > 0) {
        charts[key].chart.applyOptions({
          width: el.clientWidth,
          height: el.clientHeight,
        });
      }
    }
    updateTvPositionOverlays();
  });
  ro.observe(spotEl);
  ro.observe(callEl);
  ro.observe(putEl);
}

/* ==========================================================================
   DATA FETCHING & STATE SYNC
   ========================================================================== */
function getOrderPadChain() {
  return state.syncExpiries || !state.orderChainData ? state.chainData : state.orderChainData;
}

function getRowByStrike(chainObj, strike) {
  if (!chainObj || !chainObj.strikes) return null;
  return chainObj.strikes.find((r) => Number(r.strike) === Number(strike)) || null;
}

function getActiveCallLeg() {
  const chainObj = getOrderPadChain();
  const row = getRowByStrike(chainObj, state.selectedCallStrike);
  return row ? row.call : null;
}

function getActivePutLeg() {
  const chainObj = getOrderPadChain();
  const row = getRowByStrike(chainObj, state.selectedPutStrike);
  return row ? row.put : null;
}

async function fetchOptionChain(isInitial = false) {
  try {
    const params = new URLSearchParams({
      symbol: state.symbol,
      exchange: state.exchange,
      strike_window: '5',
    });
    if (state.chainExpiry) params.set('expiry', state.chainExpiry);
    if (state.atmStrike) params.set('current_atm', String(state.atmStrike));

    const res = await fetch(`/api/v1/options/chain?${params.toString()}`);
    const data = await res.json();

    if (data.status !== 'OK') {
      showToast(data.message || 'API Auth/Totp needed', true);
      return;
    }

    state.chainData = data;
    state.atmStrike = data.atmStrike;
    state.lotSize = data.lotSize || 65;
    state.chainExpiry = data.selectedExpiry;
    if (state.syncExpiries || !state.orderExpiry) {
      state.orderExpiry = data.selectedExpiry;
      state.orderChainData = data;
    } else if (state.orderExpiry !== state.chainExpiry) {
      // Fetch separate expiry for Order Pad if unlinked
      const ordParams = new URLSearchParams({
        symbol: state.symbol,
        exchange: state.exchange,
        expiry: state.orderExpiry,
        strike_window: '5',
      });
      const ordRes = await fetch(`/api/v1/options/chain?${ordParams.toString()}`);
      const ordData = await ordRes.json();
      if (ordData.status === 'OK') {
        state.orderChainData = ordData;
      }
    }

    // Initialize default selected Call & Put strikes to ATM
    const strikesList = (getOrderPadChain()?.strikes || []).map((r) => Number(r.strike));
    if (!state.selectedCallStrike || !strikesList.includes(Number(state.selectedCallStrike))) {
      state.selectedCallStrike = data.atmStrike;
    }
    if (!state.selectedPutStrike || !strikesList.includes(Number(state.selectedPutStrike))) {
      state.selectedPutStrike = data.atmStrike;
    }

    if (data.paperAccount) {
      state.paperAccount = data.paperAccount;
    }

    renderExpiryDropdowns(data.availableExpiries || []);
    renderTopNavbar(data);
    renderOptionChainPanel(data);
    renderOrderPadPanel();
    renderPositionsAndOrdersTables();
    updateTvPositionOverlays();

    if (isInitial) {
      await refreshAllCharts(true);
    } else {
      await refreshAllCharts(false);
    }
  } catch (err) {
    console.error('Error fetching option chain:', err);
  }
}

function renderLiveCandleTimer() {
  const el = document.getElementById('live-candle-timer');
  if (!el) return;
  const rem = Math.max(0, Math.floor(Number(state.candleRemainingSec) || 0));
  const mins = String(Math.floor(rem / 60)).padStart(2, '0');
  const secs = String(rem % 60).padStart(2, '0');
  el.textContent = rem > 0 ? `● LIVE BAR ${mins}:${secs}` : '● LIVE BAR';
}

function applyLiveTickToPane(paneKey, tickData) {
  if (!tickData || !tickData.formingCandle) return;
  const target = charts[paneKey];
  if (!target || !target.candleSeries || !target.lastBar) return;

  const incoming = tickData.formingCandle;
  if (!incoming.time || incoming.time < target.lastBar.time) return;

  if (incoming.time > target.lastBar.time) {
    // New bar interval started! Roll `prevBarClose` and spawn the new live candle on the chart
    target.prevBarClose = target.lastBar.close;
    target.lastBar = {
      time: incoming.time,
      open: Number(incoming.open),
      high: Number(incoming.high),
      low: Number(incoming.low),
      close: Number(incoming.close),
      volume: Number(incoming.volume || 0),
    };
  } else {
    // Update current forming candle in-place
    const ltp = Number(incoming.close);
    target.lastBar = {
      ...target.lastBar,
      high: Math.max(Number(target.lastBar.high), Number(incoming.high), ltp),
      low: Math.min(Number(target.lastBar.low), Number(incoming.low), ltp),
      close: ltp,
    };
  }

  target.candleSeries.update(target.lastBar);

  if (paneKey !== 'spot' && target.volSeries) {
    target.volSeries.update({
      time: target.lastBar.time,
      value: target.lastBar.volume || 0,
      color: target.lastBar.close >= target.lastBar.open ? 'rgba(38, 166, 154, 0.45)' : 'rgba(239, 83, 80, 0.45)',
    });
  }

  // Update the live OHLC header readout
  const c = target.lastBar;
  const refClose = target.prevBarClose || c.open;
  const change = c.close - refClose;
  const changePct = refClose > 0 ? (change / refClose) * 100.0 : 0.0;
  const sign = change >= 0 ? '+' : '';
  const ohlcEl = document.getElementById(`${paneKey}-chart-ohlc`);
  if (ohlcEl) {
    ohlcEl.textContent = `O ${fmtNum(c.open)} H ${fmtNum(c.high)} L ${fmtNum(c.low)} C ${fmtNum(c.close)} ${sign}${fmtNum(change)} (${sign}${fmtNum(changePct)}%)`;
    ohlcEl.className = `legend-ohlc tabular ${change >= 0 ? 'bullish' : 'bearish'}`;
  }
}

async function pollLiveCandleTicks() {
  if (!state.chainData) return;
  const spotNeo = INDEX_NEOSYMBOLS[state.symbol] || 'nse_cm|Nifty 50';
  const callLeg = getActiveCallLeg();
  const putLeg = getActivePutLeg();

  try {
    const params = new URLSearchParams({
      symbol: state.symbol,
      spot_neosymbol: spotNeo,
      call_neosymbol: callLeg?.instrumentToken || '',
      put_neosymbol: putLeg?.instrumentToken || '',
      interval: state.timeframe,
    });
    const res = await fetch(`/api/v1/charts/live-tick?${params.toString()}`);
    const data = await res.json();
    if (data.status !== 'OK') return;

    if (data.candleRemainingSec !== undefined) {
      state.candleRemainingSec = Number(data.candleRemainingSec) || 0;
      renderLiveCandleTimer();
    }

    const panes = data.panes || {};
    if (panes.spot) {
      applyLiveTickToPane('spot', panes.spot);
      if (panes.spot.ltp > 0 && state.chainData) {
        state.chainData.spotPrice = panes.spot.ltp;
        if (panes.spot.change) state.chainData.spotChange = panes.spot.change;
        if (panes.spot.changePct) state.chainData.spotChangePct = panes.spot.changePct;
      }
    }
    if (panes.call && callLeg) {
      applyLiveTickToPane('call', panes.call);
      callLeg.ltp = panes.call.ltp;
      callLeg.bid = panes.call.bid;
      callLeg.ask = panes.call.ask;
      document.getElementById('call-float-ask').textContent = fmtNum(callLeg.ask);
      document.getElementById('call-float-bid').textContent = fmtNum(callLeg.bid);
      document.getElementById('call-float-spread').textContent = fmtNum(panes.call.spread, 2);
    }
    if (panes.put && putLeg) {
      applyLiveTickToPane('put', panes.put);
      putLeg.ltp = panes.put.ltp;
      putLeg.bid = panes.put.bid;
      putLeg.ask = panes.put.ask;
      document.getElementById('put-float-ask').textContent = fmtNum(putLeg.ask);
      document.getElementById('put-float-bid').textContent = fmtNum(putLeg.bid);
      document.getElementById('put-float-spread').textContent = fmtNum(panes.put.spread, 2);
    }

    if (data.paperAccount) {
      state.paperAccount = data.paperAccount;
      renderPaperAccountBadges();
      renderPositionsAndOrdersTables();
    }
    renderOrderPadPanel();
    updateTvPositionOverlays();
  } catch (_) {}
}

async function fetchCandlesForPane(paneKey, neosymbol, fitContent = false) {
  if (!neosymbol) return;
  try {
    const params = new URLSearchParams({
      neosymbol,
      interval: state.timeframe,
      days: String(state.daysRange),
    });
    const res = await fetch(`/api/v1/charts/candles?${params.toString()}`);
    const data = await res.json();
    if (data.status !== 'OK' || !Array.isArray(data.candles) || data.candles.length === 0) return;

    if (data.candleRemainingSec !== undefined) {
      state.candleRemainingSec = Number(data.candleRemainingSec) || 0;
      renderLiveCandleTimer();
    }

    const target = charts[paneKey];
    const currentKey = `${neosymbol}:${state.timeframe}:${state.daysRange}`;
    const isNewDataset = fitContent || target.loadedKey !== currentKey;

    if (isNewDataset) {
      // Initial load or contract/timeframe switch: auto-fit first, then unlock vertical drag
      target.chart.priceScale('right').applyOptions({ autoScale: true });
      target.candleSeries.setData(data.candles);
      target.loadedKey = currentKey;
    } else {
      // Background live poll: update only the latest 2 bars in-place so the user's vertical/horizontal drag position is preserved
      const startIdx = Math.max(0, data.candles.length - 2);
      for (let i = startIdx; i < data.candles.length; i++) {
        if (!target.lastBar || data.candles[i].time >= target.lastBar.time) {
          target.candleSeries.update(data.candles[i]);
        }
      }
    }

    const lastIdx = data.candles.length - 1;
    target.lastBar = { ...data.candles[lastIdx] };
    target.prevBarClose = lastIdx >= 1 ? data.candles[lastIdx - 1].close : target.lastBar.open;

    if (paneKey === 'spot') {
      if (isNewDataset) {
        if (Array.isArray(data.ma20)) target.ma20Series.setData(data.ma20);
        if (Array.isArray(data.ema50)) target.ema50Series.setData(data.ema50);
      } else {
        if (Array.isArray(data.ma20) && data.ma20.length > 0) {
          target.ma20Series.update(data.ma20[data.ma20.length - 1]);
        }
        if (Array.isArray(data.ema50) && data.ema50.length > 0) {
          target.ema50Series.update(data.ema50[data.ema50.length - 1]);
        }
      }

      const latest = data.latest || {};
      const c = latest.candle;
      if (c) {
        const sign = latest.change >= 0 ? '+' : '';
        const ohlcEl = document.getElementById('spot-chart-ohlc');
        ohlcEl.textContent = `O ${fmtNum(c.open)} H ${fmtNum(c.high)} L ${fmtNum(c.low)} C ${fmtNum(c.close)} ${sign}${fmtNum(latest.change)} (${sign}${fmtNum(latest.changePct)}%)`;
        ohlcEl.className = `legend-ohlc tabular ${latest.change >= 0 ? 'bullish' : 'bearish'}`;
      }
      document.getElementById('spot-ma20-val').textContent = fmtNum(latest.ma20);
      document.getElementById('spot-ema50-val').textContent = fmtNum(latest.ema50);
    } else {
      if (isNewDataset) {
        const volData = data.candles.map((bar) => ({
          time: bar.time,
          value: bar.volume || 0,
          color: bar.close >= bar.open ? 'rgba(38, 166, 154, 0.45)' : 'rgba(239, 83, 80, 0.45)',
        }));
        target.volSeries.setData(volData);
      } else {
        const lastBar = data.candles[data.candles.length - 1];
        if (lastBar) {
          target.volSeries.update({
            time: lastBar.time,
            value: lastBar.volume || 0,
            color: lastBar.close >= lastBar.open ? 'rgba(38, 166, 154, 0.45)' : 'rgba(239, 83, 80, 0.45)',
          });
        }
      }

      const latest = data.latest || {};
      const c = latest.candle;
      if (c) {
        const sign = latest.change >= 0 ? '+' : '';
        const ohlcEl = document.getElementById(`${paneKey}-chart-ohlc`);
        ohlcEl.textContent = `O ${fmtNum(c.open)} H ${fmtNum(c.high)} L ${fmtNum(c.low)} C ${fmtNum(c.close)} ${sign}${fmtNum(latest.change)} (${sign}${fmtNum(latest.changePct)}%)`;
        ohlcEl.className = `legend-ohlc tabular ${latest.change >= 0 ? 'bullish' : 'bearish'}`;
      }
    }

    if (isNewDataset) {
      zoomChartToRecentBars(paneKey, data.candles.length);
      // Unlock autoScale after initial fit frame so vertical cursor dragging works immediately
      requestAnimationFrame(() => {
        target.chart.priceScale('right').applyOptions({ autoScale: false });
        updateTvPositionOverlays();
      });
    } else {
      target.totalBars = data.candles.length;
      updateTvPositionOverlays();
    }
  } catch (err) {
    console.error(`Error loading ${paneKey} chart candles:`, err);
  }
}

async function refreshAllCharts(fitContent = false) {
  const spotNeo = INDEX_NEOSYMBOLS[state.symbol] || 'nse_cm|Nifty 50';
  const callLeg = getActiveCallLeg();
  const putLeg = getActivePutLeg();

  document.getElementById('spot-chart-title').textContent = `${state.symbol} · ${state.timeframe.replace('min', 'm')} · INDICES`;
  if (callLeg) {
    document.getElementById('call-chart-title').textContent = `${callLeg.displayName} · ${state.timeframe.replace('min', 'm')} · NFO-OPT`;
    document.getElementById('call-chart-greeks').textContent = `IV: ${fmtNum(callLeg.iv, 1)}% | Δ: ${fmtNum(callLeg.delta, 2)} | Γ: ${fmtNum(callLeg.gamma, 4)} | Θ: ${fmtNum(callLeg.theta, 1)}`;
    document.getElementById('call-float-ask').textContent = fmtNum(callLeg.ask);
    document.getElementById('call-float-bid').textContent = fmtNum(callLeg.bid);
    document.getElementById('call-float-spread').textContent = fmtNum(callLeg.ask - callLeg.bid, 2);
  }
  if (putLeg) {
    document.getElementById('put-chart-title').textContent = `${putLeg.displayName} · ${state.timeframe.replace('min', 'm')} · NFO-OPT`;
    document.getElementById('put-chart-greeks').textContent = `IV: ${fmtNum(putLeg.iv, 1)}% | Δ: ${fmtNum(putLeg.delta, 2)} | Γ: ${fmtNum(putLeg.gamma, 4)} | Θ: ${fmtNum(putLeg.theta, 1)}`;
    document.getElementById('put-float-ask').textContent = fmtNum(putLeg.ask);
    document.getElementById('put-float-bid').textContent = fmtNum(putLeg.bid);
    document.getElementById('put-float-spread').textContent = fmtNum(putLeg.ask - putLeg.bid, 2);
  }

  await Promise.all([
    fetchCandlesForPane('spot', spotNeo, fitContent),
    callLeg?.instrumentToken ? fetchCandlesForPane('call', callLeg.instrumentToken, fitContent) : Promise.resolve(),
    putLeg?.instrumentToken ? fetchCandlesForPane('put', putLeg.instrumentToken, fitContent) : Promise.resolve(),
  ]);
}

/* ==========================================================================
   RENDER UI PANELS
   ========================================================================== */
function renderExpiryDropdowns(expiries) {
  const chainSel = document.getElementById('chain-expiry-select');
  const orderSel = document.getElementById('order-expiry-select');

  const html = expiries
    .map((e) => `<option value="${e.isoDate}">${e.displayLabel}</option>`)
    .join('');

  if (chainSel.innerHTML !== html) {
    chainSel.innerHTML = html;
    orderSel.innerHTML = html;
  }
  if (state.chainExpiry) chainSel.value = state.chainExpiry;
  if (state.orderExpiry) orderSel.value = state.orderExpiry;
}

function renderTopNavbar(data) {
  const isBull = data.spotChange >= 0;
  const tickerEl = document.getElementById('ticker-nifty');
  tickerEl.innerHTML = `
    <span class="ticker-name">${data.symbol}</span>
    <span class="ticker-arrow ${isBull ? 'bullish' : 'bearish'}">${isBull ? '▲' : '▼'}</span>
    <span class="ticker-ltp tabular">${fmtNum(data.spotPrice)}</span>
    <span class="ticker-chg ${isBull ? 'bullish' : 'bearish'} tabular">${isBull ? '+' : ''}${fmtNum(data.spotChange)} (${isBull ? '+' : ''}${fmtNum(data.spotChangePct)}%)</span>
  `;

  document.getElementById('ticker-fut-ltp').textContent = fmtNum(data.futuresPrice || data.referencePrice);
  document.getElementById('ticker-straddle').textContent = data.summary?.atmStraddle ? `₹${fmtNum(data.summary.atmStraddle)}` : '--';
  document.getElementById('ticker-atm-iv').textContent = data.summary?.atmIv ? `${fmtNum(data.summary.atmIv)}%` : '--';
  document.getElementById('last-sync-time').textContent = data.fetchedAtIst || '--:--:--';
  document.getElementById('toolbar-symbol-label').textContent = data.symbol;

  renderPaperAccountBadges();
}

function renderPaperAccountBadges() {
  const acc = state.paperAccount;
  if (!acc) return;

  const mtmEl = document.getElementById('nav-total-mtm');
  mtmEl.textContent = fmtSignedMoney(acc.total_mtm);
  mtmEl.className = `badge-value tabular ${acc.total_mtm > 0 ? 'bullish' : acc.total_mtm < 0 ? 'bearish' : ''}`;

  const openPnlEl = document.getElementById('nav-unrealized-pnl');
  openPnlEl.textContent = fmtSignedMoney(acc.unrealized_pnl);
  openPnlEl.className = `badge-value tabular ${acc.unrealized_pnl > 0 ? 'bullish' : acc.unrealized_pnl < 0 ? 'bearish' : ''}`;

  document.getElementById('nav-available-funds').textContent = fmtCompactINR(acc.available_margin);
  document.getElementById('order-avail-margin').textContent = `₹${fmtNum(acc.available_margin)}`;
  document.getElementById('order-used-margin').textContent = fmtCompactINR(acc.used_margin);

  const realEl = document.getElementById('oc-realized-pnl');
  realEl.textContent = fmtSignedMoney(acc.realized_pnl);
  realEl.className = `tabular ${acc.realized_pnl > 0 ? 'bullish' : acc.realized_pnl < 0 ? 'bearish' : ''}`;

  // Update Underlying Chart Portfolio Live P&L HUD
  const hudMtm = document.getElementById('hud-total-mtm');
  hudMtm.textContent = fmtSignedMoney(acc.total_mtm);
  hudMtm.className = `hud-mtm tabular ${acc.total_mtm > 0 ? 'bullish' : acc.total_mtm < 0 ? 'bearish' : ''}`;

  const hudOpen = document.getElementById('hud-open-pnl');
  hudOpen.textContent = fmtSignedMoney(acc.unrealized_pnl);
  hudOpen.className = `tabular ${acc.unrealized_pnl > 0 ? 'bullish' : acc.unrealized_pnl < 0 ? 'bearish' : ''}`;
  document.getElementById('hud-open-count').textContent = String(acc.open_positions_count || 0);

  document.getElementById('tab-pos-count').textContent = String(acc.open_positions_count || 0);
  document.getElementById('tab-ord-count').textContent = String((acc.orders || []).length);

  const tlRealEl = document.getElementById('trade-log-realized-pnl');
  if (tlRealEl) {
    tlRealEl.textContent = fmtSignedMoney(acc.realized_pnl);
    tlRealEl.className = `tabular ${acc.realized_pnl > 0 ? 'bullish' : acc.realized_pnl < 0 ? 'bearish' : ''}`;
  }
  const tlCapEl = document.getElementById('trade-log-capital');
  if (tlCapEl) {
    const netEquity = (acc.starting_capital || 1000000) + (acc.realized_pnl || 0);
    tlCapEl.textContent = `₹${fmtNum(netEquity)}`;
  }
}

function renderOptionChainPanel(data) {
  const isBull = data.spotChange >= 0;
  document.getElementById('oc-symbol-title').textContent = data.symbol;
  const spotEl = document.getElementById('oc-spot-price');
  spotEl.textContent = fmtNum(data.spotPrice);
  spotEl.className = `und-price tabular ${isBull ? 'bullish' : 'bearish'}`;

  const chgEl = document.getElementById('oc-spot-change');
  chgEl.textContent = `${isBull ? '+' : ''}${fmtNum(data.spotChange)} (${isBull ? '+' : ''}${fmtNum(data.spotChangePct)}%)`;
  chgEl.className = `und-change tabular ${isBull ? 'bullish' : 'bearish'}`;

  document.getElementById('oc-lot-badge').textContent = `Lot: ${data.lotSize}`;

  // Update Column Sub-headers for OI vs Greeks mode
  const isGreeks = state.displayMode === 'greeks';
  document.getElementById('oc-header-call-sub').textContent = isGreeks ? 'Δ / IV%' : '% Chg';
  document.getElementById('oc-header-put-sub').textContent = isGreeks ? 'Δ / IV%' : '% Chg';

  // Map open positions by neo_symbol for instant row badges
  const posByNeo = {};
  for (const p of state.paperAccount?.positions || []) {
    posByNeo[p.neo_symbol] = p;
  }

  const tbody = document.getElementById('oc-rows-body');
  tbody.innerHTML = (data.strikes || [])
    .map((row) => {
      const k = row.strike;
      const ce = row.call;
      const pe = row.put;

      const ceSub = isGreeks
        ? `<span class="oc-sub-metric tabular">${fmtNum(ce.delta, 2)} · ${fmtNum(ce.iv, 1)}%</span>`
        : `<span class="oc-sub-metric tabular ${ce.changePct >= 0 ? 'bullish' : 'bearish'}">${ce.changePct >= 0 ? '+' : ''}${fmtNum(ce.changePct, 1)}%</span>`;

      const peSub = isGreeks
        ? `<span class="oc-sub-metric tabular">${fmtNum(pe.delta, 2)} · ${fmtNum(pe.iv, 1)}%</span>`
        : `<span class="oc-sub-metric tabular ${pe.changePct >= 0 ? 'bullish' : 'bearish'}">${pe.changePct >= 0 ? '+' : ''}${fmtNum(pe.changePct, 1)}%</span>`;

      const cePos = posByNeo[ce.instrumentToken];
      const pePos = posByNeo[pe.instrumentToken];

      const cePosTag = cePos
        ? `<span class="pos-mini-tag ${cePos.net_qty < 0 ? 'short' : ''}">${cePos.net_qty > 0 ? '+' : ''}${cePos.net_qty}</span>`
        : '';
      const pePosTag = pePos
        ? `<span class="pos-mini-tag ${pePos.net_qty < 0 ? 'short' : ''}">${pePos.net_qty > 0 ? '+' : ''}${pePos.net_qty}</span>`
        : '';

      const isCallSel = Number(state.selectedCallStrike) === Number(k);
      const isPutSel = Number(state.selectedPutStrike) === Number(k);

      return `
        <div class="oc-row" data-strike="${k}">
          <div class="oc-cell-call ${isCallSel ? 'selected-leg' : ''}" data-action="select-call" data-strike="${k}">
            ${ceSub}
            <span class="oc-ltp tabular">${cePosTag}${fmtNum(ce.ltp)}</span>
          </div>
          <div class="oc-cell-strike" data-action="select-both" data-strike="${k}">
            <span class="strike-label tabular ${row.isAtm ? 'atm-pill' : ''}">${k}</span>
            <div class="oi-bars-wrap" title="Call OI: ${ce.oi.toLocaleString('en-IN')} | Put OI: ${pe.oi.toLocaleString('en-IN')}">
              <div class="oi-bar-call" style="inline-size: ${row.callOiPct}%"></div>
              <div class="oi-bar-put" style="inline-size: ${row.putOiPct}%"></div>
            </div>
          </div>
          <div class="oc-cell-put ${isPutSel ? 'selected-leg' : ''}" data-action="select-put" data-strike="${k}">
            <span class="oc-ltp tabular">${fmtNum(pe.ltp)}${pePosTag}</span>
            ${peSub}
          </div>
        </div>
      `;
    })
    .join('');

  // Footer Summary
  const sum = data.summary || {};
  document.getElementById('sum-pcr').textContent = fmtNum(sum.pcr, 2);
  document.getElementById('sum-max-pain').textContent = sum.maxPain || '--';
  document.getElementById('sum-atm-iv').textContent = sum.atmIv ? `${fmtNum(sum.atmIv, 2)}%` : '--';
  document.getElementById('sum-ivp').textContent = sum.ivPercentile
    ? `${fmtNum(sum.ivPercentile, 1)} - ${sum.ivRegime}`
    : '--';
}

/**
 * Smart position-aware margin & button updater for Call (CE) and Put (PE) execution pads.
 *
 * - If you already BOUGHT the option (`netQty > 0`), selling up to `netQty` shares simply exits your
 *   existing long position and requires ₹0 margin (`Req. ₹0 (Exit Long)`), NOT short option writing margin!
 *   Only if `orderQty > netQty` does the excess net short portion require writing margin.
 * - Similarly, if you already SOLD/SHORTED the option (`netQty < 0`), buying up to `|netQty|` shares
 *   exits your short position (`Req. ₹0 (Exit Short)`) and releases your blocked short margin.
 */
function updateLegOrderPadUI(prefix, optType, leg, orderQty) {
  if (!leg) return;

  document.getElementById(`${prefix}-contract-name`).textContent = leg.displayName;
  document.getElementById(`${prefix}-pad-ltp`).textContent = `₹${fmtNum(leg.ltp)}`;

  const buyReqEl = document.getElementById(`${prefix}-buy-req`);
  const sellReqEl = document.getElementById(`${prefix}-sell-req`);
  const buyBtnEl = document.getElementById(`btn-pad-${prefix}-buy`);
  const sellBtnEl = document.getElementById(`btn-pad-${prefix}-sell`);
  const floatBuySmall = document.querySelector(`#btn-chart-${prefix}-buy small`);
  const floatSellSmall = document.querySelector(`#btn-chart-${prefix}-sell small`);

  const pos = (state.paperAccount?.positions || []).find((p) => p.neo_symbol === leg.instrumentToken);
  const netQty = pos ? Number(pos.net_qty) : 0;
  const lotSize = Math.max(1, state.lotSize || 65);

  // 1. BUY Margin & Button Label
  if (netQty < 0) {
    // User has an open SHORT position (`netQty < 0`)
    const shortAbs = Math.abs(netQty);
    const excessLongQty = Math.max(0, orderQty - shortAbs);
    if (excessLongQty === 0) {
      buyReqEl.textContent = 'Req. ₹0 (Exit Short)';
      buyReqEl.className = 'margin-req tabular margin-free';
      buyBtnEl.textContent = `Buy ${optType} · Exit`;
      if (floatBuySmall) floatBuySmall.textContent = 'EXIT';
    } else {
      buyReqEl.textContent = `Req. ${fmtCompactINR(leg.ltp * excessLongQty)} (Net +${excessLongQty})`;
      buyReqEl.className = 'margin-req tabular';
      buyBtnEl.textContent = `Buy ${optType} · Flip`;
      if (floatBuySmall) floatBuySmall.textContent = 'FLIP';
    }
  } else {
    buyReqEl.textContent = `Req. ${fmtCompactINR(leg.ltp * orderQty)}`;
    buyReqEl.className = 'margin-req tabular';
    buyBtnEl.textContent = `Buy ${optType}`;
    if (floatBuySmall) floatBuySmall.textContent = 'BUY';
  }

  // 2. SELL Margin & Button Label
  if (netQty > 0) {
    // User has an open LONG position (`netQty > 0`) — can sell up to `netQty` with ZERO margin requirement!
    const excessShortQty = Math.max(0, orderQty - netQty);
    if (excessShortQty === 0) {
      sellReqEl.textContent = 'Req. ₹0 (Exit Long)';
      sellReqEl.className = 'margin-req tabular margin-free';
      sellBtnEl.textContent = `Sell ${optType} · Exit`;
      if (floatSellSmall) floatSellSmall.textContent = 'EXIT';
    } else {
      const excessShortLots = excessShortQty / lotSize;
      sellReqEl.textContent = `Req. ${fmtCompactINR(leg.sellMarginReq * excessShortLots)} (Net -${excessShortQty})`;
      sellReqEl.className = 'margin-req tabular';
      sellBtnEl.textContent = `Sell ${optType} · Flip`;
      if (floatSellSmall) floatSellSmall.textContent = 'FLIP';
    }
  } else {
    const orderLots = orderQty / lotSize;
    sellReqEl.textContent = `Req. ${fmtCompactINR(leg.sellMarginReq * orderLots)}`;
    sellReqEl.className = 'margin-req tabular';
    sellBtnEl.textContent = `Sell ${optType}`;
    if (floatSellSmall) floatSellSmall.textContent = 'SELL';
  }

  // 3. Inline Position Readout
  const statusEl = document.getElementById(`${prefix}-inline-pos`);
  if (pos && netQty !== 0) {
    statusEl.innerHTML = `Pos: <strong>${netQty > 0 ? '+' : ''}${netQty}</strong> | <span class="${pos.unrealized_pnl >= 0 ? 'bullish' : 'bearish'}">${fmtSignedMoney(pos.unrealized_pnl)}</span>`;
  } else {
    statusEl.textContent = `No Open ${optType} Pos`;
  }
}

function renderOrderPadPanel() {
  const chainObj = getOrderPadChain();
  if (!chainObj || !chainObj.strikes) return;

  const callStrikeSel = document.getElementById('call-strike-select');
  const putStrikeSel = document.getElementById('put-strike-select');

  const optionsHtml = chainObj.strikes
    .map((r) => `<option value="${r.strike}">${r.strike}</option>`)
    .join('');

  if (callStrikeSel.innerHTML !== optionsHtml) {
    callStrikeSel.innerHTML = optionsHtml;
    putStrikeSel.innerHTML = optionsHtml;
  }

  callStrikeSel.value = String(state.selectedCallStrike);
  putStrikeSel.value = String(state.selectedPutStrike);

  const callLeg = getActiveCallLeg();
  const putLeg = getActivePutLeg();

  const callQtyInput = document.getElementById('call-qty-input');
  const putQtyInput = document.getElementById('put-qty-input');
  callQtyInput.step = String(state.lotSize);
  callQtyInput.min = String(state.lotSize);
  putQtyInput.step = String(state.lotSize);
  putQtyInput.min = String(state.lotSize);

  const callQty = Math.max(state.lotSize, Number(callQtyInput.value) || state.lotSize);
  const putQty = Math.max(state.lotSize, Number(putQtyInput.value) || state.lotSize);

  updateLegOrderPadUI('call', 'CE', callLeg, callQty);
  updateLegOrderPadUI('put', 'PE', putLeg, putQty);
}

/* ==========================================================================
   TRADINGVIEW BEST-PRACTICE RIGHT-SIDE LIVE P&L CHART OVERLAYS
   ========================================================================== */
function updateSingleTvOverlay(paneKey, leg) {
  const overlayEl = document.getElementById(`tv-pos-overlay-${paneKey}`);
  const chartObj = charts[paneKey];
  if (!overlayEl || !chartObj || !chartObj.candleSeries) return;

  // Remove old native price line if present
  if (chartObj.entryPriceLine) {
    try {
      chartObj.candleSeries.removePriceLine(chartObj.entryPriceLine);
    } catch (_) {}
    chartObj.entryPriceLine = null;
  }

  if (!leg || !state.paperAccount) {
    overlayEl.classList.add('hidden');
    return;
  }

  const pos = (state.paperAccount.positions || []).find((p) => p.neo_symbol === leg.instrumentToken);
  if (!pos || !pos.net_qty) {
    overlayEl.classList.add('hidden');
    return;
  }

  // Add TradingView right-axis price tag at Avg Entry Price
  const isLong = pos.net_qty > 0;
  chartObj.entryPriceLine = chartObj.candleSeries.createPriceLine({
    price: pos.avg_price,
    color: isLong ? '#2962ff' : '#df514c',
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
    title: `Avg ${fmtNum(pos.avg_price)}`,
  });

  // Compute Y coordinate on the candlestick canvas for Avg Entry Price
  const canvasEl = document.getElementById(`chart-${paneKey}-canvas`);
  const paneHeight = canvasEl ? canvasEl.clientHeight : 220;
  let yCoord = chartObj.candleSeries.priceToCoordinate(pos.avg_price);
  if (yCoord === null || yCoord === undefined || Number.isNaN(yCoord)) {
    yCoord = paneHeight * 0.5;
  }
  // Clamp vertically so the TradingView Position Pill never gets clipped off-screen
  const clampedY = Math.max(26, Math.min(paneHeight - 30, yCoord));

  overlayEl.style.top = `${clampedY}px`;
  overlayEl.classList.remove('hidden');
  overlayEl.classList.toggle('short-pos', !isLong);

  document.getElementById(`tv-${paneKey}-qty`).textContent = `${isLong ? '+' : ''}${pos.net_qty}`;
  document.getElementById(`tv-${paneKey}-avg`).textContent = `@ ₹${fmtNum(pos.avg_price)}`;

  const pnlEl = document.getElementById(`tv-${paneKey}-pnl`);
  const signPct = pos.pnl_pct >= 0 ? '+' : '';
  pnlEl.textContent = `${fmtSignedMoney(pos.unrealized_pnl)} (${signPct}${fmtNum(pos.pnl_pct, 1)}%)`;
  pnlEl.className = `tv-pos-pnl tabular ${pos.unrealized_pnl >= 0 ? 'profit' : 'loss'}`;
}

function updateTvPositionOverlays() {
  updateSingleTvOverlay('call', getActiveCallLeg());
  updateSingleTvOverlay('put', getActivePutLeg());
}

/* ==========================================================================
   PAPER ORDER EXECUTION & POSITION MANAGEMENT
   ========================================================================== */
async function executePaperOrder(optType, side, customQty = null) {
  const leg = optType === 'CE' ? getActiveCallLeg() : getActivePutLeg();
  const strike = optType === 'CE' ? state.selectedCallStrike : state.selectedPutStrike;
  if (!leg || !leg.instrumentToken) {
    showToast(`No ${optType} contract loaded`, true);
    return;
  }

  const qtyInput = document.getElementById(optType === 'CE' ? 'call-qty-input' : 'put-qty-input');
  const quantity = customQty || Math.max(state.lotSize, Number(qtyInput.value) || state.lotSize);
  const execPrice = side === 'BUY' ? leg.ask || leg.ltp : leg.bid || leg.ltp;

  try {
    const res = await fetch('/api/v1/paper/order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        neo_symbol: leg.instrumentToken,
        trading_symbol: leg.tradingSymbol,
        display_name: leg.displayName,
        underlying: state.symbol,
        expiry: state.orderExpiry || state.chainExpiry,
        strike: Number(strike),
        option_type: optType,
        side,
        quantity: Number(quantity),
        price: Number(execPrice),
        lot_size: state.lotSize,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.status !== 'OK') {
      showToast(data.detail || 'Order rejected', true);
      return;
    }

    state.paperAccount = data.account;
    showToast(`${side} ${quantity} ${leg.displayName} @ ₹${fmtNum(execPrice)}`);
    renderPaperAccountBadges();
    if (state.chainData) renderOptionChainPanel(state.chainData);
    renderOrderPadPanel();
    renderPositionsAndOrdersTables();
    updateTvPositionOverlays();
  } catch (err) {
    showToast(`Order failed: ${err.message}`, true);
  }
}

async function closePositionByNeoSymbol(neoSymbol) {
  if (!neoSymbol) return;
  try {
    const res = await fetch('/api/v1/paper/close', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ neo_symbol: neoSymbol }),
    });
    const data = await res.json();
    if (data.status === 'OK') {
      state.paperAccount = data.account;
      showToast(`Closed ${data.order.display_name} @ ₹${fmtNum(data.order.price)}`);
      renderPaperAccountBadges();
      if (state.chainData) renderOptionChainPanel(state.chainData);
      renderOrderPadPanel();
      renderPositionsAndOrdersTables();
      updateTvPositionOverlays();
    }
  } catch (err) {
    showToast(`Failed to close position: ${err.message}`, true);
  }
}

async function reversePositionForPane(optType) {
  const leg = optType === 'CE' ? getActiveCallLeg() : getActivePutLeg();
  if (!leg) return;
  const pos = (state.paperAccount?.positions || []).find((p) => p.neo_symbol === leg.instrumentToken);
  if (!pos || !pos.net_qty) return;

  const reverseSide = pos.net_qty > 0 ? 'SELL' : 'BUY';
  const doubleQty = Math.abs(pos.net_qty) * 2;
  await executePaperOrder(optType, reverseSide, doubleQty);
}

async function closeAllPositions() {
  try {
    const res = await fetch('/api/v1/paper/close', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ close_all: true }),
    });
    const data = await res.json();
    if (data.status === 'OK') {
      state.paperAccount = data.account;
      showToast(`Squared off ${data.closed_count} position(s)`);
      renderPaperAccountBadges();
      if (state.chainData) renderOptionChainPanel(state.chainData);
      renderOrderPadPanel();
      renderPositionsAndOrdersTables();
      updateTvPositionOverlays();
    }
  } catch (err) {
    showToast(`Exit all failed: ${err.message}`, true);
  }
}

function renderPositionsAndOrdersTables() {
  const acc = state.paperAccount;
  if (!acc) return;

  const posTbody = document.getElementById('positions-tbody');
  if (!acc.positions || acc.positions.length === 0) {
    posTbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:#8c8c8c;padding:18px;">No open paper positions. Click Buy or Sell on the Scalper Order Pad or Chart Overlay to start scalping!</td></tr>`;
  } else {
    posTbody.innerHTML = acc.positions
      .map(
        (p) => `
      <tr>
        <td><strong>${p.display_name}</strong></td>
        <td><span class="leg-badge ${p.net_qty > 0 ? 'call' : 'put'}">${p.side}</span></td>
        <td class="num tabular"><strong>${p.net_qty > 0 ? '+' : ''}${p.net_qty}</strong></td>
        <td class="num tabular">₹${fmtNum(p.avg_price)}</td>
        <td class="num tabular">₹${fmtNum(p.ltp)}</td>
        <td class="num tabular ${p.unrealized_pnl >= 0 ? 'bullish' : 'bearish'}"><strong>${fmtSignedMoney(p.unrealized_pnl)}</strong></td>
        <td class="num tabular ${p.pnl_pct >= 0 ? 'bullish' : 'bearish'}">${p.pnl_pct >= 0 ? '+' : ''}${fmtNum(p.pnl_pct, 2)}%</td>
        <td class="num"><button class="table-exit-btn" data-close-neo="${p.neo_symbol}">Exit ×</button></td>
      </tr>
    `
      )
      .join('');
  }

  const ordTbody = document.getElementById('orders-tbody');
  if (!acc.orders || acc.orders.length === 0) {
    ordTbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:#8c8c8c;padding:18px;">No paper trades executed yet.</td></tr>`;
  } else {
    ordTbody.innerHTML = acc.orders
      .map(
        (o) => `
      <tr>
        <td class="tabular">${o.time}</td>
        <td class="tabular">${o.order_id}</td>
        <td><strong>${o.display_name}</strong></td>
        <td><span class="leg-badge ${o.side === 'BUY' ? 'call' : 'put'}">${o.side}</span></td>
        <td class="num tabular">${o.quantity}</td>
        <td class="num tabular">₹${fmtNum(o.price)}</td>
        <td class="num tabular ${o.realized_pnl_delta > 0 ? 'bullish' : o.realized_pnl_delta < 0 ? 'bearish' : ''}">
          ${o.realized_pnl_delta ? fmtSignedMoney(o.realized_pnl_delta) : '--'}
        </td>
      </tr>
    `
      )
      .join('');
  }
}

/* ==========================================================================
   EVENT LISTENERS & POLLING LOOP
   ========================================================================== */
function setupPolling() {
  clearInterval(state.pollTimer);
  clearInterval(state.liveTickTimer);
  if (state.pollIntervalMs > 0) {
    state.pollTimer = setInterval(() => {
      fetchOptionChain(false);
    }, state.pollIntervalMs);
    // Fast 2-second live forming candle & quote tick loop
    state.liveTickTimer = setInterval(() => {
      pollLiveCandleTicks();
    }, 2000);
  }
}

function wireEvents() {
  // Symbol Switcher
  document.getElementById('symbol-select').addEventListener('change', (e) => {
    state.symbol = e.target.value;
    state.chainExpiry = null;
    state.orderExpiry = null;
    state.atmStrike = null;
    state.selectedCallStrike = null;
    state.selectedPutStrike = null;
    fetchOptionChain(true);
  });

  // Poll Interval Selector & Manual Refresh
  document.getElementById('poll-interval-select').addEventListener('change', (e) => {
    state.pollIntervalMs = Number(e.target.value);
    setupPolling();
  });
  document.getElementById('btn-manual-refresh').addEventListener('click', () => {
    fetchOptionChain(false);
  });

  // Expiry Selectors & Chain-Link Sync
  document.getElementById('chain-expiry-select').addEventListener('change', (e) => {
    state.chainExpiry = e.target.value;
    if (state.syncExpiries) state.orderExpiry = state.chainExpiry;
    fetchOptionChain(true);
  });

  document.getElementById('order-expiry-select').addEventListener('change', (e) => {
    state.orderExpiry = e.target.value;
    if (state.syncExpiries) state.chainExpiry = state.orderExpiry;
    fetchOptionChain(true);
  });

  const toggleSync = () => {
    state.syncExpiries = !state.syncExpiries;
    document.getElementById('btn-sync-expiry').classList.toggle('active', state.syncExpiries);
    document.getElementById('btn-order-link').classList.toggle('active', state.syncExpiries);
    if (state.syncExpiries) {
      state.orderExpiry = state.chainExpiry;
      state.orderChainData = state.chainData;
      fetchOptionChain(false);
    }
  };
  document.getElementById('btn-sync-expiry').addEventListener('click', toggleSync);
  document.getElementById('btn-order-link').addEventListener('click', toggleSync);

  // OI / Greeks Mode Toggle
  for (const btn of document.querySelectorAll('.mode-btn')) {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.mode-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.displayMode = btn.dataset.mode;
      if (state.chainData) renderOptionChainPanel(state.chainData);
    });
  }

  // Option Chain Row Clicks (Call cell, Put cell, Strike cell)
  document.getElementById('oc-rows-body').addEventListener('click', (e) => {
    const cell = e.target.closest('[data-action]');
    if (!cell) return;
    const action = cell.dataset.action;
    const strike = Number(cell.dataset.strike);

    if (action === 'select-call') {
      state.selectedCallStrike = strike;
      document.getElementById('pane-call').classList.add('active-pane');
      document.getElementById('pane-put').classList.remove('active-pane');
    } else if (action === 'select-put') {
      state.selectedPutStrike = strike;
      document.getElementById('pane-put').classList.add('active-pane');
      document.getElementById('pane-call').classList.remove('active-pane');
    } else if (action === 'select-both') {
      state.selectedCallStrike = strike;
      state.selectedPutStrike = strike;
    }

    if (state.chainData) renderOptionChainPanel(state.chainData);
    renderOrderPadPanel();
    refreshAllCharts(true);
  });

  // Order Pad Strike Dropdowns & Qty Inputs
  document.getElementById('call-strike-select').addEventListener('change', (e) => {
    state.selectedCallStrike = Number(e.target.value);
    if (state.chainData) renderOptionChainPanel(state.chainData);
    renderOrderPadPanel();
    refreshAllCharts(true);
  });

  document.getElementById('put-strike-select').addEventListener('change', (e) => {
    state.selectedPutStrike = Number(e.target.value);
    if (state.chainData) renderOptionChainPanel(state.chainData);
    renderOrderPadPanel();
    refreshAllCharts(true);
  });

  document.getElementById('call-qty-input').addEventListener('input', () => renderOrderPadPanel());
  document.getElementById('put-qty-input').addEventListener('input', () => renderOrderPadPanel());

  // Lot Multiplier Presets (1L, 2L, 5L, 10L)
  for (const btn of document.querySelectorAll('.lot-preset-btn')) {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.lot-preset-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.lotMultiplier = Number(btn.dataset.lots) || 1;
      const qty = state.lotSize * state.lotMultiplier;
      document.getElementById('call-qty-input').value = String(qty);
      document.getElementById('put-qty-input').value = String(qty);
      renderOrderPadPanel();
    });
  }

  // Timeframe & Range Selectors
  for (const btn of document.querySelectorAll('.tf-btn')) {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tf-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.timeframe = btn.dataset.tf;
      refreshAllCharts(true);
    });
  }

  for (const btn of document.querySelectorAll('.range-btn')) {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.daysRange = Number(btn.dataset.days) || 2;
      refreshAllCharts(true);
    });
  }

  document.getElementById('btn-chart-auto-fit')?.addEventListener('click', () => {
    resetAllChartScales();
    showToast('Chart scales auto-fitted');
  });

  // Bottom Panel Tabs (Scalper Order Window | Open Positions | Trade Log)
  for (const tabBtn of document.querySelectorAll('[data-bottom-tab]')) {
    tabBtn.addEventListener('click', () => {
      const targetView = tabBtn.dataset.bottomTab;
      document.querySelectorAll('[data-bottom-tab]').forEach((b) => b.classList.remove('active'));
      tabBtn.classList.add('active');
      document.querySelectorAll('.bottom-view').forEach((v) => v.classList.remove('active'));
      document.getElementById(`bottom-view-${targetView}`)?.classList.add('active');
    });
  }

  document.getElementById('btn-top-mtm').addEventListener('click', () => {
    document.querySelector('[data-bottom-tab="positions-tab"]')?.click();
  });

  // Order Execution Buttons (Order Pad + Floating Chart Buttons)
  document.getElementById('btn-pad-call-buy').addEventListener('click', () => executePaperOrder('CE', 'BUY'));
  document.getElementById('btn-pad-call-sell').addEventListener('click', () => executePaperOrder('CE', 'SELL'));
  document.getElementById('btn-pad-put-buy').addEventListener('click', () => executePaperOrder('PE', 'BUY'));
  document.getElementById('btn-pad-put-sell').addEventListener('click', () => executePaperOrder('PE', 'SELL'));

  document.getElementById('btn-chart-call-buy').addEventListener('click', () => executePaperOrder('CE', 'BUY'));
  document.getElementById('btn-chart-call-sell').addEventListener('click', () => executePaperOrder('CE', 'SELL'));
  document.getElementById('btn-chart-put-buy').addEventListener('click', () => executePaperOrder('PE', 'BUY'));
  document.getElementById('btn-chart-put-sell').addEventListener('click', () => executePaperOrder('PE', 'SELL'));

  // TradingView Right-Side Chart Position Pill Actions (Reverse ⇅ & Close ×)
  document.getElementById('btn-tv-call-rev').addEventListener('click', () => reversePositionForPane('CE'));
  document.getElementById('btn-tv-call-close').addEventListener('click', () => {
    const leg = getActiveCallLeg();
    if (leg) closePositionByNeoSymbol(leg.instrumentToken);
  });

  document.getElementById('btn-tv-put-rev').addEventListener('click', () => reversePositionForPane('PE'));
  document.getElementById('btn-tv-put-close').addEventListener('click', () => {
    const leg = getActivePutLeg();
    if (leg) closePositionByNeoSymbol(leg.instrumentToken);
  });

  // Positions Table Exit Buttons
  document.getElementById('positions-tbody').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-close-neo]');
    if (btn) closePositionByNeoSymbol(btn.dataset.closeNeo);
  });

  // Flatten All & Reset Paper Capital / Clear Trade Log
  document.getElementById('btn-flatten-all-top').addEventListener('click', () => closeAllPositions());

  const resetPaperAccountAndHistory = async () => {
    const res = await fetch('/api/v1/paper/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ starting_capital: 1000000 }),
    });
    const data = await res.json();
    if (data.status === 'OK') {
      state.paperAccount = data.account;
      showToast('History cleared & capital restored to ₹10,00,000');
      renderPaperAccountBadges();
      if (state.chainData) renderOptionChainPanel(state.chainData);
      renderOrderPadPanel();
      renderPositionsAndOrdersTables();
      updateTvPositionOverlays();
    }
  };

  document.getElementById('btn-reset-paper').addEventListener('click', resetPaperAccountAndHistory);
  document.getElementById('btn-clear-trade-log')?.addEventListener('click', resetPaperAccountAndHistory);

  // Live IST Clock in Chart Footer & Live Candle Countdown
  setInterval(() => {
    const now = new Date();
    const istStr = now.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false });
    const clockEl = document.getElementById('live-ist-clock');
    if (clockEl) clockEl.textContent = `${istStr} UTC+5:30`;
    if (state.candleRemainingSec > 0) {
      state.candleRemainingSec -= 1;
      renderLiveCandleTimer();
    }
  }, 1000);
}

window.addEventListener('DOMContentLoaded', () => {
  initCharts();
  wireEvents();
  fetchOptionChain(true);
  setupPolling();
});
