#!/usr/bin/env python3
"""
Red Panda Zone Watcher — Kajabi Scraper
Logs into Red Panda Academy on Kajabi, reads Ian Dunlap's Stock Club
monthly price post, parses LTB / Swing / Entry zones for each ticker,
and updates red-panda-zones.json.

Runs via GitHub Actions (.github/workflows/rp-watcher.yml).
Requires GitHub secrets:
RP_COOKIES — JSON array of Kajabi session cookies (primary auth method)
RP_EMAIL — fallback if no cookies
RP_PASSWORD — fallback if no cookies
GH_PAT — Personal Access Token with repo scope (for auto-refreshing cookies)
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
RP_COOKIES  = os.environ.get("RP_COOKIES", "")   # JSON array of cookie dicts
GH_PAT      = os.environ.get("GH_PAT", "")        # PAT for refreshing the secret

KAJABI_LOGIN    = "https://redpandaacademy.mykajabi.com/login"
KAJABI_HOME     = "https://redpandaacademy.mykajabi.com"
CATEGORY_URL    = "https://redpandaacademy.mykajabi.com/products/red-panda-academy-stock-club-monthly/categories/2149257404"

PRICES_POST_KEYWORDS = ["price", "zone", "monthly", "ltb", "load the boat", "stock club"]

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

# ─── COOKIE REFRESH ──────────────────────────────────────────────────────────
def refresh_github_secret(fresh_cookies: list):
    """Push fresh cookies back to the RP_COOKIES GitHub secret so every
    subsequent run gets a renewed session — no human intervention needed."""
    import base64

    if not GH_PAT:
        print("  ⚠  GH_PAT not set — skipping cookie auto-refresh")
        return

    gh_repo = os.environ.get("GITHUB_REPOSITORY", "Elijahthomas5/red-panda-tracker")

    try:
        import requests
        from nacl import encoding, public
    except ImportError as e:
        print(f"  ⚠  Missing library for cookie refresh ({e}) — skipping")
        return

    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. Fetch the repo's Actions public key (needed to encrypt secrets)
    key_resp = requests.get(
        f"https://api.github.com/repos/{gh_repo}/actions/secrets/public-key",
        headers=headers,
        timeout=15,
    )
    key_resp.raise_for_status()
    key_data = key_resp.json()

    # 2. Encrypt the cookie JSON with libsodium
    pub_key = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
    box      = public.SealedBox(pub_key)
    encrypted = base64.b64encode(
        box.encrypt(json.dumps(fresh_cookies).encode())
    ).decode()

    # 3. Update the secret
    put_resp = requests.put(
        f"https://api.github.com/repos/{gh_repo}/actions/secrets/RP_COOKIES",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
        timeout=15,
    )
    put_resp.raise_for_status()
    print(f"  ✓ RP_COOKIES secret refreshed ({len(fresh_cookies)} cookies, "
          f"next expiry extended)")

# ─── PRICE PARSER ────────────────────────────────────────────────────────────
def get_known_tickers():
    z = load_zones()
    return set(z.get("tickers", {}).keys())

PRICE_RE = re.compile(r'\$([\d,]+\.?\d*)')

def parse_prices(raw_text: str, known_tickers: set) -> dict:
    updates = {}
    lines = [l.strip() for l in raw_text.replace("\r", "\n").split("\n")]
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
            print(f"  {current_ticker}: {updates[current_ticker]}")
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

        try_labelled(LTB_ALIASES, "ltb")
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
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("⚠ Playwright not installed.")
        sys.exit(1)

    known_tickers = get_known_tickers()
    print(f"\n🐼 Red Panda Watcher — {date.today()}")
    print(f"  Tracking {len(known_tickers)} tickers\n")

    page_text  = ""
    page_label = ""
    fresh_cookies = []

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

        # ── Auth: cookies first, password fallback ────────────────────────
        if RP_COOKIES:
            print("  Loading session cookies…")
            try:
                cookies = json.loads(RP_COOKIES)
                # Normalize sameSite values — Cookie-Editor exports "no_restriction"
                # but Playwright only accepts Strict, Lax, or None
                samesite_map = {"no_restriction": "None", "unspecified": "Lax"}
                for c in cookies:
                    ss = c.get("sameSite", "Lax")
                    c["sameSite"] = samesite_map.get(ss, ss)
                    # Remove keys Playwright doesn't accept
                    for k in ["hostOnly", "session", "storeId"]:
                        c.pop(k, None)
                    # expirationDate → expires
                    if "expirationDate" in c:
                        c["expires"] = c.pop("expirationDate")
                ctx.add_cookies(cookies)
                print(f"  ✓ Loaded {len(cookies)} cookies")

                # Verify the session is still valid
                page.goto(KAJABI_HOME, wait_until="load", timeout=30000)
                page.wait_for_timeout(2000)
                if "login" in page.url or "sign_in" in page.url:
                    print("  ⚠  Cookies expired — please refresh RP_COOKIES secret")
                    browser.close()
                    sys.exit(1)
                print(f"  ✓ Session valid → {page.url}")
            except Exception as e:
                print(f"  ⚠  Cookie load failed: {e}")
                browser.close()
                sys.exit(1)

        elif RP_EMAIL and RP_PASSWORD:
            print(f"  Logging in as {RP_EMAIL} (password mode)…")
            print("  ⚠  WARNING: Kajabi login page is protected by Cloudflare.")
            print("  If this fails, set RP_COOKIES secret instead.")
            try:
                page.goto(KAJABI_LOGIN, wait_until="load", timeout=60000)
                page.wait_for_timeout(5000)

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
                    print("  ⚠  Login form blocked by Cloudflare bot protection.")
                    print("  Add RP_COOKIES secret to bypass this.")
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
                    print("  ⚠  Login failed — check RP_EMAIL / RP_PASSWORD")
                    browser.close()
                    sys.exit(1)
                print(f"  ✓ Logged in → {page.url}")
            except PWTimeout:
                print(f"  ⚠  Login timed out. Current URL: {page.url}")
                browser.close()
                sys.exit(1)

        else:
            print("⚠ No auth configured.")
            print("  Add RP_COOKIES secret (recommended) or RP_EMAIL + RP_PASSWORD.")
            sys.exit(0)

        # ── Find latest prices post from category page ────────────────────
        print(f"  Loading category page…")
        try:
            page.goto(CATEGORY_URL, wait_until="load", timeout=60000)
            page.wait_for_timeout(4000)

            post_links = page.query_selector_all("a[href*='/posts/']")
            print(f"  Found {len(post_links)} post links")

            prices_url = None
            for link in post_links:
                try:
                    text = link.inner_text().strip().lower()
                    href = link.get_attribute("href") or ""
                    if any(kw in text for kw in PRICES_POST_KEYWORDS):
                        if "http" not in href:
                            href = "https://redpandaacademy.mykajabi.com" + href
                        prices_url = href
                        print(f"  ✓ Found prices post: {link.inner_text().strip()}")
                        print(f"    → {prices_url}")
                        break
                except:
                    pass

            if not prices_url and post_links:
                href = post_links[0].get_attribute("href") or ""
                if "http" not in href:
                    href = "https://redpandaacademy.mykajabi.com" + href
                prices_url = href
                print(f"  ⚠  No keyword match — using first post: {prices_url}")

            if not prices_url:
                print(f"  ⚠  No post links found. URL: {page.url}")
                browser.close()
                sys.exit(1)

        except PWTimeout:
            print("  ⚠  Category page timed out")
            browser.close()
            sys.exit(1)

        # ── Navigate to prices post ───────────────────────────────────────
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

            for sel in [".fr-view", ".post-body", ".post-content", ".lesson-content",
                        "article .content", "[data-post-content]", ".prose", "article", "main"]:
                el = page.query_selector(sel)
                if el:
                    page_text = el.inner_text()
                    print(f"  ✓ Got content via {sel} ({len(page_text)} chars)")
                    break

            if not page_text:
                page_text = page.inner_text("body")
                print(f"  ⚠  Using full body ({len(page_text)} chars)")

        except PWTimeout:
            print("  ⚠  Price post timed out")
            browser.close()
            sys.exit(1)

        # ── Capture fresh cookies before closing ─────────────────────────
        try:
            fresh_cookies = ctx.cookies()
            print(f"  ✓ Captured {len(fresh_cookies)} fresh cookies for refresh")
        except Exception as e:
            print(f"  ⚠  Could not capture cookies: {e}")

        browser.close()

    if not page_text.strip():
        print("  ⚠  No text extracted — aborting.")
        sys.exit(1)

    print(f"\n  Parsing prices…")
    updates = parse_prices(page_text, known_tickers)

    if not updates:
        print("  ⚠  No ticker prices found. Format may have changed.")
        print("  First 500 chars:")
        print("  " + page_text[:500].replace("\n", "\n  "))
        sys.exit(0)

    print(f"\n  Parsed {len(updates)} ticker(s).")
    zones = load_zones()
    zones, changed = merge_updates(zones, updates, page_label)

    if changed > 0:
        save_zones(zones)
        print(f"\n✅ red-panda-zones.json updated ({changed} change(s)).")
    else:
        print("\n✓ No price changes — already up to date.")

    # ── Auto-refresh RP_COOKIES secret so next run still works ───────────
    if fresh_cookies and GH_PAT:
        print("\n  Refreshing RP_COOKIES secret…")
        try:
            refresh_github_secret(fresh_cookies)
        except Exception as e:
            print(f"  ⚠  Cookie refresh failed: {e}")
            print("  Scrape succeeded — cookies will expire in ~24h.")

    sys.exit(0)

if __name__ == "__main__":
    run()
