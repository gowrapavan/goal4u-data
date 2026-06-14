import requests
import re
from datetime import datetime
from core.utils import convert_time, short_label, IST, GMT
from core.team_data import find_team_crest

def fetch():
    """Scrape matches from sportsonline.st"""
    url = "https://sportsonline.pk/prog.txt"
    text = requests.get(url, timeout=10).text

    today = datetime.now(IST).strftime("%A").upper()
    pattern = rf"{today}\n(.*?)(?=\n[A-Z]+\n|$)"
    m = re.search(pattern, text, re.S)
    
    if not m:
        return []

    block = m.group(1)
    matches = []

    for line in block.splitlines():
        m = re.match(r"(\d{2}:\d{2})\s+(.+?)\s+x\s+(.+?) \| (http.+)", line)
        if m:
            time_str, home, away, stream_url = m.groups()
            
            home_logo = find_team_crest(home.strip())
            away_logo = find_team_crest(away.strip())

            matches.append({
                "time": convert_time(time_str, GMT),
                "game": "football",
                "league": "",
                "home_team": home.strip(),
                "away_team": away.strip(),
                "label": short_label(home, away),
                "home_logo": home_logo,
                "away_logo": away_logo,
                "url": stream_url.strip()
            })
            
    return matches