# scrapers/koraaclub.py
"""
Scraper for https://koraa.club/

Match-listing page is the same AlbaYallaShoot WP theme as live_soccer.py
(`.MT_Info` li items are visible here, so channel/commentator/league use the
same extraction as live_soccer.py, not the .TourName fallback koralivetv.py
needed).

The article pages it links to are a different story. Example:
q1.smartkora.com/2026/06/e1.html is a Blogger-hosted page with NO static
<iframe src="..."> to scrape. Instead:

  - A JS player (Clappr) is bootstrapped from a `window.firstStreamUrl = '...'`
    assignment — that's the "بث 1" / default stream.
  - Extra server buttons ("بث 2", "بث 3", "EN") each carry their own URL
    inside onclick="window.bgrSwitch('URL', 'type', this[, 'referrer'])".

Since there's no headless browser here, get_stream_url() pulls these via
regex against the raw HTML rather than DOM selectors, and falls back to the
plain-iframe approach (used by live_soccer.py / koralivetv.py) in case a
match links out to a simpler page instead.
"""

import re
import requests
from bs4 import BeautifulSoup
from core.team_data import load_team_data
from core.utils import short_label

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

BASE_URL = "https://koraa.club/"

# Don't bother resolving streams for matches that have already ended.
SKIP_STREAM_STATUSES = {"finished"}

IFRAME_SELECTORS = [
    ".entry-content iframe",
    "#postsacs iframe",
    "iframe",
]

FIRST_STREAM_RE = re.compile(r"window\.firstStreamUrl\s*=\s*['\"]([^'\"]+)['\"]")
BGR_BUTTON_RE = re.compile(
    r"<button[^>]*onclick=\"window\.bgrSwitch\(\s*([^,]+?)\s*,\s*'([^']*)'[^)]*\)\"[^>]*>(.*?)</button>",
    re.S,
)


def _extract_js_servers(html, first_stream_url):
    """Parse out the bgrSwitch button list: [{label, type, url}, ...]."""
    servers = []
    for url_expr, stream_type, label in BGR_BUTTON_RE.findall(html):
        url_expr = url_expr.strip()
        if url_expr == "window.firstStreamUrl":
            url = first_stream_url
        else:
            url = url_expr.strip("'\"")
        label_clean = re.sub(r"<[^>]+>", "", label).strip()
        if url:
            servers.append({"label": label_clean or stream_type, "type": stream_type, "url": url})
    return servers


def get_stream_url(article_url):
    """
    Follow a match's link and resolve its stream(s).
    Returns (primary_url, servers_list) -- servers_list may be empty.
    """
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=10)
        html = resp.text

        # JS-driven player (e.g. smartkora.com blogger pages)
        match = FIRST_STREAM_RE.search(html)
        if match:
            primary = match.group(1)
            servers = _extract_js_servers(html, primary)
            return primary, servers

        # Fall back to a plain iframe somewhere on the page
        soup = BeautifulSoup(html, "html.parser")
        for selector in IFRAME_SELECTORS:
            iframe = soup.select_one(selector)
            if iframe and iframe.get("src"):
                return iframe.get("src"), []

    except Exception:
        pass
    return None, []


def parse_match(m):
    classes = m.get("class", [])
    status = next((c for c in classes if c != "AY_Match"), "unknown")

    tm1 = m.select_one(".TM1")
    tm2 = m.select_one(".TM2")
    if not tm1 or not tm2:
        return None

    home_tag = tm1.select_one(".TM_Name")
    away_tag = tm2.select_one(".TM_Name")
    if not home_tag or not away_tag:
        return None

    home = home_tag.get_text(strip=True)
    away = away_tag.get_text(strip=True)

    home_logo_tag = tm1.select_one(".TM_Logo img")
    away_logo_tag = tm2.select_one(".TM_Logo img")
    home_logo = (home_logo_tag.get("data-src") or home_logo_tag.get("src")) if home_logo_tag else None
    away_logo = (away_logo_tag.get("data-src") or away_logo_tag.get("src")) if away_logo_tag else None

    time_tag = m.select_one(".MT_Time")
    time_str = time_tag.get_text(strip=True) if time_tag else ""

    score = None
    result_tag = m.select_one(".MT_Result")
    if result_tag:
        goals = [g.get_text(strip=True) for g in result_tag.select(".RS-goals")]
        if len(goals) == 2:
            score = f"{goals[0]}-{goals[1]}"

    stat_tag = m.select_one(".MT_Stat")
    status_text = stat_tag.get_text(strip=True) if stat_tag else ""

    info_items = m.select(".MT_Info li")
    channel = info_items[0].get_text(strip=True) if len(info_items) > 0 else ""
    commentator = info_items[1].get_text(strip=True) if len(info_items) > 1 else ""
    league = info_items[2].get_text(strip=True) if len(info_items) > 2 else ""

    link_tag = m.select_one("a[href]")
    article_url = link_tag.get("href") if link_tag else None
    if article_url and article_url.startswith("/"):
        article_url = BASE_URL.rstrip("/") + article_url

    stream_url, servers = (None, [])
    if article_url and status not in SKIP_STREAM_STATUSES:
        stream_url, servers = get_stream_url(article_url)

    return {
        "home_team": home,
        "away_team": away,
        "home_logo": home_logo,
        "away_logo": away_logo,
        "league": league,
        "time": time_str,
        "score": score,
        "status": status,
        "status_text": status_text,
        "channel": channel,
        "commentator": commentator,
        "url": stream_url or article_url,
        "servers": servers,        # alternate streams, if any: [{label, type, url}]
        "article_url": article_url,
        "label": short_label(home, away),
    }


def scrape():
    load_team_data()
    all_matches = []

    try:
        response = requests.get(BASE_URL, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")

        for m in soup.select(".AY_Match"):
            try:
                match = parse_match(m)
                if match:
                    all_matches.append(match)
            except Exception:
                continue

    except Exception as e:
        print(f"❌ Error scraping koraa.club: {e}")
        return []

    print(f"✅ koraa.club: {len(all_matches)} matches")
    return all_matches


if __name__ == "__main__":
    import json
    print(json.dumps(scrape(), ensure_ascii=False, indent=2))