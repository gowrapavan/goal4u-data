import requests
from bs4 import BeautifulSoup
from core.team_data import load_team_data
from core.utils import short_label

def get_stream_url(article_url):
    """Visits the article page to extract the actual iframe stream URL."""
    try:
        # Use a real User-Agent to avoid blocks
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(article_url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        iframe = soup.select_one("iframe")
        return iframe.get("src") if iframe else None
    except:
        return None

def scrape():
    load_team_data()
    all_matches = []
    url = "https://www.freekora.com/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Target the match blocks
        for m in soup.select(".AY_Match"):
            try:
                # Get names
                home = m.select_one(".TM1 .TM_Name").text.strip()
                away = m.select_one(".TM2 .TM_Name").text.strip()
                
                # Get both logos (targeting TM1 and TM2 separately)
                home_logo = m.select_one(".TM1 .TM_Logo img").get("data-src")
                away_logo = m.select_one(".TM2 .TM_Logo img").get("data-src")
                
                # Get the link to the watch page
                article_url = m.select_one("a").get("href")
                
                # Get the real stream URL from the watch page
                stream_url = get_stream_url(article_url)
                
                all_matches.append({
                    "home_team": home,
                    "away_team": away,
                    "home_logo": home_logo,
                    "away_logo": away_logo,
                    "league": "Live",
                    "time": m.select_one(".MT_Time").text.strip(),
                    "url": stream_url or article_url, # Fallback to article if iframe not found
                    "label": short_label(home, away)
                })
            except Exception as e:
                continue # Skip broken cards
                
    except Exception as e:
        print(f"❌ Error scraping Freekora: {e}")
        
    return all_matches