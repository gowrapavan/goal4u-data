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


# ── Tournament-specific standings helpers ─────────────────────────────────────

def _build_team_group_map(matches_path: str) -> dict[int, str]:
    """
    Read matches.json and return {team_id: group_label} for GROUP_STAGE matches.
    Returns empty dict if the file is missing or has no group info.
    """
    import json as _json
    try:
        with open(matches_path, encoding="utf-8") as fh:
            payload = _json.load(fh)
        matches: list[dict] = payload.get("data", [])
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}

    team_to_group: dict[int, str] = {}
    for match in matches:
        if match.get("stage") != "GROUP_STAGE":
            continue
        group = match.get("group")
        if not group:
            continue
        home_id = (match.get("homeTeam") or {}).get("id")
        away_id = (match.get("awayTeam") or {}).get("id")
        if home_id:
            team_to_group[home_id] = group
        if away_id:
            team_to_group[away_id] = group

    return team_to_group


def _split_by_group(
    flat_table: list[dict],
    team_group_map: dict[int, str],
) -> dict[str, list[dict]]:
    """
    Split a flat standings table into a {group_label: [rows]} dict.
    Rows with no group mapping are put under "UNKNOWN".
    Within each group, rows are re-numbered 1..N by their original position order.
    """
    groups: dict[str, list[dict]] = {}
    for row in flat_table:
        team_id = (row.get("team") or {}).get("id")
        group   = team_group_map.get(team_id, "UNKNOWN") if team_id else "UNKNOWN"
        groups.setdefault(group, []).append(row)

    # Re-number positions within each group
    for rows in groups.values():
        rows.sort(key=lambda r: r.get("position") or 9999)
        for i, row in enumerate(rows, 1):
            row = dict(row)
            row["position"] = i
            rows[i - 1] = row

    return groups


def flatten_tournament_standings(
    raw_data: dict,
    code: str,
    matches_path: str,
) -> dict | None:
    """
    Like flatten_standings() but splits the flat GROUP_STAGE table into
    per-group entries using the team→group mapping derived from matches.json.

    The output standings list has one entry per group (GROUP_A … GROUP_L)
    instead of one flat entry with group=null.

    Falls back to flatten_standings() if the matches file is unavailable
    or contains no group info (e.g. before the tournament starts).
    """
    standings_list = raw_data.get("standings", [])
    if not standings_list:
        return None

    season = raw_data.get("season") or {}

    # Build the team→group map from matches.json
    team_group_map = _build_team_group_map(matches_path)

    # Find the TOTAL GROUP_STAGE entry (the one we want to split)
    total_entry = next(
        (s for s in standings_list if s.get("stage") == "GROUP_STAGE" and s.get("type") == "TOTAL"),
        None,
    )

    if not total_entry or not team_group_map:
        # Fallback: use the original flatten_standings
        return flatten_standings(raw_data, code)

    flat_table   = total_entry.get("table", [])
    flat_rows    = [flatten_standing_row(r) for r in flat_table]
    groups_split = _split_by_group(flat_rows, team_group_map)

    # Sort group keys alphabetically (GROUP_A, GROUP_B, …)
    sorted_groups = sorted(groups_split.keys())

    per_group_standings = [
        {
            "stage": "GROUP_STAGE",
            "type":  "TOTAL",
            "group": group_label,
            "table": groups_split[group_label],
        }
        for group_label in sorted_groups
        if group_label != "UNKNOWN"
    ]

    # Append UNKNOWN at the end if any teams couldn't be mapped
    if "UNKNOWN" in groups_split:
        per_group_standings.append({
            "stage": "GROUP_STAGE",
            "type":  "TOTAL",
            "group": "UNKNOWN",
            "table": groups_split["UNKNOWN"],
        })

    return {
        "competition_code": code,
        "display_title":    get_display_title(code),
        "season": {
            "id":              season.get("id"),
            "startDate":       season.get("startDate"),
            "endDate":         season.get("endDate"),
            "currentMatchday": season.get("currentMatchday"),
        },
        "standings": per_group_standings,
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