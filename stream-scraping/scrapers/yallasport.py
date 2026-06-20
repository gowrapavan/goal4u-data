import re
import requests
import pytz
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from core.utils import convert_time, short_label
from core.team_data import find_team_crest

URL = "https://s22.yalla-sport.top/kooratv.txt"
GMT3 = pytz.FixedOffset(180)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

LINE_RE = re.compile(r"(\d{2}:\d{2})\s+(.+?)\s+vs\s+(.+?)\s*\|\s*(\S+)")


def split_team(raw):
    raw = raw.strip()
    m = re.match(r"^(.*\S)\s+([A-Z]{2,4})$", raw)
    if m:
        return m.group(1).strip(), m.group(2)
    return raw, ""


def fetch():
    try:
        resp = requests.get(URL, headers=HEADERS, timeout=10)
        body = resp.text
    except Exception as e:
        print(f"❌ Error fetching yalla-sport.top schedule: {e}")
        return []

    # As of mid-2026 the root page is sometimes just a stub that links out
    # to the real schedule file (e.g. "/kooratv.txt") instead of embedding
    # it directly. Follow that link if present, rather than hardcoding the
    # filename -- it appears to rotate the same way the stream subdomain does.
    if "<" in body[:200]:
        soup = BeautifulSoup(body, "html.parser")
        txt_link = soup.select_one('a[href$=".txt"]')
        if txt_link and txt_link.get("href"):
            txt_url = urljoin(URL, txt_link["href"])
            try:
                resp = requests.get(txt_url, headers=HEADERS, timeout=10)
                body = resp.text
            except Exception as e:
                print(f"❌ Error fetching schedule file {txt_url}: {e}")
                return []

    text = BeautifulSoup(body, "html.parser").get_text("\n") if "<" in body[:200] else body

    matches = []
    for time_str, home_raw, away_raw, stream_url in LINE_RE.findall(text):
        home, home_code = split_team(home_raw)
        away, away_code = split_team(away_raw)

        matches.append({
            "time": convert_time(time_str, GMT3),
            "game": "football",
            "league": "",
            "home_team": home,
            "away_team": away,
            "home_code": home_code,
            "away_code": away_code,
            "home_logo": find_team_crest(home),
            "away_logo": find_team_crest(away),
            "label": short_label(home, away),
            "url": stream_url.strip(),
        })

    print(f"✅ yalla-sport.top: {len(matches)} matches")
    return matches


if __name__ == "__main__":
    import json
    print(json.dumps(fetch(), ensure_ascii=False, indent=2))