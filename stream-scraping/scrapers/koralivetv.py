# scrapers/koralivetv.py
"""
Scraper for https://www.koralivetv.online/

Same AlbaYallaShoot WP theme as live_soccer.py, but two differences worth
calling out:

1. This theme variant force-hides `.MT_Info` (display:none!important), so
   channel/commentator/league aren't available there. League text instead
   lives in a `.TourName` div inside `.MT_Data`.

2. Match links don't all stay on koralivetv.online. Some point out to a
   Blogger-hosted "yallakoralive" stream page (e.g.
   22.yallakoralive.com/2026/05/bein-sport-3.html), which uses a totally
   different template where the <iframe> lives inside `#postsacs` instead
   of `.entry-content`. get_stream_url() tries both, plus a bare `iframe`
   fallback, to cover whatever the link resolves to.
"""

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

BASE_URL = "https://www.koralivetv.online/"

# Don't bother resolving an iframe for matches that have already ended.
SKIP_STREAM_STATUSES = {"finished"}

# Tried in order against whatever page the match link resolves to.
IFRAME_SELECTORS = [
    ".entry-content iframe",  # AlbaYallaShoot theme (live-soccer.tv / koralivetv.online articles)
    "#postsacs iframe",       # Blogger "yallakoralive" stream pages
    "iframe",                 # generic fallback
]


def get_stream_url(article_url):
    """Follow a match's link and pull the real iframe stream URL from it."""
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for selector in IFRAME_SELECTORS:
            iframe = soup.select_one(selector)
            if iframe and iframe.get("src"):
                return iframe.get("src")
    except Exception:
        pass
    return None


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

    # League lives in .TourName here, not .MT_Info li like live-soccer.tv.
    # Fall back to .MT_Info li in case a future template restores it.
    league_tag = m.select_one(".TourName")
    if league_tag:
        league = league_tag.get_text(strip=True)
    else:
        info_items = m.select(".MT_Info li")
        league = info_items[-1].get_text(strip=True) if info_items else ""

    link_tag = m.select_one("a[href]")
    article_url = link_tag.get("href") if link_tag else None
    # Some hrefs are relative ("/matches-today"); normalize against BASE_URL.
    if article_url and article_url.startswith("/"):
        article_url = BASE_URL.rstrip("/") + article_url

    stream_url = None
    if article_url and status not in SKIP_STREAM_STATUSES:
        stream_url = get_stream_url(article_url)

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
        "url": stream_url or article_url,
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
        print(f"❌ Error scraping koralivetv.online: {e}")
        return []

    print(f"✅ koralivetv.online: {len(all_matches)} matches")
    return all_matches


if __name__ == "__main__":
    import json
    print(json.dumps(scrape(), ensure_ascii=False, indent=2))