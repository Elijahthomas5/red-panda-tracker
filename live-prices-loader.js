// live-prices-loader.js
// Loaded by index.html to fetch prices from live-prices.json (no API key needed).
// Patches into the existing RP tracker without modifying index.html logic.
(async function() {
  'use strict';

  const LIVE_PRICES_URL = 'https://raw.githubusercontent.com/Elijahthomas5/red-panda-tracker/main/live-prices.json';
  const CACHE_KEY = 'rp_live_prices_cache';
  const CACHE_TTL = 20 * 60 * 1000; // 20 min

  async function loadLivePrices() {
    // Try cache first
    try {
      const cached = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null');
      if (cached && (Date.now() - cached.ts) < CACHE_TTL) {
        Object.assign(livePrices, cached.prices);
        const el = document.getElementById('last-update');
        if (el) el.textContent = 'Cached \u00b7 ' + (cached.updated || '');
        return true;
      }
    } catch(e) { localStorage.removeItem(CACHE_KEY); }

    try {
      const r = await fetch(LIVE_PRICES_URL + '?t=' + Date.now(), {
        signal: AbortSignal.timeout(8000)
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      const updated = data._updated || '';

      // Populate livePrices (global var in index.html)
      Object.entries(data).forEach(([k, v]) => {
        if (!k.startsWith('_') && typeof v === 'number') livePrices[k] = v;
      });

      // Cache it
      localStorage.setItem(CACHE_KEY, JSON.stringify({
        ts: Date.now(),
        prices: {...livePrices},
        updated
      }));

      // Update status display
      const lastUpdate = document.getElementById('last-update');
      if (lastUpdate) lastUpdate.textContent = updated;
      const pill = document.getElementById('schwab-pill');
      if (pill) pill.className = 'status-pill live';
      const txt = document.getElementById('schwab-text');
      if (txt) txt.textContent = 'Live (15min)';

      return true;
    } catch(e) {
      console.warn('[live-prices-loader] fetch failed:', e.message);
      return false;
    }
  }

  // Wait for RP_DATA and livePrices to exist (index.html loads them)
  function waitForApp(cb, retries = 20) {
    if (typeof livePrices !== 'undefined' && typeof renderTable === 'function') {
      cb();
    } else if (retries > 0) {
      setTimeout(() => waitForApp(cb, retries - 1), 200);
    }
  }

  waitForApp(async () => {
    const ok = await loadLivePrices();
    if (ok) {
      renderTable();
      updateStats();
    }
    // Refresh every 20 minutes
    setInterval(async () => {
      localStorage.removeItem(CACHE_KEY); // force fresh fetch
      const ok = await loadLivePrices();
      if (ok) { renderTable(); updateStats(); }
    }, 20 * 60 * 1000);
  });
})();
