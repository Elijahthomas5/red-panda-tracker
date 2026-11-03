#!/usr/bin/env python3
"""
Red Panda Price Alert — Zone Checker
Fetches live stock prices and sends a single bundled push notification via ntfy.sh
when any stocks touch the LTB / Swing / Entry zone from Ian's post.

Runs via GitHub Actions every 15 min during market hours.
Requires GitHub secret: NTFY_TOPIC (your ntfy.sh topic name)
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ZONES_PATH  = "red-panda-zones.json"
STATE_PATH  = "price-alert-state.json"
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
COOLDOWN_HOURS = 6   # don't re-alert same ticker+zone within this many hours

ZONE_KEYS   = ["ltb", "swing", "entry"]
ZONE_NAMES  = {"ltb": "Load the Boat 🚢", "swing": "Swing Trade 📈", "entry": "Quick Entry ⚡"}
ZONE_PRIORITY = {"ltb": "high", "swing": "default", "entry": "default"}

# ─── STATE ───────────────────────────────────────────────────────────────────
def load_zones():
    with open(ZONES_PATH) as f:
        return json.load(f)

def load_state():
    if Path(STATE_PATH).exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def on_cooldown(state, key):
    if key not in state:
        return False
    last = datetime.fromisoformat(state[key])
    return datetime.now(timezone.utc) - last < timedelta(hours=COOLDOWN_HOURS)

# ─── NOTIFY ──────────────────────────────────────────────────────────────────
def notify_bundled(alerts: list):
    """Send one notification listing all triggered stocks."""
    if not alerts:
        return

    priorities = [ZONE_PRIORITY[a["zone"]] for a in alerts]
    priority = 4 if "high" in priorities else 3

    title = f"🐼 Red Panda Alert — {len(alerts)} stock{'s' if len(alerts) > 1 else ''} in zone"

    lines = []
    for a in sorted(alerts, key=lambda x: ZONE_KEYS.index(x["zone"])):
        pct_str = f"{a['pct']:+.1f}%" if abs(a['pct']) > 0.05 else "right at zone"
        lines.append(f"{a['ticker']} ${a['price']:,.2f} — {ZONE_NAMES[a['zone']]} (target ${a['zone_price']:,.2f}, {pct_str})")

    message = "\n".join(lines)

    if not NTFY_TOPIC:
        print(f"  [no NTFY_TOPIC set]\n  {title}\n  {message}")
        return

    try:
        import requests
        requests.post(
            "https://ntfy.sh",
            json={
                "topic":    NTFY_TOPIC,
                "title":    title,
                "message":  message,
                "priority": priority,
                "tags":     ["moneybag"],
                "click":    "https://elijahthomas5.github.io/red-panda-tracker/",
            },
            timeout=10,
        ).raise_for_status()
        print(f"  ✓ Sent bundled alert: {title}")
    except Exception as e:
        print(f"  ⚠  ntfy failed: {e}")

# ─── PRICES ──────────────────────────────────────────────────────────────────
def fetch_prices(tickers: list) -> dict:
    """Fetch latest intraday price for each ticker via yfinance."""
    import yfinance as yf

    prices = {}
    raw = yf.download(
        tickers,
        period="1d",
        interval="5m",
        auto_adjust=True,
        progress=False,
        group_by="ticker",
    )

    if len(tickers) == 1:
        t = tickers[0]
        try:
            series = raw["Close"].dropna()
            if not series.empty:
                prices[t] = round(float(series.iloc[-1]), 2)
        except Exception:
            pass
    else:
        for t in tickers:
            try:
                series = raw[t]["Close"].dropna()
                if not series.empty:
                    prices[t] = round(float(series.iloc[-1]), 2)
            except Exception:
                pass

    return prices

# ─── MAIN ────────────────────────────────────────────────────────────────────
def run():
    try:
        import yfinance  # noqa
        import requests  # noqa
    except ImportError as e:
        print(f"⚠ Missing library: {e}")
        sys.exit(1)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n🐼 Red Panda Price Alert — {now_str}")

    zones_data   = load_zones()
    tickers_data = zones_data.get("tickers", {})
    state        = load_state()

    valid = {
        t: d for t, d in tickers_data.items()
        if any(p is not None for p in (d.get("prices") or [None, None, None]))
    }

    if not valid:
        print("  No tickers with zone prices — nothing to check.")
        sys.exit(0)

    print(f"  Checking {len(valid)} tickers…")
    ticker_list = list(valid.keys())

    try:
        current_prices = fetch_prices(ticker_list)
    except Exception as e:
        print(f"  ⚠  Price fetch failed: {e}")
        sys.exit(1)

    print(f"  Got prices for {len(current_prices)}/{len(ticker_list)} tickers")

    alerts_to_fire = []
    state_changed  = False
    now_utc        = datetime.now(timezone.utc)

    for ticker, data in valid.items():
        price = current_prices.get(ticker)
        if price is None:
            continue

        prices = data.get("prices") or [None, None, None]
        name   = data.get("name", ticker)

        for zone_key, zone_price in zip(ZONE_KEYS, prices):
            if zone_price is None:
                continue

            in_zone = price <= zone_price * 1.01
            if not in_zone:
                continue

            state_key = f"{ticker}_{zone_key}"

            if on_cooldown(state, state_key):
                print(f"  ⏳ {ticker} {zone_key} — in zone but on cooldown, skipping")
                continue

            pct = ((price - zone_price) / zone_price) * 100
            alerts_to_fire.append({
                "ticker":     ticker,
                "name":       name,
                "zone":       zone_key,
                "price":      price,
                "zone_price": zone_price,
                "pct":        pct,
            })
            state[state_key] = now_utc.isoformat()
            state_changed = True

    if alerts_to_fire:
        print(f"\n  {len(alerts_to_fire)} alert(s) to send:")
        for a in alerts_to_fire:
            print(f"    {a['ticker']} ${a['price']:,.2f} → {ZONE_NAMES[a['zone']]}")
        notify_bundled(alerts_to_fire)
    else:
        print("  No stocks in zone (or all on cooldown).")

    if state_changed:
        save_state(state)

    sys.exit(0)

if __name__ == "__main__":
    run()
