# scrapers/hesgoal.py
import requests
import base64
import re
import random
from datetime import datetime, timezone
from core.team_data import find_team_crest, load_team_data
from core.utils import short_label

API_BASE = "https://ws.kora-api.space"
P = 12  # The 'p' parameter seen in frame.php requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://hesgoals.eu/",
    "Accept-Language": "en-US,en;q=0.9",
}

# These are the fallback edge domains seen in the source
FALLBACK_FRAME_DOMAINS = [
    "vsys.kora-top.mov",
    "ar.kora-top.mov",
    "yalla.kora-top.mov",
]


def build_frame_url(ch_obj: dict, edge_domain: str, edges: list) -> str:
    """
    Replicate exactly what the site's JS does:
      getNextEdgeUrl() → https://{edge}.{edge_domain}/frame.php
    Then the iframe src becomes:
      {frame_url}?ch={ch}&p={p}&token={visitor_id}&kt={timestamp}
    """
    ch_key = ch_obj.get("ch", "")
    ch_edge = ch_obj.get("edge", "1")
    ch_link = ch_obj.get("link", "")
    ch_type = ch_obj.get("type", "Frame")

    # If type is HLS, the link itself IS the stream — use it directly
    if ch_type == "HLS" and ch_link:
        return ch_link

    # edge == 0 means use ch_link directly (no edge routing)
    if str(ch_edge) == "0" and ch_link:
        return ch_link

    # Build edge URL: pick a random edge from the edges array + edge_domain
    if edges and edge_domain:
        chosen_edge = random.choice(edges)
        base = f"https://{chosen_edge}.{edge_domain}/frame.php"
    else:
        # Fallback to the hardcoded domains in the site's JS
        base = f"https://{random.choice(FALLBACK_FRAME_DOMAINS)}/frame.php"

    # Final URL matches what the browser sends:
    # frame.php?ch=max5&p=12&token=<uuid>&kt=<unix_seconds>
    kt = int(datetime.now().timestamp())
    token = "7e829b3c-58ff-472b-91ec-a3ad7e3fd1b1"  # visitor_id equivalent
    return f"{base}?ch={ch_key}&p={P}&token={token}&kt={kt}"


def decode_m3u8_from_frame(frame_url: str) -> str | None:
    """
    Fetch the frame.php page and decode the base64 CONFIG.token
    to get the real .m3u8 stream URL.
    """
    try:
        # Must send Referer as sportscarinsurance.net — that's what the site checks
        headers = {
            **HEADERS,
            "Referer": "https://blog.sportscarinsurance.net/",
        }
        resp = requests.get(frame_url, headers=headers, timeout=8)
        if resp.status_code != 200:
            return None

        # Extract CONFIG.token value
        match = re.search(r'token:\s*["\']([A-Za-z0-9_\-]+)["\']', resp.text)
        if not match:
            return None

        raw = match.group(1)

        # URL-safe base64 decode (same as JS urlSafeBase64Decode)
        padding = 4 - len(raw) % 4
        if padding != 4:
            raw += "=" * padding
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")

        if ".m3u8" in decoded:
            return decoded

        return None

    except Exception as e:
        print(f"      ⚠️ Frame decode failed: {e}")
        return None


def scrape():
    load_team_data()
    all_matches = []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        resp = requests.get(
            f"{API_BASE}/api/matches/{today}/1",
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"❌ Match list API returned {resp.status_code}")
            return []
        match_list = resp.json().get("matches", [])
    except Exception as e:
        print(f"❌ Hesgoal API unreachable: {e}")
        return []

    print(f"   Found {len(match_list)} total matches")

    for m in match_list:
        # Skip finished (2)
        if m.get("status") == 2:
            continue

        match_id = m.get("id")

        try:
            detail_resp = requests.get(
                f"{API_BASE}/api/matche/{match_id}/en",
                headers=HEADERS,
                timeout=8,
            )
            d = detail_resp.json()
        except Exception as e:
            print(f"   ⚠️ Skipping match {match_id}: {e}")
            continue

        # Only process active matches with channels
        if not d.get("active") or not d.get("has_channels"):
            continue

        channels = d.get("channels", [])
        edge_domain = d.get("edge_domain", "")

        # edges array can be a JSON string or a list
        edges_raw = d.get("edges", [])
        if isinstance(edges_raw, str):
            try:
                import json
                edges = json.loads(edges_raw)
            except Exception:
                edges = []
        else:
            edges = edges_raw if isinstance(edges_raw, list) else []

        home = d.get("home_en", "Unknown")
        away = d.get("away_en", "Unknown")
        league = d.get("league_en", "")
        time_str = d.get("time", "")
        score = d.get("score", "")
        status = d.get("status", 0)
        home_logo = find_team_crest(home)
        away_logo = find_team_crest(away)

        for ch in channels:
            server_name = ch.get("server_name", ch.get("server_name_en", "Server"))
            ch_type = ch.get("type", "Frame")

            # Build the proper frame/stream URL
            frame_url = build_frame_url(ch, edge_domain, edges)

            # Try to extract the real m3u8 from the frame page
            m3u8_url = None
            if ch_type != "HLS" and str(ch.get("edge", "1")) != "0":
                m3u8_url = decode_m3u8_from_frame(frame_url)
                if m3u8_url:
                    print(f"   ✅ Got m3u8 for {home} vs {away} — {server_name}")

            # For HLS type, frame_url IS the m3u8
            if ch_type == "HLS":
                m3u8_url = frame_url

            all_matches.append({
                "match_id": match_id,
                "home_team": home,
                "away_team": away,
                "home_logo": home_logo,
                "away_logo": away_logo,
                "league": league,
                "time": time_str,
                "score": score,
                "status": status,
                "server_name": server_name,
                "stream_type": ch_type,
                # Use m3u8 if we got it, else fall back to frame URL
                "url": m3u8_url if m3u8_url else frame_url,
                "m3u8": m3u8_url,        # direct playable stream (or None)
                "frame_url": frame_url,   # always available as fallback
                "label": f"{short_label(home, away)}-{server_name}",
            })

    print(f"✅ Hesgoal: {len(all_matches)} stream entries")
    return all_matches