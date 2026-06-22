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
import sys
import time
from difflib import SequenceMatcher

sys.path.insert(0, ".")  # allow running as: python workers/fetch_match_links.py

# Unified Cloudflare bypass fetcher (Playwright → curl_cffi → free proxy)
from workers.cf_fetcher import fetch_html as _cf_fetch_html

# Pull season + competition list from the single source of truth so this
# script updates automatically when config.py changes — no manual edit needed.
from config import TRACKED_COMPETITIONS, get_season_paths

_paths   = get_season_paths(os.environ.get("SEASON"))
SEASON   = _paths["season"]          # e.g. "2025-2026"
DATA_DIR = "data"

# Use the same list as every other worker — no separate definition here.
COMPETITIONS = TRACKED_COMPETITIONS


def get_competition_url(code: str, season: str) -> str | None:
    """
    Dynamically generates the YallaShoot competition URL based on the SEASON.
    If SEASON is "2025-2026", it fetches the 2025-2026 URL automatically.
    """
    # World cup uses just the ending year (e.g., 2026)
    year_end = season.split("-")[-1]

    # League competitions use the full season slug (e.g. "2026-2027").
    # Cup tournaments (WC, EC) happen in a single calendar year and use
    # just the end year (e.g. "world-cup-2026", "euros-2028").
    # If the competition runs in the second half of the season (e.g. WC 2026
    # is in season folder "2026-2027"), year_end is the correct suffix.
    year_start = season.split("-")[0]
    bases = {
        "WC":  f"world-cup-{year_end}",
        "EC":  f"uefa-euro-{year_end}",          # UEFA Euros — runs every 4 years
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
    """Download HTML from YallaShoot using the CF bypass cascade.

    Tries (in order):
      1. Playwright + stealth  — solves JS challenges (required on GitHub Actions)
      2. curl_cffi             — fast TLS impersonation (good locally)
      3. Free rotating proxies — last-resort fallback

    See workers/cf_fetcher.py for implementation details.
    """
    print(f"  [Fetching] {url}")
    html = _cf_fetch_html(url, retries=2)
    if not html:
        print(f"  ✗ All fetch methods failed for {url}")
    return html


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
    r"""
    Extract all unique YallaShoot match URLs.
    Example: https://yallashoot.soccer/live/liverpool-bournemouth-2025-08-15/

    NOTE: The slug character class includes unicode word chars (\w) so that
    teams with accented names (Curaçao → curacao, Côte d'Ivoire, etc.) are
    captured correctly regardless of how YallaShoot encodes them in the URL.
    """
    pattern = re.compile(
        r'https://yallashoot\.soccer/live/([\w\-]+)-(\d{4}-\d{2}-\d{2})/',
        re.IGNORECASE | re.UNICODE,
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

    # Normalize unicode accented characters to their ASCII equivalents
    # so slugs like "curacao" match API names like "Curaçao"
    _unicode_map = str.maketrans({
        "ç": "c", "ć": "c", "č": "c",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "á": "a", "à": "a", "â": "a", "ä": "a", "ã": "a",
        "í": "i", "ì": "i", "î": "i", "ï": "i",
        "ó": "o", "ò": "o", "ô": "o", "ö": "o", "õ": "o",
        "ú": "u", "ù": "u", "û": "u", "ü": "u",
        "ñ": "n", "ß": "ss", "ø": "o", "å": "a",
    })
    name = name.translate(_unicode_map)

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
        "fk kairat": "kairat almaty",
        # WC-specific names that differ between API and YallaShoot slugs
        "curacao": "curacao",          # API: "Curaçao" → already normalized above
        "ivory coast": "ivory coast",  # API: "Côte d'Ivoire" → normalized above
        "cote d ivoire": "ivory coast",
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


def load_fd_matches(competition: str, season_str: str | None = None) -> list[dict]:
    s = season_str or SEASON
    path = os.path.join(DATA_DIR, s, "matches", f"{competition}.json")
    data = load_json(path)
    if not data:
        return []
    return data.get("data", [])


def save_match_links(competition: str, records: list[dict], season_str: str | None = None):
    """Creates the directory if it doesn't exist and safely writes the JSON file."""
    s = season_str or SEASON
    out_path = os.path.join(DATA_DIR, s, "match-links", f"{competition}.json")

    # Ensure the target directory exists
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Write the data with the {"data": [...]} envelope expected by the stats scraper
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"data": records}, f, indent=2, ensure_ascii=False)

    print(f"  ✓ Saved {len(records)} matches to {out_path}")


# ── CLI entry point ───────────────────────────────────────────────────────────

def process_one(competition: str, season_str: str | None = None):
    s = season_str or SEASON
    url = get_competition_url(competition, s)
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

    fd_matches = load_fd_matches(competition, season_str=s)
    print(f"  Loaded {len(fd_matches)} football-data matches")

    records = build_match_links(ys_entries, fd_matches)
    save_match_links(competition, records, season_str=s)


def main():
    parser = argparse.ArgumentParser(description="Extract & map YallaShoot match URLs dynamically")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--competition", help="Competition code, e.g. PL")
    group.add_argument("--all",         action="store_true", help="Fetch links for all competitions")

    args = parser.parse_args()

    if args.all:
        for comp in COMPETITIONS:
            process_one(comp)
            time.sleep(2)  # Be polite to YallaShoot servers between large requests
    else:
        process_one(args.competition)


if __name__ == "__main__":
    main()