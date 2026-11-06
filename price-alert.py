#!/usr/bin/env python3
"""
Red Panda Price Alert â Zone Checker
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

ZONES_PATH       = "red-panda-zones.json"
STATE_PATH       = "price-alert-state.json"
LIVE_PRICES_PATH = "live-prices.json"
NTFY_TOPIC       = os.environ.get("NTFY_TOPIC", "")
COOLDOWN_HOURS   = 6   # don't re-alert same ticker+zone within this many hours

ZONE_KEYS   = ["ltb", "swing", "entry"]
ZONE_NAMES  = {"ltb": "Load the Boat ð¢", "swing": "Swing Trade ð", "entry": "Quick Entry â¡"}
ZONE_PRIORITY = {"ltb": "high", "swing": "default", "entry": "default"}

# âââ STATE âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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

def save_live_prices(prices: dict, now_utc):
    """Write current prices to live-prices.json for the website (no API key needed)."""
    # Determine ET offset (EDT AprâOct, EST NovâMar)
    month = now_utc.month
    et_offset = timedelta(hours=-4) if 4 <= month <= 10 else timedelta(hours=-5)
    now_et = now_utc.astimezone(timezone(et_offset))
    data = {
        "_updated":     now_et.strftime("%Y-%m-%d %I:%M %p ET"),
        "_updated_utc": now_utc.isoformat(),
    }
    data.update(prices)
    with open(LIVE_PRICES_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  â Wrote live-prices.json ({len(prices)} tickers)")

def on_cooldown(state, key):
    if key not in state:
        return False
    last = datetime.fromisoformat(state[key])
    return datetime.now(timezone.utc) - last < timedelta(hours=COOLDOWN_HOURS)

# âââ NOTIFY ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def notify_bundled(alerts: list):
    """Send one notification listing all triggered stocks.
    alerts = [{"ticker": str, "name": str, "zone": str, "price": float, "zone_price": float, "pct": float}]
    """
    if not alerts:
        return

    # Determine highest priority in the batch
    priorities = [ZONE_PRIORITY[a["zone"]] for a in alerts]
    priority = "high" if "high" in priorities else "default"

    title = f"ð¼ Red Panda Alert â {len(alerts)} stock{'s' if len(alerts) > 1 else ''} in zone"

    lines = []
    for a in sorted(alerts, key=lambda x: ZONE_KEYS.index(x["zone"])):
        pct_str = f"{a['pct']:+.1f}%" if abs(a['pct']) > 0.05 else "right at zone"
        lines.append(f"{a['ticker']} ${a['price']:,.2f} â {ZONE_NAMES[a['zone']]} (target ${a['zone_price']:,.2f}, {pct_str})")

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
                "priority": 4 if priority == "high" else 3,
                "tags":     ["moneybag"],
                "click":    "https://elijahthomas5.github.io/red-panda-tracker/",
            },
            timeout=10,
        ).raise_for_status()
        print(f"  â Sent bundled alert: {title}")
    except Exception as e:
        print(f"  â   ntfy failed: {e}")

# âââ PRICES ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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

# âââ MAIN ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def run():
    try:
        import yfinance  # noqa
        import requests  # noqa
    except ImportError as e:
        print(f"â  Missing library: {e}")
        sys.exit(1)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\nð¼ Red Panda Price Alert â {now_str}")

    zones_data   = load_zones()
    tickers_data = zones_data.get("tickers", {})
    state        = load_state()

    valid = {
        t: d for t, d in tickers_data.items()
        if any(p is not None for p in (d.get("prices") or [None, None, None]))
    }

    if not valid:
        print("  No tickers with zone prices â nothing to check.")
        sys.exit(0)

    print(f"  Checking {len(valid)} tickersâ¦")
    ticker_list = list(valid.keys())

    try:
        current_prices = fetch_prices(ticker_list)
    except Exception as e:
        print(f"  â   Price fetch failed: {e}")
        sys.exit(1)

    print(f"  Got prices for {len(current_prices)}/{len(ticker_list)} tickers")
    now_utc = datetime.now(timezone.utc)
    save_live_prices(current_prices, now_utc)

    alerts_to_fire = []
    state_changed  = False

    for ticker, data in valid.items():
        price = current_prices.get(ticker)
        if price is None:
            continue

        prices = data.get("prices") or [None, None, None]
        name   = data.get("name", ticker)

        for zone_key, zone_price in zip(ZONE_KEYS, prices):
            if zone_price is None:
                continue

            in_zone = price <= zone_price * 1.01   # at or within 1% below zone
            if not in_zone:
                continue

            state_key = f"{ticker}_{zone_key}"

            if on_cooldown(state, state_key):
                print(f"  â³ {ticker} {zone_key} â in zone but on cooldown, skipping")
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
            print(f"    {a['ticker']} ${a['price']:,.2f} â {ZONE_NAMES[a['zone']]}")
        notify_bundled(alerts_to_fire)
    else:
        print("  No stocks in zone (or all on cooldown).")

    if state_changed:
        save_state(state)

    sys.exit(0)

if __name__ == "__main__":
    run()
