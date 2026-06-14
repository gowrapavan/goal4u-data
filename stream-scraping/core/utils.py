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
    h = re.sub(r'[^a-z]', '', home.lower())[:3] or home.lower()[:3]
    a = re.sub(r'[^a-z]', '', away.lower())[:3] or away.lower()[:3]
    return f"{h}-{a}"