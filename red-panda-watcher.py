#!/usr/bin/env python3
"""
Red Panda Zone Watcher — Kajabi Scraper
Logs into Red Panda Academy on Kajabi, reads Ian Dunlap's Stock Club
monthly price post, parses LTB / Swing / Entry zones for each ticker,
and updates red-panda-zones.json.

Runs via GitHub Actions (.github/workflows/rp-watcher.yml).
Requires: RP_EMAIL + RP_PASSWORD as GitHub secrets.
          pip install playwright playwright-stealth && playwright install chromium
"""

import json
import os
import re
import sys
from datetime import date

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ZONES_PATH  = os.path.join(BASE_DIR, 'red-panda-zones.json')

RP_EMAIL    = os.environ.get("RP_EMAIL", "")
RP_PASSWORD = os.environ.get("RP_PASSWORD", "")

KAJABI_LOGIN    = "https://redpandaacademy.mykajabi.com/login"
CATEGORY_URL    = "https://redpandaacademy.mykajabi.com/products/red-panda-academy-stock-club-monthly/categories/2149257404"

# Keywords that identify the monthly prices post (case-insensitive)
PRICES_POST_KEYWORDS = ["price", "zone", "monthly", "ltb", "load the boat", "stock club"]

# Label aliases the parser recognizes
LTB_ALIASES   = {"ltb", "load the boat", "load", "l.t.b", "boat", "ltb zone"}
SWING_ALIASES = {"swing", "swing trade", "swing zone", "s.t", "swing entry"}
ENTRY_ALIASES = {"entry", "quick entry", "qe", "quick", "q.e", "entry zone"}

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

PRICE_RE   = re.compile(r'\$?([\d,]+\.?\d*)')
TICKER_RE  = re.compile(r'\b([A-Z]{1,5})\b')


def parse_prices(raw_text: str, known_tickers: set) -> dict:
    updates = {}
    lines   = [l.strip() for l in raw_text.replace("\r", "\n").split("\n")]
    current_ticker = None
    buf = {"ltb": None, "swing": None, "entry": None, "unlabelled": []}

    def flush():
        nonlocal buf
        if not current_ticker:
            return
        for slot in ["ltb", "swing", "entry"]:
            if buf[slot] is None and buf["unlabelled"]:
                buf[slot] = buf["unlabelled"].pop(0)
        if any(buf[k] is not None for k in ["ltb", "swing", "entry"]):
            updates[current_ticker] = [buf["ltb"], buf["swing"], buf["entry"]]
            print(f"    {current_ticker}: {updates[current_ticker]}")
        buf = {"ltb": None, "swing": None, "entry": None, "unlabelled": []}

    for line in lines:
        if not line:
            continue
        line_lower = line.lower()
        first_word = line.split()[0].strip(":-/$.,")
        if first_word in known_tickers:
            flush()
            current_ticker = first_word
            remainder = line[len(first_word):].strip(":-/ \t")
            line = remainder
            line_lower = line.lower()
        if current_ticker is None:
            continue

        def try_labelled(aliases, slot):
            for alias in aliases:
                pat = rf'(?i)\b{re.escape(alias)}\b[\s:–-]*\$?([\d,]+\.?\d*)'
                m = re.search(pat, line)
                if m:
                    try:
                        val = float(m.group(1).replace(",", ""))
                        buf[slot] = val
                    except ValueError:
                        pass

        try_labelled(LTB_ALIASES,   "ltb")
        try_labelled(SWING_ALIASES, "swing")
        try_labelled(ENTRY_ALIASES, "entry")

        has_label = any(alias in line_lower
                        for aliases in [LTB_ALIASES, SWING_ALIASES, ENTRY_ALIASES]
                        for alias in aliases)
        prices_on_line = []
        for m in PRICE_RE.finditer(line):
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    prices_on_line.append(val)
            except ValueError:
                pass
        if not has_label:
            buf["unlabelled"].extend(prices_on_line)

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
    if not RP_EMAIL or not RP_PASSWORD:
        print("⚠  RP_EMAIL / RP_PASSWORD not set.")
        print("   GitHub → Settings → Secrets → add RP_EMAIL and RP_PASSWORD.")
        sys.exit(0)

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("⚠  Playwright not installed.")
        sys.exit(1)

    try:
        from playwright_stealth import stealth_sync
        print("  ✓ playwright-stealth loaded")
    except ImportError:
        print("  ⚠  playwright-stealth not found — running without stealth")
        stealth_sync = None

    known_tickers = get_known_tickers()
    print(f"\n🐼 Red Panda Watcher — {date.today()}")
    print(f"   Tracking {len(known_tickers)} tickers\n")

    page_text  = ""
    page_label = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
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
            ignore_https_errors=True,
        )
        page = ctx.new_page()

        # Apply stealth patches to avoid Cloudflare bot detection
        if stealth_sync:
            stealth_sync(page)
            print("  ✓ Stealth patches applied")

        # ── Log into Kajabi ───────────────────────────────────────────────────
        print(f"  Logging in as {RP_EMAIL}…")
        try:
            page.goto(KAJABI_LOGIN, wait_until="load", timeout=60000)
            # Extra wait — Cloudflare Turnstile needs time to pass
            page.wait_for_timeout(5000)
            print(f"  Login page loaded: {page.url}")

            email_el = (
                page.query_selector('input[type="email"]') or
                page.query_selector('input[name*="email"]') or
                page.query_selector('input[id*="email"]') or
                page.query_selector('input[placeholder*="email" i]') or
                page.query_selector('input[autocomplete="email"]')
            )
            pw_el = (
                page.query_selector('input[type="password"]') or
                page.query_selector('input[name*="password"]') or
                page.query_selector('input[id*="password"]') or
                page.query_selector('input[autocomplete="current-password"]')
            )

            if not email_el or not pw_el:
                # Cloudflare may still be loading — wait longer and retry
                print("  Form not ready, waiting for Cloudflare to clear…")
                page.wait_for_timeout(10000)
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
                print("  ⚠  Login form not found after waiting. Page text:")
                print("     " + page.inner_text("body")[:600].replace("\n", "\n     "))
                browser.close()
                sys.exit(1)

            email_el.fill(RP_EMAIL)
            pw_el.fill(RP_PASSWORD)
            pw_el.press("Enter")

            try:
                page.wait_for_url(lambda url: "login" not in url and "sign_in" not in url, timeout=20000)
            except PWTimeout:
                page.wait_for_timeout(5000)

            if "login" in page.url or "sign_in" in page.url:
                print("  ⚠  Login may have failed — check RP_EMAIL / RP_PASSWORD secrets")
                print(f"     Current URL: {page.url}")
                browser.close()
                sys.exit(1)

            print(f"  ✓ Logged in → {page.url}")

        except PWTimeout:
            print(f"  ⚠  Login timed out. Current URL: {page.url}")
            browser.close()
            sys.exit(1)

        # ── Find the latest prices post from the category page ───────────────
        print(f"  Loading category page to find latest prices post…")
        try:
            page.goto(CATEGORY_URL, wait_until="load", timeout=60000)
            page.wait_for_timeout(4000)

            post_links = page.query_selector_all("a[href*='/posts/']")
            print(f"  Found {len(post_links)} post links on category page")

            prices_url = None
            for link in post_links:
                try:
                    text = link.inner_text().strip().lower()
                    href = link.get_attribute("href") or ""
                    if any(kw in text for kw in PRICES_POST_KEYWORDS):
                        if "http" not in href:
                            href = "https://redpandaacademy.mykajabi.com" + href
                        prices_url = href
                        print(f"  ✓ Found prices post by keyword: {link.inner_text().strip()}")
                        print(f"    → {prices_url}")
                        break
                except:
                    pass

            if not prices_url and post_links:
                try:
                    href = post_links[0].get_attribute("href") or ""
                    if "http" not in href:
                        href = "https://redpandaacademy.mykajabi.com" + href
                    prices_url = href
                    print(f"  ⚠  No keyword match — using first post: {prices_url}")
                except:
                    pass

            if not prices_url:
                print("  ⚠  Could not find any post links on category page.")
                print(f"     Page URL: {page.url}")
                browser.close()
                sys.exit(1)

        except PWTimeout:
            print("  ⚠  Category page timed out")
            browser.close()
            sys.exit(1)

        # ── Navigate to the prices post ───────────────────────────────────────
        print(f"  Navigating to prices post…")
        try:
            page.goto(prices_url, wait_until="load", timeout=60000)
            page.wait_for_timeout(3000)

            try:
                heading = page.query_selector("h1, h2, .post-title, .lesson-title")
                if heading:
                    page_label = heading.inner_text().strip()
            except:
                pass

            content_selectors = [
                ".fr-view",
                ".post-body",
                ".post-content",
                ".lesson-content",
                "article .content",
                "[data-post-content]",
                ".prose",
                "article",
                "main",
            ]
            for sel in content_selectors:
                el = page.query_selector(sel)
                if el:
                    page_text = el.inner_text()
                    print(f"  ✓ Got content via selector: {sel} ({len(page_text)} chars)")
                    break

            if not page_text:
                page_text = page.inner_text("body")
                print(f"  ⚠  Using full body text ({len(page_text)} chars)")

        except PWTimeout:
            print("  ⚠  Price post page timed out")
            browser.close()
            sys.exit(1)

        browser.close()

    if not page_text.strip():
        print("  ⚠  No text extracted from page — aborting.")
        sys.exit(1)

    print(f"\n  Parsing prices from post text…")
    updates = parse_prices(page_text, known_tickers)

    if not updates:
        print("  ⚠  No ticker prices found in post. The format may have changed.")
        print("     First 500 chars of page text:")
        print("     " + page_text[:500].replace("\n", "\n     "))
        sys.exit(0)

    print(f"\n  Parsed {len(updates)} ticker(s) from post.")

    zones = load_zones()
    zones, changed = merge_updates(zones, updates, page_label)

    if changed > 0:
        save_zones(zones)
        print(f"\n✅ red-panda-zones.json updated ({changed} change(s)).")
    else:
        print("\n✓ No price changes — zones already up to date.")

    sys.exit(0)


if __name__ == "__main__":
    run()
