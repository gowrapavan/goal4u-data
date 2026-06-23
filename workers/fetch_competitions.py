#!/usr/bin/env python3
"""
workers/fetch_competitions.py — League competition metadata fetcher.

Writes per-competition files into data/{season}/{CODE}/:
    competitionInfo.json   ← competition + current season metadata
    standing.json          ← league table
    topScorer.json         ← top goal-scorers

Does NOT touch tournament codes (WC, EC) — those are handled by
fetch_worldCup.py and fetch_Euro.py which have their own all-in-one flow.

Usage (standalone):
    python -m workers.fetch_competitions                    # current season, all leagues
    python -m workers.fetch_competitions --season 2024     # historical 2024-2025
    python -m workers.fetch_competitions --competition PL  # one competition only
    python -m workers.fetch_competitions --mode standings  # standings only
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

sys.path.insert(0, ".")

from config import (  # type: ignore[import]
    COMPETITION_STRIP_FIELDS,
    LEAGUE_COMPETITIONS,
    SCORERS_LIMIT,
    get_current_season_start_year,
    get_season_paths,
)
from workers.tournament_paths import get_data_paths, get_display_title, is_tournament
from workers.utils import fetch, safe_write, strip_fields

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_competitions")


# ── Flatten helpers ───────────────────────────────────────────────────────────

def flatten_competition_info(raw: dict) -> dict:
    """Flatten raw /competitions/{code} response into competitionInfo shape."""
    raw = dict(raw)
    strip_fields(raw, COMPETITION_STRIP_FIELDS)
    area           = raw.get("area") or {}
    current_season = raw.get("currentSeason") or {}
    return {
        "id":      raw.get("id"),
        "name":    raw.get("name"),
        "code":    raw.get("code"),
        "type":    raw.get("type"),
        "emblem":  raw.get("emblem"),
        "area": {
            "id":   area.get("id"),
            "name": area.get("name"),
            "code": area.get("code"),
            "flag": area.get("flag"),
        },
        "currentSeason": {
            "id":               current_season.get("id"),
            "startDate":        current_season.get("startDate"),
            "endDate":          current_season.get("endDate"),
            "currentMatchday":  current_season.get("currentMatchday"),
            "winner":           current_season.get("winner"),
        },
    }


def flatten_standing_row(row: dict) -> dict:
    team = row.get("team") or {}
    return {
        "position":      row.get("position"),
        "team": {
            "id":        team.get("id"),
            "name":      team.get("name"),
            "shortName": team.get("shortName"),
            "tla":       team.get("tla"),
            "crest":     team.get("crest"),
        },
        "playedGames":   row.get("playedGames"),
        "form":          row.get("form"),
        "won":           row.get("won"),
        "draw":          row.get("draw"),
        "lost":          row.get("lost"),
        "points":        row.get("points"),
        "goalsFor":      row.get("goalsFor"),
        "goalsAgainst":  row.get("goalsAgainst"),
        "goalDifference":row.get("goalDifference"),
    }


def flatten_standings(raw_data: dict, code: str) -> dict | None:
    standings_list = raw_data.get("standings", [])
    if not standings_list:
        return None
    season = raw_data.get("season") or {}
    return {
        "competition_code": code,
        "display_title":    get_display_title(code),
        "season": {
            "id":              season.get("id"),
            "startDate":       season.get("startDate"),
            "endDate":         season.get("endDate"),
            "currentMatchday": season.get("currentMatchday"),
        },
        "standings": [
            {
                "stage": s.get("stage"),
                "type":  s.get("type"),
                "group": s.get("group"),
                "table": [flatten_standing_row(r) for r in s.get("table", [])],
            }
            for s in standings_list
        ],
    }


def flatten_scorer(scorer: dict) -> dict:
    player = scorer.get("player") or {}
    team   = scorer.get("team")   or {}
    return {
        "player": {
            "id":           player.get("id"),
            "name":         player.get("name"),
            "firstName":    player.get("firstName"),
            "lastName":     player.get("lastName"),
            "dateOfBirth":  player.get("dateOfBirth"),
            "nationality":  player.get("nationality"),
            "position":     player.get("position"),
            "shirtNumber":  player.get("shirtNumber"),
        },
        "team": {
            "id":        team.get("id"),
            "name":      team.get("name"),
            "shortName": team.get("shortName"),
            "tla":       team.get("tla"),
            "crest":     team.get("crest"),
        },
        "playedMatches": scorer.get("playedMatches"),
        "goals":         scorer.get("goals"),
        "assists":       scorer.get("assists"),
        "penalties":     scorer.get("penalties"),
    }


def flatten_scorers(raw_data: dict, code: str) -> dict | None:
    raw_scorers = raw_data.get("scorers", [])
    if not raw_scorers:
        return None
    comp_season = raw_data.get("season") or {}
    return {
        "competition_code": code,
        "display_title":    get_display_title(code),
        "season": {
            "id":              comp_season.get("id"),
            "startDate":       comp_season.get("startDate"),
            "endDate":         comp_season.get("endDate"),
            "currentMatchday": comp_season.get("currentMatchday"),
        },
        "count":   len(raw_scorers),
        "scorers": [flatten_scorer(s) for s in raw_scorers],
    }


# ── Per-competition fetchers ──────────────────────────────────────────────────

def fetch_competition_info(code: str, season_str: str) -> None:
    """Fetch and write competitionInfo.json for one league competition."""
    raw = fetch(f"/competitions/{code}")
    if not raw:
        return
    paths = get_data_paths(code, season=season_str)
    safe_write(paths["competition_info"], flatten_competition_info(raw))


def fetch_standing(code: str, api_season: int, season_str: str) -> dict | None:
    """Fetch standings and return the flattened dict (also writes to disk)."""
    raw = fetch(f"/competitions/{code}/standings", params={"season": api_season})
    if not raw:
        return None
    flattened = flatten_standings(raw, code)
    if flattened:
        paths = get_data_paths(code, season=season_str)
        safe_write(paths["standing"], flattened)
    return flattened


def fetch_scorers(code: str, api_season: int, season_str: str) -> None:
    """Fetch top scorers and write topScorer.json for one league competition."""
    params = {"limit": SCORERS_LIMIT, "season": api_season}
    raw = fetch(f"/competitions/{code}/scorers", params=params)
    if not raw:
        return
    flattened = flatten_scorers(raw, code)
    if flattened:
        paths = get_data_paths(code, season=season_str)
        safe_write(paths["scorers"], flattened)


# ── Batch runners ─────────────────────────────────────────────────────────────

def run(
    mode: str = "all",
    season: Optional[int] = None,
    competition: Optional[str] = None,
) -> None:
    """
    Main entry point.

    mode        : "all" | "info" | "standings" | "scorers"
    season      : API season start year (e.g. 2024 → 2024-2025).
                  None → current season from config.
    competition : single competition code to process, e.g. "PL".
                  None → all LEAGUE_COMPETITIONS.
    """
    if season is not None:
        season_str = f"{season}-{season + 1}"
        api_season = season
    else:
        paths      = get_season_paths()
        season_str = paths["season"]
        api_season = get_current_season_start_year()

    # Determine which codes to process (exclude tournaments)
    if competition:
        code_upper = competition.upper()
        if is_tournament(code_upper):
            logger.error(
                "%s is a tournament code — use fetch_worldCup.py / fetch_Euro.py instead.",
                code_upper,
            )
            return
        codes = [code_upper]
    else:
        codes = [c for c in LEAGUE_COMPETITIONS if not is_tournament(c)]

    for code in codes:
        logger.info("Processing %s  [season=%s, mode=%s]", code, season_str, mode)

        if mode in ("all", "info"):
            fetch_competition_info(code, season_str)

        if mode in ("all", "standings"):
            fetch_standing(code, api_season, season_str)

        if mode in ("all", "scorers"):
            fetch_scorers(code, api_season, season_str)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch league competition metadata.")
    parser.add_argument(
        "--mode",
        choices=["all", "info", "standings", "scorers"],
        default="all",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Start year of the season (e.g. 2024 → 2024-2025). Default: current.",
    )
    parser.add_argument(
        "--competition",
        type=str,
        default=None,
        metavar="CODE",
        help="Fetch only this competition code, e.g. PL, BL1.",
    )
    args = parser.parse_args()
    run(mode=args.mode, season=args.season, competition=args.competition)
