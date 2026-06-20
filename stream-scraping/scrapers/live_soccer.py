# scrapers/live_soccer.py
"""
Scraper for https://live-soccer.tv/

Page structure (AlbaYallaShoot WP theme, same as freekora/yallashots):
  .AY_Match                  -> one match card. Its class list also carries the
                                 status: "live", "not-started", "finished",
                                 "comming-soon", "gools", etc.
  .MT_Team.TM1 / .MT_Team.TM2 -> each side: ".TM_Name" + ".TM_Logo img[data-src]"
  .MT_Time                    -> kickoff time text
  .MT_Result .RS-goals        -> two spans with the live/final score
  .MT_Stat                    -> human status text ("جارية الان" / "لم تبدأ بعد" / "انتهت")
  .MT_Info li                 -> [channel, commentator, league] (icons via ::before)
  a[href]                     -> link to follow for the stream iframe.
                                 NOTE: for live matches this href goes straight to a
                                 stream page on a different domain (e.g.
                                 world.kooora-sia.com/bein-1/) which already has the
                                 <iframe> in .entry-content. For not-started matches
                                 it's a live-soccer.tv/matches/... article page. Same
                                 get_stream_url() handles both since we just look for
                                 the first iframe on whatever page the href points to.
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

BASE_URL = "https://live-soccer.tv/"

# Don't bother resolving an iframe for matches that have already ended.
SKIP_STREAM_STATUSES = {"finished"}


def get_stream_url(article_url):
    """Follow a match's link and pull the real iframe stream URL from it."""
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        iframe = soup.select_one(".entry-content iframe") or soup.select_one("iframe")
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

    info_items = m.select(".MT_Info li")
    channel = info_items[0].get_text(strip=True) if len(info_items) > 0 else ""
    commentator = info_items[1].get_text(strip=True) if len(info_items) > 1 else ""
    league = info_items[2].get_text(strip=True) if len(info_items) > 2 else ""

    link_tag = m.select_one("a[href]")
    article_url = link_tag.get("href") if link_tag else None

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
        "channel": channel,
        "commentator": commentator,
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
        print(f"❌ Error scraping live-soccer.tv: {e}")
        return []

    print(f"✅ live-soccer.tv: {len(all_matches)} matches")
    return all_matches


if __name__ == "__main__":
    import json
    print(json.dumps(scrape(), ensure_ascii=False, indent=2))