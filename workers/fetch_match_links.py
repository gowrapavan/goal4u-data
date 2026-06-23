#!/usr/bin/env python3
"""
workers/fetch_match_links.py
──────────────────────────────────────────────────────────────────────────────
Scrapes the yallashoot.soccer competition page to collect all match URLs,
then cross-references them against the local matches.json (from football-data)
to produce match_stats_links.json — the index used by fetch_match_stats.py.

Output: {root}/match_stats_links.json
Schema:
    {
      "_meta": {
        "last_synced": "...",
        "competition": "PL",
        "season": "2025-2026",
        "total_urls": 380,
        "matched": 371,
        "unmatched": 9
      },
      "data": [
        {
          "match_id":  494130,       ← fd match ID (null if no match found)
          "date":      "2025-08-17",
          "home_team": "Arsenal",
          "away_team": "Brighton",
          "url":  "https://yallashoot.soccer/live/arsenal-brighton-2025-08-17/",
          "slug": "arsenal-brighton-2025-08-17"
        },
        ...
      ]
    }

Usage:
    python fetch_match_links.py --competition PL
    python fetch_match_links.py --competition WC
    python fetch_match_links.py --all
    python fetch_match_links.py --competition PL --season 2024-2025
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

sys.path.insert(0, ".")

from workers.cf_fetcher import fetch_html as _cf_fetch_html
from workers.tournament_paths import (
    get_data_paths,
    get_display_title,
    get_league_yallashoot_slug,
    get_tournament_year,
    get_yallashoot_slug,
    is_tournament,
)
from config import TRACKED_COMPETITIONS, get_season_paths

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_match_links")

# ── Season ────────────────────────────────────────────────────────────────────
_paths = get_season_paths()
SEASON = _paths["season"]


# ── URL builder ───────────────────────────────────────────────────────────────

def get_competition_url(code: str, season: str) -> str | None:
    """
    Return the yallashoot competition page URL for a given code.

    Tournaments: uses get_yallashoot_slug(code, year: int)
    Leagues:     uses get_league_yallashoot_slug(code, season: str)
    """
    code = code.upper()

    if is_tournament(code):
        year = get_tournament_year(code)
        slug = get_yallashoot_slug(code, year)
    else:
        slug = get_league_yallashoot_slug(code, season)

    if not slug:
        logger.warning("[links] No yallashoot slug for %s — skipping", code)
        return None

    return f"https://yallashoot.soccer/competition/{slug}/"


# ── HTML scraping ─────────────────────────────────────────────────────────────

def extract_match_urls(html: str) -> list[dict]:
    """
    Extract all /live/{slug}-{date}/ URLs from the competition page HTML.
    Returns a list of dicts: { url, date, slug }
    """
    pattern = re.compile(
        r'https://yallashoot\.soccer/live/([\w\-]+)-(\d{4}-\d{2}-\d{2})/',
        re.IGNORECASE,
    )
    seen: set[str] = set()
    results: list[dict] = []
    for m in pattern.finditer(html):
        url  = m.group(0)
        slug = m.group(1)
        date = m.group(2)
        if url not in seen:
            seen.add(url)
            results.append({"url": url, "date": date, "slug": slug})
    return results


# ── Name normalisation for fuzzy matching ─────────────────────────────────────

_CHAR_MAP = str.maketrans({
    "ç": "c", "ć": "c", "č": "c",
    "é": "e", "è": "e", "ê": "e",
    "á": "a", "à": "a", "â": "a",
    "í": "i", "ì": "i", "î": "i",
    "ó": "o", "ò": "o", "ô": "o",
    "ú": "u", "ù": "u", "û": "u",
    "ñ": "n", "ß": "ss",
    "ø": "o", "å": "a", "æ": "ae",
})

_ALIASES: dict[str, str] = {
    "wolverhampton wanderers": "wolves",
    "tottenham hotspur": "spurs",
    "fc internazionale milano": "inter",
    "inter milan": "inter",
    "olympique lyonnais": "lyon",
    "olympique de marseille": "marseille",
    "stade rennais fc 1901": "rennes",
    "racing club de lens": "lens",
    "paris saint germain": "psg",
    "atletico de madrid": "atletico madrid",
    "club atletico de madrid": "atletico madrid",
    "real betis balompie": "real betis",
    "deportivo alaves": "alaves",
}

_NOISE = [" fc", " afc", " cf", " sc", " club", " united", " city", " hotspur"]


def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = name.lower().translate(_CHAR_MAP)
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = " ".join(name.split())
    for k, v in _ALIASES.items():
        if k in name:
            name = name.replace(k, v)
    for noise in _NOISE:
        name = name.replace(noise, "")
    return " ".join(name.split())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def find_best_match(ys_entry: dict, fd_matches: list[dict]) -> dict | None:
    """
    Find the football-data.org match that corresponds to a yallashoot URL entry,
    using date + fuzzy team-name matching on the slug.

    Returns the fd match dict if similarity >= 0.55, else None.
    """
    date = ys_entry["date"]
    slug = ys_entry["slug"].replace("-", " ")

    best_score = 0.0
    best_match = None

    for m in fd_matches:
        if m.get("utcDate", "")[:10] != date:
            continue
        home = (m.get("homeTeam") or {}).get("name") or ""
        away = (m.get("awayTeam") or {}).get("name") or ""
        score = max(
            _similarity(slug, f"{home} {away}"),
            _similarity(slug, f"{away} {home}"),
            _similarity(slug, home),
            _similarity(slug, away),
        )
        if score > best_score:
            best_score, best_match = score, m

    if best_score >= 0.55:
        return best_match

    logger.debug(
        "[links] No match for slug '%s' on %s (best=%.2f)", slug, date, best_score
    )
    return None


# ── Safe write ────────────────────────────────────────────────────────────────

def _safe_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ── Core logic ────────────────────────────────────────────────────────────────

def process_one(competition: str, season_str: str | None = None) -> None:
    """
    Scrape yallashoot for *competition*, match URLs against matches.json,
    and write match_stats_links.json.
    """
    s = season_str or SEASON
    url = get_competition_url(competition, s)
    if not url:
        return

    display = get_display_title(competition)
    logger.info("[links] Fetching %s competition page: %s", display, url)

    html = _cf_fetch_html(url, retries=2)
    if not html:
        logger.error("[links] Failed to fetch HTML for %s", competition)
        return

    ys_entries = extract_match_urls(html)
    logger.info("[links] Found %d match URLs on %s page", len(ys_entries), display)

    # Load fd matches for cross-referencing
    paths = get_data_paths(competition, season=s)
    fd_matches: list[dict] = []
    try:
        with open(paths["matches"], "r", encoding="utf-8") as f:
            fd_matches = json.load(f).get("data", [])
        logger.info("[links] Loaded %d fd matches for matching", len(fd_matches))
    except FileNotFoundError:
        logger.warning(
            "[links] matches.json not found for %s — match_id will be null", competition
        )
    except Exception as exc:
        logger.warning("[links] Could not read matches.json: %s", exc)

    # Cross-reference ys URLs → fd match IDs
    linked: list[dict] = []
    matched_count = 0

    for entry in ys_entries:
        fd = find_best_match(entry, fd_matches)
        if fd:
            matched_count += 1
            linked.append({
                "match_id":  fd["id"],
                "date":      entry["date"],
                "home_team": (fd.get("homeTeam") or {}).get("name"),
                "away_team": (fd.get("awayTeam") or {}).get("name"),
                "url":       entry["url"],
                "slug":      entry["slug"],
            })
        else:
            linked.append({
                "match_id":  None,
                "date":      entry["date"],
                "home_team": None,
                "away_team": None,
                "url":       entry["url"],
                "slug":      entry["slug"],
            })

    logger.info(
        "[links] %s: %d/%d URLs matched to fd matches",
        display, matched_count, len(ys_entries),
    )

    payload = {
        "_meta": {
            "last_synced": datetime.now(timezone.utc).isoformat(),
            "competition": competition,
            "season":      s,
            "total_urls":  len(ys_entries),
            "matched":     matched_count,
            "unmatched":   len(ys_entries) - matched_count,
        },
        "data": linked,
    }

    _safe_write(paths["match_stats_links"], payload)
    logger.info("[links] Written → %s", paths["match_stats_links"])


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch yallashoot match links for a competition."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--competition", "-c", help="Competition code e.g. PL, WC")
    group.add_argument("--all", action="store_true", help="Process all tracked competitions")
    parser.add_argument(
        "--season", "-s",
        help="Season string e.g. 2025-2026 (optional; overrides env SEASON)"
    )
    args = parser.parse_args()

    season_override = args.season or os.environ.get("SEASON") or None

    if args.all:
        for comp in TRACKED_COMPETITIONS:
            process_one(comp, season_str=season_override)
            time.sleep(2)
    else:
        process_one(args.competition, season_str=season_override)


if __name__ == "__main__":
    main()
