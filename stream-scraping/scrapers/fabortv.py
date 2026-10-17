# scrapers/fabortv.py
"""
Scraper for https://fabor-tv.to/

How the site works (from the page sources):

  1. The home page lists today's matches as `.AY_Match` cards (same
     AlbaYallaShoot WP theme as live_soccer / koraaclub).
       - class "live"        -> the card link goes straight to a stream gateway:
                                https://fabortvto.hes-goals.mov/?m=<id>&p=<p>
       - class "not-started" -> the card link is just an article page on
                                fabor-tv.to (no stream exists yet)
     Kickoff time is in the card's  data-start="2026-10-17 12:30:00+01:00".

  2. The gateway instantly redirects to a player page:
       https://yassirtv.com/hard/<hash><p>.html?match=<id>

  3. That player page has NO static <iframe src>. A <head> script builds it:
       window.__playerSrc = 'https://<host>/playerv5.php?match=' + encodeURIComponent(m)
                            + '&key=<key>'
     and the body <iframe id="player"> gets that src.

So we: parse cards -> follow the gateway redirect (HTTP, meta-refresh or JS)
-> pull the playerv5 iframe URL out of the final page.

Output schema matches koraaclub.py (home_team, away_team, ..., url, servers,
article_url, label) plus:  match_id, page_url, referer.
  url      = playerv5 iframe URL (best), else the yassirtv page, else the card link
  servers  = [{label, type, url}]  (one entry when the iframe was resolved)
  referer  = origin of the page that embeds the iframe (send it when probing/playing)
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core.utils import IST, short_label

BASE_URL = "https://fabor-tv.to/"

# Tried in order until one returns match cards.
PAGE_URLS = [
    "https://fabor-tv.to/fabor4/",
    "https://fabor-tv.to/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.8",
}

TIMEOUT = 12
MAX_WORKERS = 6

# Statuses we never resolve a stream for.
SKIP_STREAM_STATUSES = {"finished"}

# Upcoming matches only link to an article page (no stream yet), so resolving
# them wastes requests. Set True to try anyway.
RESOLVE_UPCOMING = False

# LAST RESORT only: if the gateway redirect can't be followed (e.g. it is built
# by obfuscated JS), try the page URL pattern seen in the browser:
#   https://yassirtv.com/hard/2908c7d4425d87350.html?match=4742043
# where 87350 is the "p" value from the card link. The hash part may rotate,
# so set this to "" to disable.
FALLBACK_PAGE_TEMPLATE = "https://yassirtv.com/hard/2908c7d4425d{p}.html?match={m}"

# ── regexes ──────────────────────────────────────────────────────────────────

# Full URL already present in the HTML:  .../playerv5.php?match=4742043&key=abc
PLAYER_FULL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?/playerv5\.php\?match=\d+&key=[\w-]+", re.I
)

# URL assembled by JS:
#   'https://host/playerv5.php?match=' + encodeURIComponent(m) + '&key=abc'
PLAYER_JS_RE = re.compile(
    r"[\"'](https?://[^\"'\s]+?\.php\?match=)[\"']\s*\+\s*"
    r"encodeURIComponent\(\s*[\w.]+\s*\)\s*\+\s*[\"']&key=([\w-]+)[\"']"
)

META_REFRESH_RE = re.compile(
    r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]*content=[\"']?\s*\d+\s*;\s*url=([^\"'>\s]+)",
    re.I,
)
JS_REDIRECT_RE = re.compile(
    r"(?:window\.|top\.|document\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*[\"']([^\"']+)[\"']",
    re.I,
)

# Best-effort league name pulled from the card's link title (the card itself
# has an empty .TourName). Arabic text -- translate_matches() converts it.
LEAGUE_RE = re.compile(
    r"(الدوري\s+\S+|دوري\s+أبطال\s+\S+|الكونفدرالية\s+\S+|كأس\s+\S+(?:\s+\S+)?)"
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _get(url, referer=None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    r.encoding = "utf-8"  # don't let requests guess (avoids mojibake)
    return r


def _qs(url, *keys):
    """First query-string value found for any of `keys`."""
    q = parse_qs(urlparse(url or "").query)
    for k in keys:
        if q.get(k):
            return q[k][0]
    return None


def _origin(url):
    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else None


def extract_player(html, page_url, match_id):
    """Find the playerv5 iframe URL in a page, or None."""
    html_u = html.replace("&amp;", "&")

    # 1) A real <iframe src="...playerv5.php?match=..."> in the HTML.
    soup = BeautifulSoup(html, "html.parser")
    for ifr in soup.select("iframe[src]"):
        src = (ifr.get("src") or "").strip()
        if "playerv5" in src or ".php?match=" in src:
            return urljoin(page_url, src)

    # 2) Full URL sitting anywhere in the source.
    m = PLAYER_FULL_RE.search(html_u)
    if m:
        return m.group(0)

    # 3) URL assembled by the <head> script (this is what yassirtv.com does).
    m = PLAYER_JS_RE.search(html_u)
    if m and match_id:
        return f"{m.group(1)}{quote(str(match_id))}&key={m.group(2)}"

    return None


def resolve_stream(link):
    """
    Follow a card link to the player page and extract the iframe URL.
    Returns {player_url, page_url, match_id, error}.
    """
    out = {
        "player_url": None,
        "page_url": None,
        "match_id": _qs(link, "m", "match"),
        "error": None,
    }

    seen = set()
    url, referer = link, BASE_URL

    for _ in range(5):  # max redirect hops (HTTP + meta + JS)
        if url in seen:
            break
        seen.add(url)
        try:
            r = _get(url, referer)
        except Exception as e:
            out["error"] = type(e).__name__
            break

        final = r.url
        out["page_url"] = final
        out["match_id"] = _qs(final, "match", "m") or out["match_id"]

        if r.status_code >= 400:
            out["error"] = f"HTTP {r.status_code}"
            break

        player = extract_player(r.text, final, out["match_id"])
        if player:
            out["player_url"] = player
            out["error"] = None
            return out

        nxt = META_REFRESH_RE.search(r.text) or JS_REDIRECT_RE.search(r.text)
        if not nxt:
            break
        referer, url = final, urljoin(final, nxt.group(1))

    # Last resort: guessed player-page URL.
    p = _qs(link, "p")
    if FALLBACK_PAGE_TEMPLATE and p and out["match_id"] and not out["player_url"]:
        guess = FALLBACK_PAGE_TEMPLATE.format(p=p, m=out["match_id"])
        try:
            r = _get(guess, BASE_URL)
            if r.status_code < 400:
                player = extract_player(r.text, r.url, out["match_id"])
                if player:
                    out.update(player_url=player, page_url=r.url, error=None)
        except Exception:
            pass

    return out


def kickoff_ist(card):
    """data-start (with UTC offset) -> 'YYYY-MM-DD HH:MM IST'. Falls back to .MT_Time."""
    raw = (card.get("data-start") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        except ValueError:
            pass
    t = card.select_one(".MT_Time")
    return t.get_text(strip=True) if t else ""


def parse_card(card):
    tm1, tm2 = card.select_one(".TM1"), card.select_one(".TM2")
    if not tm1 or not tm2:
        return None
    home_tag, away_tag = tm1.select_one(".TM_Name"), tm2.select_one(".TM_Name")
    if not home_tag or not away_tag:
        return None

    home = home_tag.get_text(strip=True)
    away = away_tag.get_text(strip=True)

    def logo(team):
        img = team.select_one(".TM_Logo img")
        if not img:
            return None
        src = img.get("data-src") or img.get("src")
        return urljoin(BASE_URL, src) if src else None

    # status = the card's class that isn't a theme helper (live / not-started / finished ...)
    classes = [c for c in card.get("class", []) if not c.startswith("AY_")]
    status = classes[0] if classes else "unknown"

    score = None
    res = card.select_one(".MT_Result")
    if res:
        goals = [g.get_text(strip=True) for g in res.select(".RS-goals")]
        if len(goals) == 2:
            score = f"{goals[0]}-{goals[1]}"

    stat = card.select_one(".MT_Stat")

    link_tag = card.select_one("a[href]")
    link = urljoin(BASE_URL, link_tag["href"]) if link_tag else None
    title = (link_tag.get("title") or "") if link_tag else ""
    lm = LEAGUE_RE.search(title)

    return {
        "home_team": home,
        "away_team": away,
        "home_logo": logo(tm1),
        "away_logo": logo(tm2),
        "league": lm.group(1).strip() if lm else "",
        "time": kickoff_ist(card),
        "score": score,
        "status": status,
        "status_text": stat.get_text(strip=True) if stat else "",
        "_link": link,
    }


def _fetch_cards():
    for page in PAGE_URLS:
        try:
            r = _get(page)
            if r.status_code >= 400:
                print(f"   ⚠️ {page} -> HTTP {r.status_code}")
                continue
            cards = BeautifulSoup(r.text, "html.parser").select(".AY_Match")
            if cards:
                return cards
        except Exception as e:
            print(f"   ⚠️ {page} failed: {type(e).__name__}: {e}")
    return []


def _should_resolve(m):
    if not m["_link"] or m["status"] in SKIP_STREAM_STATUSES:
        return False
    if m["status"] == "not-started" and not RESOLVE_UPCOMING:
        return False
    return True


# ── entry point ──────────────────────────────────────────────────────────────

def scrape():
    cards = _fetch_cards()
    if not cards:
        print("❌ fabor-tv.to: no match cards found")
        return []

    matches = []
    for c in cards:
        try:
            m = parse_card(c)
            if m:
                matches.append(m)
        except Exception:
            continue

    # Resolve streams in parallel (one gateway + one player page per live match).
    todo = [m for m in matches if _should_resolve(m)]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        resolved = list(ex.map(lambda m: resolve_stream(m["_link"]), todo))
    by_id = {id(m): r for m, r in zip(todo, resolved)}

    results = []
    ok = 0
    for m in matches:
        link = m.pop("_link")
        r = by_id.get(id(m)) or {}
        player = r.get("player_url")
        page = r.get("page_url")

        if player:
            ok += 1
            url = player
        elif page and page != link:
            url = page            # at least the yassirtv page (it embeds the player)
        else:
            url = link            # fallback: no stream resolved

        results.append({
            "match_id": r.get("match_id") or _qs(link, "m", "match"),
            **m,
            "channel": "",
            "url": url,
            "servers": (
                [{"label": "Yasir TV", "type": "iframe", "url": player}] if player else []
            ),
            "page_url": page,
            "referer": _origin(page) if page and page != link else None,
            "article_url": link,
            "label": short_label(m["home_team"], m["away_team"]),
        })

    print(f"✅ fabor-tv.to: {len(results)} matches, {ok} streams resolved")
    return results


if __name__ == "__main__":
    import json
    print(json.dumps(scrape(), ensure_ascii=False, indent=2))