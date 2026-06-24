#!/usr/bin/env python3
"""
workers/fetch_worldCup.py — All-in-one World Cup data fetcher.

Writes to:  data/world-cup/world-cup-{year}/
    competitionInfo.json
    matches.json
    teams.json
    standing.json
    scorers.json

The year is always read from config.TOURNAMENT_YEARS["WC"] — never derived
from a league season string.

This script is intentionally self-contained: it does not share any
if-is_tournament() branch with the league fetchers.  Clean separation was
one of the key requirements from the architecture notes.

Usage:
    python -m workers.fetch_worldCup             # year from config
    python -m workers.fetch_worldCup --year 2026 # explicit override
    python -m workers.fetch_worldCup --mode matches  # one section only
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
logger = logging.getLogger("fetch_worldCup")

CODE = "WC"


# ── Section fetchers ──────────────────────────────────────────────────────────

def fetch_wc_competition_info(year: int, paths: dict) -> None:
    raw = fetch(f"/competitions/{CODE}")
    if not raw:
        logger.warning("WC: could not fetch competition info")
        return
    safe_write(paths["competition_info"], flatten_competition_info(raw))
    logger.info("WC %d: wrote competitionInfo.json", year)


def fetch_wc_teams(year: int, paths: dict) -> None:
    """
    Fetch the WC squad.
    The WC API season parameter is the tournament year itself (e.g. 2026).
    """
    n = fetch_teams_for_competition(CODE, api_season=year, paths=paths)
    logger.info("WC %d: wrote %d teams", year, n)


def fetch_wc_matches(year: int, paths: dict) -> None:
    """Fetch all WC matches for the tournament year."""
    matches = fetch_matches_for_competition(CODE, year, paths=paths)
    logger.info("WC %d: wrote %d matches", year, len(matches))


def fetch_wc_standing(year: int, paths: dict) -> None:
    raw = fetch(f"/competitions/{CODE}/standings", params={"season": year})
    if not raw:
        logger.warning("WC %d: no standings data", year)
        return
    flattened = flatten_tournament_standings(raw, CODE, paths["matches"])
    if flattened:
        # Inject the year into the display title (e.g. "FIFA World Cup 2026")
        flattened["display_title"] = get_display_title(CODE, year)
        safe_write(paths["standing"], flattened)
        logger.info("WC %d: wrote standing.json", year)


def fetch_wc_scorers(year: int, paths: dict) -> None:
    params = {"limit": SCORERS_LIMIT, "season": year}
    raw = fetch(f"/competitions/{CODE}/scorers", params=params)
    if not raw:
        logger.warning("WC %d: no scorers data", year)
        return
    flattened = flatten_scorers(raw, CODE)
    if flattened:
        flattened["display_title"] = get_display_title(CODE, year)
        safe_write(paths["scorers"], flattened)
        logger.info("WC %d: wrote scorers.json", year)


def audit_wc_matches(year: int, paths: dict, lookback_hours: int = 168) -> int:
    """
    Incremental match audit for the World Cup.

    Re-fetches only stale matches (IN_PLAY, PAUSED, POSTPONED, recently
    finished without full data) instead of downloading all 104 matches.

    Returns the number of matches actually updated.
    """
    stale, updated, skipped = audit_matches_for_tournament(
        CODE, paths, lookback_hours=lookback_hours,
    )
    logger.info(
        "WC %d audit: stale=%d  updated=%d  skipped=%d",
        year, stale, updated, skipped,
    )
    return updated


# ── Main runner ───────────────────────────────────────────────────────────────

def run(year: Optional[int] = None, mode: str = "all", lookback_hours: int = 168) -> None:
    """
    Fetch World Cup data.

    year          : Override the tournament year from config.TOURNAMENT_YEARS["WC"].
    mode          : "all" | "audit" | "info" | "teams" | "matches" | "standings" | "scorers"
    lookback_hours: How far back to consider matches stale in audit mode (default 168 = 7 days).

    audit mode:
        Re-fetches only stale matches (live, postponed, recently finished
        without full data). Then refreshes standings + scorers only if any
        matches changed. Much cheaper than "all" during an active tournament.
    """
    if year is None:
        year = get_tournament_year(CODE)

    paths = get_data_paths(CODE, tournament_year=year)
    logger.info("World Cup %d — output root: %s  [mode=%s]", year, paths["root"], mode)

    if mode == "audit":
        updated = audit_wc_matches(year, paths, lookback_hours=lookback_hours)
        if updated > 0:
            logger.info("WC %d: %d matches changed — refreshing standings + scorers", year, updated)
            fetch_wc_standing(year, paths)
            fetch_wc_scorers(year, paths)
        else:
            logger.info("WC %d: no match changes — skipping standings/scorers refresh", year)
        logger.info("World Cup %d — audit done.", year)
        return

    if mode in ("all", "info"):
        fetch_wc_competition_info(year, paths)

    if mode in ("all", "teams"):
        fetch_wc_teams(year, paths)

    if mode in ("all", "matches"):
        fetch_wc_matches(year, paths)

    if mode in ("all", "standings"):
        fetch_wc_standing(year, paths)

    if mode in ("all", "scorers"):
        fetch_wc_scorers(year, paths)

    logger.info("World Cup %d — done.", year)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch all World Cup data.")
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Tournament year (e.g. 2026). Default: from config.TOURNAMENT_YEARS['WC'].",
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