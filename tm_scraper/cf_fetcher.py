#!/usr/bin/env python3
"""
workers/cf_fetcher.py
──────────────────────────────────────────────────────────────────────────────
Pro-level Cloudflare bypass fetcher — Playwright + Webshare Premium Proxies.

Strategy (tried in order):
  1. Playwright + playwright-stealth  →  executes the real CF JS challenge
     in a headless Chromium browser. This is the ONLY method that reliably
     works from GitHub Actions datacenter IPs because it runs actual JS.
  2. curl_cffi Chrome impersonation   →  fast, works locally / non-CF sites.
  3. Webshare Rotating Proxy          →  last resort if both above fail.

GitHub Actions requirements (add to your workflow YAML):
  - name: Install system deps for Playwright
    run: |
      pip install playwright playwright-stealth
      playwright install chromium --with-deps

Usage:
  from workers.cf_fetcher import fetch_html
  html = fetch_html("https://yallashoot.soccer/competition/...")
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Optional

logger = logging.getLogger("cf_fetcher")

# ── Config ────────────────────────────────────────────────────────────────────

_TIMEOUT_MS  = 30_000          # Playwright page timeout  (ms)
_WAIT_MS     = 4_000           # Extra wait after page load for CF to resolve (ms)
_HTTP_TIMEOUT = 20             # curl_cffi / urllib timeout (seconds)
_MAX_RETRIES  = 3              # retries per method

# Randomise Chrome version so each run looks like a slightly different browser
_CHROME_VERSIONS = [
    "chrome120", "chrome119", "chrome118",
    "chrome116", "chrome110", "chrome107",
]


# ── Method 1: Playwright stealth ──────────────────────────────────────────────

def _fetch_with_playwright(url: str) -> Optional[str]:
    """
    Launch a real Chromium browser, solve the CF JS challenge, return HTML.
    Requires:  pip install playwright playwright-stealth
               playwright install chromium --with-deps
    """
    try:
        from playwright.sync_api import sync_playwright
        try:
            from playwright_stealth import stealth_sync
            _has_stealth = True
        except ImportError:
            _has_stealth = False
            logger.debug("playwright-stealth not installed — running without stealth patch")

        with sync_playwright() as p:
            # NOTE: headless=False used to be tried on CI on the assumption
            # that Xvfb was providing a virtual display there. It isn't (no
            # workflow step starts Xvfb), so headed launches on GitHub
            # Actions always crashed with "Missing X server or $DISPLAY"
            # before even reaching the page. Since this method is already
            # last-resort (see fetch_html()'s ordering) and has a 0%
            # documented success rate on CI anyway, always run headless.
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                ],
            )

            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                # Realistic headers
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                },
            )

            page = ctx.new_page()

            if _has_stealth:
                stealth_sync(page)          # patches navigator.webdriver etc.

            # Navigate and wait for CF to finish its challenge
            resp = page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)

            if resp and resp.status == 403:
                logger.warning("[Playwright] Got 403 — waiting for CF challenge resolution...")
                # CF challenge pages redirect automatically; wait for network idle
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

            # Extra settle time for JS-rendered content
            page.wait_for_timeout(_WAIT_MS)

            # Verify we're past the challenge (CF challenge page title = "Just a moment...")
            title = page.title()
            if "just a moment" in title.lower():
                logger.warning("[Playwright] CF challenge NOT resolved (title: %r)", title)
                browser.close()
                return None

            html = page.content()
            browser.close()

            logger.info("[Playwright] ✓ Fetched %d chars from %s", len(html), url)
            return html

    except ImportError:
        logger.debug("[Playwright] Not installed — skipping")
        return None
    except Exception as exc:
        logger.warning("[Playwright] Failed: %s", exc)
        return None


# ── Method 2: curl_cffi TLS impersonation ────────────────────────────────────

def _fetch_with_curl_cffi(url: str, proxy: Optional[str] = None) -> Optional[str]:
    """
    Fast TLS-fingerprint spoof via curl_cffi.
    Works against sites using TLS-based bot detection (not CF JS challenge).
    Still useful as a fast path locally or on non-CF-challenged pages.
    """
    try:
        from curl_cffi import requests as cffi_requests

        impersonate = random.choice(_CHROME_VERSIONS)
        session = cffi_requests.Session(impersonate=impersonate)

        proxies = {"http": proxy, "https": proxy} if proxy else None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = session.get(
                    url,
                    timeout=_HTTP_TIMEOUT,
                    proxies=proxies,
                    headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.google.com/",
                        "DNT": "1",
                    },
                )

                if resp.status_code == 200:
                    # Reject CF challenge HTML
                    if "just a moment" in resp.text[:500].lower():
                        logger.debug("[curl_cffi] Got CF challenge page — not useful")
                        return None
                    logger.info("[curl_cffi] ✓ %s (attempt %d)", url, attempt)
                    return resp.text

                logger.debug("[curl_cffi] HTTP %d on attempt %d", resp.status_code, attempt)
                time.sleep(5 * attempt)

            except Exception as e:
                logger.debug("[curl_cffi] Error attempt %d: %s", attempt, e)
                time.sleep(5 * attempt)

        return None

    except ImportError:
        logger.debug("[curl_cffi] Not installed — skipping")
        return None


# ── Method 3: Webshare Rotating Endpoint ─────────────────────────────────────

def _fetch_with_webshare_proxy(url: str) -> Optional[str]:
    """
    Try curl_cffi through the Webshare Rotating Proxy Endpoint.
    Webshare handles the IP rotation automatically per request.
    """
    proxy_url = os.environ.get("WEBSHARE_PROXY_URL")

    if not proxy_url:
        logger.warning("[Webshare] WEBSHARE_PROXY_URL env var is not set. Skipping proxy fallback.")
        return None

    logger.info("[Webshare] Trying request via rotating proxy endpoint...")

    # We retry a few times because the IP changes automatically on every request
    for attempt in range(1, 4):
        result = _fetch_with_curl_cffi(url, proxy=proxy_url)
        if result:
            logger.info("[Webshare] ✓ Success via rotating proxy (Attempt %d)", attempt)
            return result
            
        logger.debug("[Webshare] Rotating proxy attempt %d failed, retrying...", attempt)
        time.sleep(2)

    logger.warning("[Webshare] All proxy attempts failed for %s", url)
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_html(url: str, retries: int = 2) -> Optional[str]:
    """
    Fetch HTML from a Cloudflare-protected page using a cascade of methods.

    Order (revised — see NOTE below):
      1. curl_cffi             (fast, ~1-3s, works locally and is usually
                                 what actually succeeds via the proxy too)
      2. curl_cffi + Webshare  (the one that reliably bypasses CF in CI)
      3. Playwright + stealth  (slow, ~20-90s; skipped on CI by default)

    NOTE on ordering: Playwright used to be tried FIRST on every CI run.
    In production it has never once solved this site's Cloudflare JS
    challenge from GitHub Actions' datacenter IPs (100% failure rate
    across every run we've logged) — it just burns 2 attempts x ~20-45s
    each (~35-90s) per match before falling through to Webshare, which is
    the method that actually works. For a 78-match run that's 30-60+
    wasted minutes for nothing. Webshare is now tried first/second, and
    Playwright is kept only as a last-resort fallback in case the site's
    CF rules change later — set SKIP_PLAYWRIGHT=false to re-enable it on
    CI if you ever see it start succeeding again.

    Args:
        url:     The URL to fetch.
        retries: How many times to retry the Playwright method before
                 giving up (only used if Playwright isn't skipped).

    Returns:
        HTML string on success, None if all methods fail.
    """
    is_ci = os.environ.get("CI", "").lower() in ("true", "1", "yes")

    # ── 1. Fast path: curl_cffi direct (no proxy) ──
    logger.info("[fetch_html] curl_cffi attempt for %s", url)
    html = _fetch_with_curl_cffi(url)
    if html:
        return html

    # ── 2. curl_cffi through Webshare rotating proxy ──
    # This is empirically the method that bypasses this site's CF rules
    # from datacenter IPs — try it before paying Playwright's time cost.
    logger.info("[fetch_html] Trying Webshare proxy for %s", url)
    html = _fetch_with_webshare_proxy(url)
    if html:
        return html

    # ── 3. Last resort: Playwright + stealth ──
    # Default-skipped on CI since it has a 0% success rate against this
    # site in practice. Override with SKIP_PLAYWRIGHT=false to re-enable.
    default_skip = "true" if is_ci else "false"
    skip_playwright = os.environ.get("SKIP_PLAYWRIGHT", default_skip).lower() in ("1", "true", "yes")

    if skip_playwright:
        logger.warning(
            "[fetch_html] curl_cffi + Webshare both failed for %s — "
            "Playwright skipped (SKIP_PLAYWRIGHT=%s)", url, skip_playwright,
        )
        return None

    for attempt in range(1, retries + 1):
        logger.info("[fetch_html] Playwright attempt %d/%d for %s", attempt, retries, url)
        html = _fetch_with_playwright(url)
        if html:
            return html
        if attempt < retries:
            sleep_s = 10 * attempt
            logger.info("[fetch_html] Playwright failed — sleeping %ds before retry", sleep_s)
            time.sleep(sleep_s)

    return None