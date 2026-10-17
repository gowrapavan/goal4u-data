# scrapers/sportsonline.py
"""
Scraper for sportsonline's daily schedule file (prog.txt).

File layout -- one block per weekday, times in UK local time:

    SATURDAY
    HD1 ENGLISH                      <- channel legend for that day
    HD2 ENGLISH
    ...
    12:30 Arsenal x Leeds United | https://.../channels/hd/hd1.php
    12:30 Arsenal x Leeds United | https://.../channels/pt/eleven1.php   <- same match, another channel
    ...
    00:10 Junior x Inter Bogota | https://...    <- after midnight, but still listed under SATURDAY

Compared with the old version this:
  * treats times as UK local time (BST in summer), not GMT
  * dates entries after midnight as the NEXT calendar day
  * merges the one-line-per-channel repeats into a single match with `servers`
  * skips non-football events (MotoGP, F1, UFC, NFL, tennis, beach soccer...)
  * decodes the file as UTF-8 itself (requests would guess ISO-8859-1 for a
    text/plain response and garble names like "Munchen" / "Fenerbahce")

Proxy (sportsonline is blocked in some countries, e.g. India)
-----------------------------------------------------------
Nothing to set up: if the direct connection fails, this scraper finds a free
PUBLIC proxy by itself (downloads public proxy lists, tests hundreds of them in
parallel against the real schedule, keeps the first that works, and remembers
it for the next run). Free proxies are flaky, so a run can occasionally fail --
just run again.

Optional:
    SPORTSONLINE_PROXY=http://user:pass@host:port   your own proxy(ies), comma
                                                    separated, tried FIRST
    SPORTSONLINE_PUBLIC_PROXY=0                     turn the auto public-proxy
                                                    search off
Only this scraper uses a proxy; the other sources connect directly. Never send
passwords through a public proxy (this one only downloads a public text file).
"""

import os
import random
import re
import tempfile
import threading
import time
import requests
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta

from core.utils import short_label, IST
from core.team_data import find_team_crest

# Tried in order. The site now says to use sportsonline.st; .pk is a fallback.
URLS = [
    "https://sportsonline.st/prog.txt",
    "https://sportsonline.pk/prog.txt",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

# Kick-off times in the file match UK slots (Premier League 12:30 / 15:00 /
# 17:30, Bundesliga 15:30 CEST shown as 14:30). Europe/London switches
# between GMT and BST automatically (same offset as Lisbon).
SITE_TZ = pytz.timezone("Europe/London")

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
DAY_RE = re.compile(r"^(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)\s*$", re.M)
LEGEND_RE = re.compile(r"^((?:HD|BR)\d+)\s+([A-Z][A-Z &]*)$")          # "HD9 GERMAN & SPANISH"
MATCH_RE = re.compile(r"^(\d{1,2}):(\d{2})\s+(.+?)\s*\|\s*(https?://\S+)\s*$")
TEAMS_RE = re.compile(r"^(.+?)\s+x\s+(.+)$")


# ── proxy settings ───────────────────────────────────────────────────────────

# Your own proxy/proxies (comma separated). Tried first. Optional.
PROXY_LIST = [p.strip() for p in os.environ.get("SPORTSONLINE_PROXY", "").split(",") if p.strip()]

# Auto-find a free public proxy when the direct connection fails.
USE_PUBLIC_PROXIES = os.environ.get("SPORTSONLINE_PUBLIC_PROXY", "1") != "0"

# Free proxy lists on GitHub (plain text, one ip:port per line).
PUBLIC_PROXY_LISTS = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
]
IP_PORT_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")

MAX_CANDIDATES = 600      # random sample tested per run
PROBE_WORKERS = 60        # proxies tested at the same time
PROBE_TIMEOUT = (5, 8)    # (connect, read) per public proxy
POOL_BUDGET_S = 100       # give up on the pool after this many seconds
DIRECT_TIMEOUT = (6, 15)  # direct connection (short, so a block fails fast)
TIMEOUT = (10, 20)        # your own proxies

# Remembers the last public proxy that worked (outside the repo).
CACHE_FILE = os.path.join(tempfile.gettempdir(), "sportsonline_proxy.txt")


def _mask(proxy):
    """http://user:pass@host:port -> http://***@host:port (safe to print)."""
    return re.sub(r"//[^@/]+@", "//***@", proxy) if proxy else "direct"


def _looks_like_schedule(text):
    """Real prog.txt = weekday headings AND 'HH:MM A x B | http...' lines.
    (Rejects proxy error pages / captive portals that return HTTP 200.)"""
    if not DAY_RE.search(text):
        return False
    return any(MATCH_RE.match(line.strip()) for line in text.splitlines())


def _try(url, proxy, timeout):
    """Download `url` (optionally via `proxy`); schedule text or None."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, proxies=proxies)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
    except Exception:
        return None
    return text if _looks_like_schedule(text) else None


def _load_public_proxies():
    """Download the public lists (directly) -> shuffled list of 'ip:port'."""
    found = set()

    def grab(list_url):
        try:
            r = requests.get(list_url, timeout=(6, 20))
            r.raise_for_status()
            return [f"{ip}:{port}" for ip, port in IP_PORT_RE.findall(r.text)]
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=len(PUBLIC_PROXY_LISTS)) as ex:
        for chunk in ex.map(grab, PUBLIC_PROXY_LISTS):
            found.update(chunk)

    pool = list(found)
    random.shuffle(pool)
    return pool[:MAX_CANDIDATES]


def _search_public_pool(url):
    """Test public proxies in parallel; return (proxy_url, text) for the first that works."""
    candidates = _load_public_proxies()
    if not candidates:
        print("⚠️ sportsonline: couldn't download any public proxy list")
        return None
    print(f"   sportsonline: direct connection failed, testing {len(candidates)} public proxies...")

    stop = threading.Event()

    def probe(addr):
        if stop.is_set():
            return None
        proxy = f"http://{addr}"
        text = _try(url, proxy, PROBE_TIMEOUT)
        return (proxy, text) if text else None

    ex = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
    futures = [ex.submit(probe, c) for c in candidates]
    result = None
    try:
        for fut in as_completed(futures, timeout=POOL_BUDGET_S):
            res = fut.result()
            if res:
                result = res
                break
    except FuturesTimeout:
        pass
    finally:
        stop.set()
        ex.shutdown(wait=False, cancel_futures=True)
    return result


def _read_cached_proxy():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_cached_proxy(proxy):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(proxy)
    except OSError:
        pass


def _download():
    """
    Return the schedule text, or None.
    Order: your own proxies -> last working public proxy -> direct -> fresh public pool.
    """
    # 1) your own proxies
    for proxy in PROXY_LIST:
        for url in URLS:
            text = _try(url, proxy, TIMEOUT)
            if text:
                print(f"   sportsonline: downloaded via {_mask(proxy)}")
                return text
        print(f"⚠️ sportsonline: proxy {_mask(proxy)} failed")

    # 2) the public proxy that worked last time
    cached = _read_cached_proxy() if USE_PUBLIC_PROXIES else None
    if cached:
        text = _try(URLS[0], cached, PROBE_TIMEOUT)
        if text:
            print(f"   sportsonline: downloaded via cached proxy {cached}")
            return text

    # 3) direct (works outside blocked regions, e.g. GitHub Actions)
    for url in URLS:
        text = _try(url, None, DIRECT_TIMEOUT)
        if text:
            print("   sportsonline: downloaded directly")
            return text

    # 4) search a fresh public proxy
    if USE_PUBLIC_PROXIES:
        found = _search_public_pool(URLS[0])
        if found:
            proxy, text = found
            _write_cached_proxy(proxy)
            print(f"   sportsonline: downloaded via public proxy {proxy}")
            return text
        print("⚠️ sportsonline: no working public proxy this time -- run again")
    return None


def _parse(text, today):
    """Football matches from the site's current-day block (UK date `today`)."""
    wanted_day = DAYS[today.weekday()]
    day = None
    legend = {}
    prev_minutes = None
    rolled = False  # True once the clock has wrapped past midnight in this block
    grouped = {}

    for raw in text.splitlines():
        line = raw.strip()

        heading = DAY_RE.match(line)
        if heading:
            day, legend, prev_minutes, rolled = heading.group(1), {}, None, False
            continue
        if day != wanted_day:
            continue

        leg = LEGEND_RE.match(line)
        if leg:
            legend[leg.group(1)] = leg.group(2).strip()
            continue

        m = MATCH_RE.match(line)
        if not m:
            continue
        hh, mm, title, url = int(m.group(1)), int(m.group(2)), m.group(3).strip(), m.group(4)

        # A block runs ~07:00 -> ~04:00 next morning. A big backwards jump in
        # the clock means midnight was crossed; small dips (17:30 -> 17:00)
        # are just unsorted lines.
        minutes = hh * 60 + mm
        if prev_minutes is not None and minutes < prev_minutes - 360:
            rolled = True
        prev_minutes = minutes

        # Football only: "A x B", and no "Category: " prefix (Beach Soccer: ...).
        if ":" in title:
            continue
        teams = TEAMS_RE.match(title)
        if not teams:
            continue
        home, away = teams.group(1).strip(), teams.group(2).strip()

        start = SITE_TZ.localize(
            datetime(today.year, today.month, today.day, hh, mm)
            + timedelta(days=1 if rolled else 0)
        )

        key = (hh, mm, rolled, home, away)
        entry = grouped.get(key)
        if entry is None:
            entry = grouped[key] = {
                "_start": start,
                "time": start.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
                "game": "football",
                "league": "",
                "home_team": home,
                "away_team": away,
                "label": short_label(home, away),
                "home_logo": find_team_crest(home),
                "away_logo": find_team_crest(away),
                "url": url,       # first channel listed
                "servers": [],    # every channel: [{label, type, url}]
            }

        channel = url.rsplit("/", 1)[-1].split(".")[0].upper()   # ".../hd/hd2.php" -> "HD2"
        language = legend.get(channel)
        if all(s["url"] != url for s in entry["servers"]):
            entry["servers"].append({
                "label": f"{channel} · {language}" if language else channel,
                "type": "iframe",
                "url": url,
            })

    matches = sorted(grouped.values(), key=lambda e: e["_start"])
    for e in matches:
        del e["_start"]
    return matches


def fetch():
    text = _download()
    if not text:
        print("❌ sportsonline: could not download the schedule")
        return []

    today = datetime.now(SITE_TZ).date()
    matches = _parse(text, today)
    print(f"✅ sportsonline: {len(matches)} football matches ({DAYS[today.weekday()]} slate)")
    return matches


if __name__ == "__main__":
    import json
    print(json.dumps(fetch(), ensure_ascii=False, indent=2))