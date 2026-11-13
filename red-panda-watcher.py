 #!/usr/bin/env python3
"""
Red Panda Zone Watcher — Kajabi Scraper
Logs into Red Panda Academy on Kajabi, reads Ian Dunlap's Stock Club
monthly price post, parses LTB / Swing / Entry zones for each ticker,
and updates red-panda-zones.json.

Runs via GitHub Actions (.github/workflows/rp-watcher.yml).
Requires GitHub secrets:
RP_EMAIL    — Kajabi account email
RP_PASSWORD — Kajabi account password
"""

import json
import os
import re
import sys
from datetime import date

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ZONES_PATH = os.path.join(BASE_DIR, 'red-panda-zones.json')

RP_EMAIL    = os.environ.get("RP_EMAIL", "")
RP_PASSWORD = os.environ.get("RP_PASSWORD", "")

KAJABI_LOGIN = "https://redpandaacademy.mykajabi.com/login"
KAJABI_HOME  = "https://redpandaacademy.mykajabi.com"
CATEGORY_URL = "https://redpandaacademy.mykajabi.com/products/red-panda-academy-stock-club-monthly/categories/2149257404"

PRICES_POST_KEYWORDS = ["price", "zone", "monthly", "ltb", "load the boat", "stock club"]

# ─── LOAD / SAVE ZONES ───────────────────────────────────────────────────────
def load_zones():
    if not os.path.exists(ZONES_PATH):
        return {"_updated": "", "_label": "", "_note": "", "tickers": {}}
    with open(ZONES_PATH) as f:
        return json.load(f)

def save_zones(data):
    with open(ZONES_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  ✅ Saved {ZONES_PATH}")

# ─── PRICE PARSER ────────────────────────────────────────────────────────────
def get_known_tickers():
    z = load_zones()
    return set(z.get("tickers", {}).keys())

PRICE_RE = re.compile(r'\$([\d,]+\.?\d*)')

def is_valid_price(val: float) -> bool:
    """Filter out list-number artifacts ($1, $2) and year-shaped values (2026)."""
    if val < 5:
        return False
    if 2020 <= val <= 2029 and val == int(val):
        return False
    return True

def parse_prices(raw_text: str, known_tickers: set) -> dict:
    """
    Positional parser: for each ticker, grab the next 1–3 dollar prices in order.
    Position 1 = Load the Boat, 2 = Swing Trade, 3 = Quick Entry.
    Matches Ian's post format: TICKER \n $ltb \n $swing \n $entry \n NEXT_TICKER
    """
    updates = {}
    lines = [l.strip() for l in raw_text.replace("\r", "\n").split("\n")]
    current_ticker = None
    prices_buf = []

    def flush():
        nonlocal current_ticker, prices_buf
        if not current_ticker or not prices_buf:
            current_ticker = None
            prices_buf = []
            return
        while len(prices_buf) < 3:
            prices_buf.append(None)
        updates[current_ticker] = prices_buf[:3]
        print(f"  {current_ticker}: {updates[current_ticker]}")
        current_ticker = None
        prices_buf = []

    for line in lines:
        if not line:
            continue
        first_word = line.split()[0].strip(":-/$.,")
        if first_word in known_tickers:
            flush()
            current_ticker = first_word
            # Prices on the same line as the ticker (e.g. "NVDA $122 $195 $210")
            remainder = line[len(first_word):]
            for m in PRICE_RE.finditer(remainder):
                try:
                    val = float(m.group(1).replace(",", ""))
                    if is_valid_price(val) and len(prices_buf) < 3:
                        prices_buf.append(val)
                except ValueError:
                    pass
        elif current_ticker is not None:
            if len(prices_buf) >= 3:
                continue  # already have all three — wait for next ticker
            for m in PRICE_RE.finditer(line):
                try:
                    val = float(m.group(1).replace(",", ""))
                    if is_valid_price(val) and len(prices_buf) < 3:
                        prices_buf.append(val)
                except ValueError:
                    pass

    flush()
    return updates

# ─── MERGE UPDATES INTO ZONES ────────────────────────────────────────────────
def merge_updates(zones: dict, updates: dict, page_label: str) -> dict:
    changed = 0
    for ticker, prices in updates.items():
        existing = zones["tickers"].get(ticker)
        if existing is None:
            zones["tickers"][ticker] = {"name": ticker, "prices": prices}
            print(f"  ➕ New ticker added: {ticker}")
            changed += 1
        else:
            old = existing.get("prices", [None, None, None])
            if old != prices:
                existing["prices"] = prices
                print(f"  🔄 Updated {ticker}: {old} → {prices}")
                changed += 1
    today = date.today()
    zones["_updated"] = today.strftime("%Y-%m-%d")
    zones["_label"]   = page_label or f"Ian Dunlap · Stock Club · {today.strftime('%b %d, %Y')}"
    print(f"\n  {changed} ticker(s) changed.")
    return zones, changed

# ─── MAIN SCRAPER ────────────────────────────────────────────────────────────
def run():
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        from playwright_stealth import stealth_sync
    except ImportError:
        print("⚠ playwright and playwright-stealth are required.")
        sys.exit(1)

    if not RP_EMAIL or not RP_PASSWORD:
        print("⚠ RP_EMAIL and RP_PASSWORD secrets are required.")
        sys.exit(1)

    known_tickers = get_known_tickers()
    print(f"\n🐼 Red Panda Watcher — {date.today()}")
    print(f"   Tracking {len(known_tickers)} tickers\n")

    page_text  = ""
    page_label = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = ctx.new_page()
        stealth_sync(page)   # mask headless signals — bypasses Cloudflare

        # ── Login ──────────────────────────────────────────────────────────
        print(f"   Logging in as {RP_EMAIL}…")
        try:
            page.goto(KAJABI_LOGIN, wait_until="load", timeout=60000)
            page.wait_for_timeout(4000)

            email_el = (
                page.query_selector('input[type="email"]') or
                page.query_selector('input[name*="email"]') or
                page.query_selector('input[id*="email"]')
            )
            pw_el = (
                page.query_selector('input[type="password"]') or
                page.query_selector('input[name*="password"]')
            )

            if not email_el or not pw_el:
                print("   ⚠ Login form not found — Cloudflare may still be blocking.")
                browser.close()
                sys.exit(1)

            email_el.fill(RP_EMAIL)
            pw_el.fill(RP_PASSWORD)
            pw_el.press("Enter")

            try:
                page.wait_for_url(lambda url: "login" not in url, timeout=20000)
            except PWTimeout:
                page.wait_for_timeout(5000)

            if "login" in page.url:
                print("   ⚠ Login failed — check RP_EMAIL / RP_PASSWORD secrets.")
                browser.close()
                sys.exit(1)
            print(f"   ✓ Logged in → {page.url}")

        except PWTimeout:
            print(f"   ⚠ Login timed out. URL: {page.url}")
            browser.close()
            sys.exit(1)

        # ── Find latest prices post ────────────────────────────────────────
        print("   Loading category page…")
        try:
            page.goto(CATEGORY_URL, wait_until="load", timeout=60000)
            page.wait_for_timeout(4000)

            post_links = page.query_selector_all("a[href*='/posts/']")
            print(f"   Found {len(post_links)} post links")

            prices_url = None
            for link in post_links:
                try:
                    text = link.inner_text().strip().lower()
                    href = link.get_attribute("href") or ""
                    if any(kw in text for kw in PRICES_POST_KEYWORDS):
                        if "http" not in href:
                            href = "https://redpandaacademy.mykajabi.com" + href
                        prices_url = href
                        print(f"   ✓ Found prices post: {link.inner_text().strip()}")
                        break
                except:
                    pass

            if not prices_url and post_links:
                href = post_links[0].get_attribute("href") or ""
                if "http" not in href:
                    href = "https://redpandaacademy.mykajabi.com" + href
                prices_url = href
                print(f"   ⚠ No keyword match — using first post: {prices_url}")

            if not prices_url:
                print(f"   ⚠ No post links found. URL: {page.url}")
                browser.close()
                sys.exit(1)

        except PWTimeout:
            print("   ⚠ Category page timed out")
            browser.close()
            sys.exit(1)

        # ── Read prices post ───────────────────────────────────────────────
        print("   Navigating to prices post…")
        try:
            page.goto(prices_url, wait_until="load", timeout=60000)
            page.wait_for_timeout(3000)

            try:
                heading = page.query_selector("h1, h2, .post-title, .lesson-title")
                if heading:
                    page_label = heading.inner_text().strip()
            except:
                pass

            for sel in [".fr-view", ".post-body", ".post-content", ".lesson-content",
                        "article .content", "[data-post-content]", ".prose", "article", "main"]:
                el = page.query_selector(sel)
                if el:
                    page_text = el.inner_text()
                    print(f"   ✓ Got content via {sel} ({len(page_text)} chars)")
                    break

            if not page_text:
                page_text = page.inner_text("body")
                print(f"   ⚠ Using full body ({len(page_text)} chars)")

        except PWTimeout:
            print("   ⚠ Price post timed out")
            browser.close()
            sys.exit(1)

        browser.close()

    if not page_text.strip():
        print("   ⚠ No text extracted — aborting.")
        sys.exit(1)

    print("\n   Parsing prices…")
    updates = parse_prices(page_text, known_tickers)

    if not updates:
        print("   ⚠ No ticker prices found. Format may have changed.")
        print("   First 500 chars of page text:")
        print("   " + page_text[:500].replace("\n", "\n   "))
        sys.exit(0)

    print(f"\n   Parsed {len(updates)} ticker(s).")
    zones = load_zones()
    zones, changed = merge_updates(zones, updates, page_label)

    if changed > 0:
        save_zones(zones)
        print(f"\n✅ red-panda-zones.json updated ({changed} change(s)).")
    else:
        print("\n✓ No price changes — already up to date.")

    sys.exit(0)

if __name__ == "__main__":
    run()
