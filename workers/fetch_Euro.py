#!/usr/bin/env python3
"""
workers/fetch_Euro.py — All-in-one UEFA Euro data fetcher.

Writes to:  data/euros/euro-{year}/
    competitionInfo.json
    matches.json
    teams.json
    standing.json
    scorers.json

The year is always read from config.TOURNAMENT_YEARS["EC"] — never derived
from a league season string.

Mirrors fetch_worldCup.py exactly, just for code EC and a different
output directory prefix.

Usage:
    python -m workers.fetch_Euro             # year from config
    python -m workers.fetch_Euro --year 2028 # explicit override
    python -m workers.fetch_Euro --mode matches
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

sys.path.insert(0, ".")

from config import SCORERS_LIMIT  # type: ignore[import]
from workers.fetch_competitions import (
    flatten_competition_info,
    flatten_tournament_standings,
    flatten_scorers,
)
from workers.fetch_matches import fetch_matches_for_competition
from workers.fetch_teams import fetch_teams_for_competition
from workers.tournament_paths import get_data_paths, get_display_title, get_tournament_year
from workers.utils import fetch, safe_write
from workers.audit_matches import audit_matches_for_tournament

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_Euro")

CODE = "EC"


# ── Section fetchers ──────────────────────────────────────────────────────────

def fetch_euro_competition_info(year: int, paths: dict) -> None:
    raw = fetch(f"/competitions/{CODE}")
    if not raw:
        logger.warning("EC: could not fetch competition info")
        return
    safe_write(paths["competition_info"], flatten_competition_info(raw))
    logger.info("Euro %d: wrote competitionInfo.json", year)


def fetch_euro_teams(year: int, paths: dict) -> None:
    n = fetch_teams_for_competition(CODE, api_season=year, paths=paths)
    logger.info("Euro %d: wrote %d teams", year, n)


def fetch_euro_matches(year: int, paths: dict) -> None:
    matches = fetch_matches_for_competition(CODE, year, paths=paths)
    logger.info("Euro %d: wrote %d matches", year, len(matches))


def fetch_euro_standing(year: int, paths: dict) -> None:
    raw = fetch(f"/competitions/{CODE}/standings", params={"season": year})
    if not raw:
        logger.warning("Euro %d: no standings data", year)
        return
    flattened = flatten_tournament_standings(raw, CODE, paths["matches"])
    if flattened:
        flattened["display_title"] = get_display_title(CODE, year)
        safe_write(paths["standing"], flattened)
        logger.info("Euro %d: wrote standing.json", year)


def fetch_euro_scorers(year: int, paths: dict) -> None:
    params = {"limit": SCORERS_LIMIT, "season": year}
    raw = fetch(f"/competitions/{CODE}/scorers", params=params)
    if not raw:
        logger.warning("Euro %d: no scorers data", year)
        return
    flattened = flatten_scorers(raw, CODE)
    if flattened:
        flattened["display_title"] = get_display_title(CODE, year)
        safe_write(paths["scorers"], flattened)
        logger.info("Euro %d: wrote scorers.json", year)


def audit_euro_matches(year: int, paths: dict, lookback_hours: int = 168) -> int:
    """
    Incremental match audit for UEFA Euro.

    Re-fetches only stale matches instead of downloading the full match list.
    Returns the number of matches actually updated.
    """
    stale, updated, skipped = audit_matches_for_tournament(
        CODE, paths, lookback_hours=lookback_hours,
    )
    logger.info(
        "Euro %d audit: stale=%d  updated=%d  skipped=%d",
        year, stale, updated, skipped,
    )
    return updated


# ── Main runner ───────────────────────────────────────────────────────────────

def run(year: Optional[int] = None, mode: str = "all", lookback_hours: int = 168) -> None:
    """
    Fetch UEFA Euro data.

    year          : Override the tournament year from config.TOURNAMENT_YEARS["EC"].
    mode          : "all" | "audit" | "info" | "teams" | "matches" | "standings" | "scorers"
    lookback_hours: How far back to consider matches stale in audit mode (default 168 = 7 days).

    audit mode:
        Re-fetches only stale matches, then refreshes standings + scorers
        only if any matches changed.
    """
    if year is None:
        year = get_tournament_year(CODE)

    paths = get_data_paths(CODE, tournament_year=year)
    logger.info("UEFA Euro %d — output root: %s  [mode=%s]", year, paths["root"], mode)

    if mode == "audit":
        updated = audit_euro_matches(year, paths, lookback_hours=lookback_hours)
        if updated > 0:
            logger.info("Euro %d: %d matches changed — refreshing standings + scorers", year, updated)
            fetch_euro_standing(year, paths)
            fetch_euro_scorers(year, paths)
        else:
            logger.info("Euro %d: no match changes — skipping standings/scorers refresh", year)
        logger.info("UEFA Euro %d — audit done.", year)
        return

    if mode in ("all", "info"):
        fetch_euro_competition_info(year, paths)

    if mode in ("all", "teams"):
        fetch_euro_teams(year, paths)

    if mode in ("all", "matches"):
        fetch_euro_matches(year, paths)

    if mode in ("all", "standings"):
        fetch_euro_standing(year, paths)

    if mode in ("all", "scorers"):
        fetch_euro_scorers(year, paths)

    logger.info("UEFA Euro %d — done.", year)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch all UEFA Euro data.")
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Tournament year (e.g. 2028). Default: from config.TOURNAMENT_YEARS['EC'].",
    )
    parser.add_argument(
        "--mode",
        choices=["all", "audit", "info", "teams", "matches", "standings", "scorers"],
        default="all",
        help="Which section to fetch. Use 'audit' for incremental updates during active tournament.",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=168,
        metavar="HOURS",
        help="Hours to look back for stale matches in audit mode (default: 168 = 7 days).",
    )
    args = parser.parse_args()
    run(year=args.year, mode=args.mode, lookback_hours=args.lookback)