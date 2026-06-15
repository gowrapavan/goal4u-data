import requests
from bs4 import BeautifulSoup
from core.team_data import load_team_data
from core.utils import short_label

def get_stream_url(article_url):
    """Visits the article page to extract the actual iframe stream URL."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(article_url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Look for the iframe in the post content
        iframe = soup.select_one("#postsacs iframe")
        return iframe.get("src") if iframe and iframe.has_attr("src") else article_url
    except:
        return article_url

def scrape():
    load_team_data()
    all_matches = []
    url = "https://www.yallashots.top/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Matches are inside .AY_Match
        for m in soup.select(".AY_Match"):
            try:
                # Get Team 1 Data
                tm1 = m.select_one(".TM1")
                home = tm1.select_one(".TM_Name").text.strip()
                home_logo = tm1.select_one(".TM_Logo img").get("data-src")
                
                # Get Team 2 Data
                tm2 = m.select_one(".TM2")
                away = tm2.select_one(".TM_Name").text.strip()
                away_logo = tm2.select_one(".TM_Logo img").get("data-src")
                
                # Get Link
                article_url = m.select_one("a").get("href")
                
                all_matches.append({
                    "home_team": home,
                    "away_team": away,
                    "home_logo": home_logo,
                    "away_logo": away_logo,
                    "league": m.select_one(".TourName").text.strip() if m.select_one(".TourName") else "Live",
                    "time": m.select_one(".MT_Time").text.strip(),
                    "url": get_stream_url(article_url),
                    "label": short_label(home, away)
                })
            except Exception:
                continue
    except Exception as e:
        print(f"❌ Error scraping yallashots: {e}")
        
    return all_matches