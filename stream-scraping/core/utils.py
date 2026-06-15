# D:\test\football\API\stream-scraping\core\utils.py
import pytz
from datetime import datetime
import re

IST = pytz.timezone("Asia/Kolkata")
GMT = pytz.timezone("GMT")

def convert_time(timestr, src_tz):
    """Convert HH:MM string from src timezone to IST with today's date."""
    now = datetime.now()
    dt = datetime.strptime(timestr, "%H:%M")
    dt = dt.replace(year=now.year, month=now.month, day=now.day)
    dt = src_tz.localize(dt).astimezone(IST)
    return dt.strftime("%Y-%m-%d %H:%M IST")

def short_label(home, away):
    """Generate short label like bri-man."""
    home_str = str(home or "")
    away_str = str(away or "")
    h = re.sub(r'[^a-z]', '', home_str.lower())[:3] or home_str.lower()[:3] or "hom"
    a = re.sub(r'[^a-z]', '', away_str.lower())[:3] or away_str.lower()[:3] or "awy"
    return f"{h}-{a}"