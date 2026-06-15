#!/usr/bin/env python3
"""
fetch_match_links.py

Extracts YallaShoot match URLs directly from their live competition pages,
maps them to football-data match IDs, and stores:
    data/{season}/match-links/{competition}.json

Usage:
    python workers/fetch_match_links.py --competition PL
    python workers/fetch_match_links.py --competition PD
    python workers/fetch_match_links.py --all
"""

import argparse
import re
import os
import json
import time
import requests
from difflib import SequenceMatcher

# Only import safe_write from utils
from utils import safe_write

# Define constants locally since utils.py is path-agnostic
SEASON = "2025-2026"
DATA_DIR = "data"

# List of supported competitions
COMPETITIONS = ["PL", "PD", "SA", "FL1", "BL1", "CL", "WC"]

def get_competition_url(code: str, season: str) -> str | None:
    """
    Dynamically generates the YallaShoot competition URL based on the SEASON.
    If SEASON is "2025-2026", it fetches the 2025-2026 URL automatically.
    """
    # World cup uses just the ending year (e.g., 2026)
    year_end = season.split("-")[-1]
    
    bases = {
        "WC":  f"world-cup-{year_end}",
        "PL":  f"english-premier-league-{season}",
        "PD":  f"la-liga-spain-{season}",
        "SA":  f"serie-a-italy-{season}",
        "FL1": f"ligue-1-france-{season}",
        "BL1": f"bundesliga-germany-{season}",
        "CL":  f"uefa-champions-league-{season}",
    }
    
    if code not in bases:
        return None
        
    return f"https://yallashoot.soccer/competition/{bases[code]}/"

# ── HTTP & I/O Helpers ────────────────────────────────────────────────────────

def fetch_html(url: str) -> str | None:
    """Download HTML from YallaShoot with basic bot protection bypass."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp.text
            print(f"  [Warn] HTTP {resp.status_code} on {url}. Retrying...")
            time.sleep(3)
        except requests.RequestException as e:
            print(f"  [Warn] Request failed: {e}. Retrying...")
            time.sleep(3)
            
    return None

def load_json(path: str) -> dict | None:
    """Helper to safely load a JSON file."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None

# ── URL extraction ────────────────────────────────────────────────────────────

def extract_match_urls(html: str) -> list[dict]:
    """
    Extract all unique YallaShoot match URLs.
    Example: https://yallashoot.soccer/live/liverpool-bournemouth-2025-08-15/
    """
    pattern = re.compile(
        r'https://yallashoot\.soccer/live/([a-z0-9\-]+)-(\d{4}-\d{2}-\d{2})/',
        re.IGNORECASE,
    )

    seen = set()
    results = []

    for m in pattern.finditer(html):
        slug = m.group(1)
        date = m.group(2)
        url = m.group(0)

        if url in seen:
            continue
        seen.add(url)

        results.append({
            "url": url,
            "date": date,
            "slug": slug,
        })

    return results

# ── Match mapping ─────────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Normalize team names to improve fuzzy matching accuracy."""
    if not name:
        return ""
        
    name = name.lower()
    
    # Replace special characters with spaces
    name = re.sub(r"[^a-z0-9]", " ", name)
    name = " ".join(name.split()).strip()
    
    # Map long formal API names to their short URL counterparts FIRST
    aliases = {
        "wolverhampton wanderers": "wolves",
        "tottenham hotspur": "spurs",
        "fc internazionale milano": "inter",
        "internazionale milano": "inter",
        "inter milan": "inter",
        "olympique lyonnais": "lyon",
        "olympique de marseille": "marseille",
        "stade rennais fc 1901": "rennes",
        "stade rennais": "rennes",
        "racing club de lens": "lens",
        "ogc nice": "nice",
        "as saint etienne": "saint etienne",
        "hellas verona": "verona",
        "ssc napoli": "napoli",
        "paphos": "pafos",
        "qarabag agdam": "qarabag",
        "fk bodo glimt": "bodoglimt",
        "bodo glimt": "bodoglimt",
        "fk kairat": "kairat almaty"
    }
    
    for k, v in aliases.items():
        if name == k or k in name:
            name = name.replace(k, v)
            
    # Then remove common football suffixes
    remove = [" fc", " afc", " cf", " sc", " club", " united", " city"]
    for r in remove:
        name = name.replace(r, "")
        
    return " ".join(name.split()).strip()

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()

def find_best_match(ys_entry: dict, fd_matches: list[dict]) -> dict | None:
    """
    Map one YallaShoot URL entry to the best football-data match.
    Matches by date first, then fuzzy matches the entire URL slug
    against combinations of home/away team names.
    """
    date = ys_entry["date"]
    slug = ys_entry["slug"].replace("-", " ")

    best_score = 0.0
    best_match = None

    for m in fd_matches:
        fd_date = m.get("utcDate", "")[:10]
        if fd_date != date:
            continue

        # Safely extract team names to prevent 'NoneType' crashes
        home_team = m.get("homeTeam") or {}
        away_team = m.get("awayTeam") or {}
        
        home = home_team.get("name") or ""
        away = away_team.get("name") or ""

        # Test multiple combinations for the highest score
        score = max(
            similarity(slug, f"{home} {away}"),
            similarity(slug, f"{away} {home}"),
            similarity(slug, home),
            similarity(slug, away),
        )

        if score > best_score:
            best_score = score
            best_match = m

    if best_score >= 0.55:
        return best_match
    return None

def build_match_links(ys_entries: list[dict], fd_matches: list[dict]) -> list[dict]:
    """Merge YallaShoot URLs with football-data match metadata."""
    linked = []

    for i, entry in enumerate(ys_entries):
        fd = find_best_match(entry, fd_matches)
        
        if fd:
            linked.append({
                "match_id":  fd["id"],
                "date":      entry["date"],
                "home_team": fd["homeTeam"]["name"],
                "away_team": fd["awayTeam"]["name"],
                "url":       entry["url"],
                "slug":      entry["slug"],
            })
            print(f"  ✓ [{i+1}/{len(ys_entries)}] {fd['homeTeam']['name']} vs {fd['awayTeam']['name']}")
        else:
            linked.append({
                "match_id":  None,
                "date":      entry["date"],
                "home_team": None,
                "away_team": None,
                "url":       entry["url"],
                "slug":      entry["slug"],
            })
            print(f"  ✗ [{i+1}/{len(ys_entries)}] Unmatched: {entry['slug']} on {entry['date']}")

    return linked

def load_fd_matches(competition: str) -> list[dict]:
    path = os.path.join(DATA_DIR, SEASON, "matches", f"{competition}.json")
    data = load_json(path)
    if not data:
        return []
    return data.get("data", [])

def save_match_links(competition: str, records: list[dict]):
    out_path = os.path.join(DATA_DIR, SEASON, "match-links", f"{competition}.json")
    safe_write(out_path, records)

# ── CLI entry point ───────────────────────────────────────────────────────────

def process_one(competition: str):
    url = get_competition_url(competition, SEASON)
    if not url:
        print(f"  ✗ Error: No URL configured for competition code '{competition}'")
        return

    print(f"\n[{competition}] Fetching live HTML from {url} ...")
    html = fetch_html(url)
    
    if not html:
        print(f"  ✗ Error: Failed to download HTML for {competition}.")
        return

    ys_entries = extract_match_urls(html)
    print(f"  Found {len(ys_entries)} YallaShoot URLs")

    fd_matches = load_fd_matches(competition)
    print(f"  Loaded {len(fd_matches)} football-data matches")

    records = build_match_links(ys_entries, fd_matches)
    save_match_links(competition, records)

def main():
    parser = argparse.ArgumentParser(description="Extract & map YallaShoot match URLs dynamically")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--competition", help="Competition code, e.g. PL")
    group.add_argument("--all",         action="store_true", help="Fetch links for all competitions")

    args = parser.parse_args()

    if args.all:
        for comp in COMPETITIONS:
            process_one(comp)
            time.sleep(2) # Be polite to YallaShoot servers between large requests
    else:
        process_one(args.competition)

if __name__ == "__main__":
    main()