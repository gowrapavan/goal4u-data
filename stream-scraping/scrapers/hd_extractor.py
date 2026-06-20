# core/hd_extractor.py
"""
Pulls direct .m3u8 stream links out of any scraper's match list.

Most scrapers put their single best link in match["url"]. koraaclub.py is
special: it also exposes match["servers"], a list of alternate stream
options (some "media" / m3u8, some "iframe" / not directly playable). This
checks both, but does NOT inspect "article_url" or "frame_url" fields --
those are pages to load, not stream links, even if they happen to contain
the substring ".m3u8" somewhere in a query string.
"""


def is_m3u8(url):
    return bool(url) and ".m3u8" in url.lower()


def extract_m3u8_matches(source_name, matches):
    """
    Scan a list of match dicts (as returned by any scraper's scrape/fetch
    function) and return every direct .m3u8 link found, flattened into:
        {source, home_team, away_team, label, channel, server_label, url}
    server_label is only present for links found inside match["servers"].
    """
    found = []

    for m in matches or []:
        home = m.get("home_team")
        away = m.get("away_team")
        label = m.get("label")
        channel = m.get("channel") or m.get("league") or ""

        primary_url = m.get("url")
        if is_m3u8(primary_url):
            found.append({
                "source": source_name,
                "home_team": home,
                "away_team": away,
                "label": label,
                "channel": channel,
                "url": primary_url,
            })

        for server in m.get("servers", []) or []:
            server_url = server.get("url")
            if is_m3u8(server_url):
                found.append({
                    "source": source_name,
                    "home_team": home,
                    "away_team": away,
                    "label": label,
                    "channel": channel,
                    "server_label": server.get("label"),
                    "url": server_url,
                })

    return found