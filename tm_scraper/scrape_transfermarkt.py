#!/usr/bin/env python3
"""
workers/scrape_transfermarkt.py

Scrapes the top 25 most valuable players from Transfermarkt, downloads their
profile pictures into an assets/ folder, and stores the full structured JSON.

Usage:
    python -m workers.scrape_transfermarkt
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup

sys.path.insert(0, ".")

from workers.cf_fetcher import fetch_html as _cf_fetch_html
from workers.utils import safe_write

logger = logging.getLogger("scrape_tm")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

URL = "https://www.transfermarkt.co.in/spieler-statistik/wertvollstespieler/marktwertetop"
ASSETS_DIR = Path("assets")


def _download_binary(url: str) -> bytes | None:
    """Download binary data safely using curl_cffi browser spoofing."""
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.get(
            url,
            impersonate="chrome120",
            timeout=15,
            headers={"Referer": "https://www.transfermarkt.co.in/"}
        )
        if resp.status_code == 200:
            return resp.content
    except ImportError:
        import requests
        try:
            resp = requests.get(
                url,
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.transfermarkt.co.in/"
                }
            )
            if resp.status_code == 200:
                return resp.content
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"Failed binary download for {url}: {e}")
    return None


def sanitize_filename(name: str) -> str:
    """Convert a name into a clean, safe lowercase file string."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9\s_-]", "", name)
    return re.sub(r"[\s_-]+", "_", name)


def parse_transfermarkt_table(html: str) -> list[dict]:
    """Parses the page, extracts data, and pulls image metadata."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="items")
    if not table:
        logger.error("Could not locate the main data table.")
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    rows = tbody.find_all("tr", recursive=False)
    players = []

    # Ensure assets directory tree exists at project root
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    for row in rows:
        try:
            # 1. Basic Metadata Extraction
            name_td = row.find("td", class_="hauptlink")
            if not name_td:
                continue
            player_name = name_td.text.strip()
            
            tds = row.find_all("td", recursive=False)
            age = tds[2].text.strip() if len(tds) > 2 else "Unknown"
            
            club_link = row.find("a", title=True, href=lambda href: href and "verein" in href)
            club_name = club_link.get("title") if club_link else "Unknown"

            value_td = row.find("td", class_="rechts hauptlink")
            market_value = value_td.text.strip() if value_td else "Unknown"

            # 2. Image URL Extraction
            img_el = row.find("img")
            img_url = img_el.get("src") if img_el else None
            
            image_local_path = None

            # 3. Handle Profile Photo Processing
            if img_url and ("portrait" in img_url or "spieler" in img_url):
                # Upgrade sizes from thumbnail to medium
                img_url = img_url.replace("small", "medium").split("?")[0]
                
                img_ext = ".jpg" if ".jpg" in img_url.lower() else ".png"
                file_slug = sanitize_filename(player_name)
                dest_path = ASSETS_DIR / f"{file_slug}{img_ext}"
                
                logger.info(f"Downloading portrait for {player_name}...")
                img_bytes = _download_binary(img_url)
                
                if img_bytes:
                    dest_path.write_bytes(img_bytes)
                    image_local_path = str(dest_path.as_posix())
                    logger.info(f" ✓ Saved image to {image_local_path}")
                else:
                    logger.warning(f" ✗ Could not download image for {player_name}")

            players.append({
                "name": player_name,
                "age": age,
                "club": club_name,
                "market_value": market_value,
                "image_path": image_local_path
            })
                
        except Exception as e:
            logger.warning(f"Skipping unparseable row item: {e}")
            continue

    return players


def run() -> None:
    logger.info("=========================================================")
    logger.info(" Processing Full Top 25 Transfermarkt Profiles & Assets")
    logger.info("=========================================================")
    
    html = _cf_fetch_html(URL, retries=2)
    if not html:
        logger.error("Fetch returned empty stream. Stopping.")
        return

    top_players = parse_transfermarkt_table(html)
    
    if top_players:
        output_target = "data/transfermarkt_valuable_players.json"
        safe_write(output_target, top_players)
        logger.info(f"==> Success! Stored JSON mapping tracking {len(top_players)} profiles inside {output_target}")
    else:
        logger.error("Extraction matrix returned 0 players.")


if __name__ == "__main__":
    run()