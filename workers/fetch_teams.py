#!/usr/bin/env python3
"""
workers/fetch_teams.py — Fetch teams + squads for league competitions.

Writes a single teams.json per competition folder:
    data/{season}/{CODE}/teams.json       ← leagues
    data/world-cup/world-cup-{year}/teams.json  ← called from fetch_worldCup.py

Audit note: teams should be refreshed at most once every ~2 months per plan.
The GitHub Actions audit will enforce the schedule; this script always writes
when called directly.

Usage:
    python -m workers.fetch_teams                        # current season, all leagues
    python -m workers.fetch_teams --season 2024          # historical
    python -m workers.fetch_teams --competition PL       # one competition
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

sys.path.insert(0, ".")

from config import (  # type: ignore[import]
    LEAGUE_COMPETITIONS,
    TEAM_STRIP_FIELDS,
    get_current_season_start_year,
    get_season_paths,
)
from workers.tournament_paths import get_data_paths, is_tournament
from workers.utils import fetch, safe_write, strip_fields

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_teams")


# ── Flatten helpers ───────────────────────────────────────────────────────────

def flatten_player(player: dict) -> dict:
    GUARANTEED = [
        "id", "name", "firstName", "lastName", "position",
        "dateOfBirth", "nationality", "shirtNumber", "marketValue", "contract",
    ]
    out = {key: player.get(key) for key in GUARANTEED}
    # carry forward any extra keys the API adds
    for k, v in player.items():
        if k not in out:
            out[k] = v
    return out


def flatten_coach(coach: dict | None) -> dict | None:
    if not coach:
        return None
    return {
        "id":          coach.get("id"),
        "firstName":   coach.get("firstName"),
        "lastName":    coach.get("lastName"),
        "name":        coach.get("name"),
        "nationality": coach.get("nationality"),
        "dateOfBirth": coach.get("dateOfBirth"),
        "contract":    coach.get("contract"),
    }


def flatten_team(raw: dict) -> dict:
    raw  = dict(raw)
    strip_fields(raw, TEAM_STRIP_FIELDS)
    area  = raw.pop("area",  None) or {}
    coach = raw.pop("coach", None)
    squad = raw.pop("squad", None) or []
    return {
        "id":                 raw.pop("id",                 None),
        "name":               raw.pop("name",               None),
        "shortName":          raw.pop("shortName",          None),
        "tla":                raw.pop("tla",                None),
        "crest":              raw.pop("crest",              None),
        "website":            raw.pop("website",            None),
        "founded":            raw.pop("founded",            None),
        "clubColors":         raw.pop("clubColors",         None),
        "venue":              raw.pop("venue",              None),
        "area": {
            "id":   area.get("id"),
            "name": area.get("name"),
            "code": area.get("code"),
            "flag": area.get("flag"),
        },
        "area_code":           area.get("code"),
        "activeCompetitions":  raw.pop("activeCompetitions",  None),
        "runningCompetitions": raw.pop("runningCompetitions", None),
        "marketValue":         raw.pop("marketValue",         None),
        "coach":  flatten_coach(coach),
        "squad":  [flatten_player(p) for p in squad],
        "staff":  raw.pop("staff", []),
        # carry forward any remaining keys
        **{k: v for k, v in raw.items()},
    }


# ── Core fetch ────────────────────────────────────────────────────────────────

def fetch_teams_for_competition(
    code: str,
    *,
    api_season: Optional[int] = None,
    season_str: Optional[str] = None,
    paths: Optional[dict] = None,
) -> int:
    """
    Fetch all teams for `code` and write teams.json.

    `paths` can be pre-computed (used by fetch_worldCup / fetch_Euro so they
    control the output location).  If omitted, paths are derived from
    `season_str` via get_data_paths().

    Returns the number of teams written (0 on failure).
    """
    params = {"season": api_season} if api_season is not None else None
    raw    = fetch(f"/competitions/{code}/teams", params=params)
    if not raw:
        logger.warning("%s: no team data returned", code)
        return 0

    raw_teams = raw.get("teams", [])
    if not raw_teams:
        logger.warning("%s: empty teams list", code)
        return 0

    if paths is None:
        paths = get_data_paths(code, season=season_str)

    teams = []
    seen: set[int] = set()
    for team_raw in raw_teams:
        tid = team_raw.get("id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            teams.append(flatten_team(team_raw))
        except Exception as exc:
            logger.warning("%s: skipping team %s due to error: %s", code, tid, exc)

    if teams:
        safe_write(paths["teams"], teams)
        logger.info("%s: wrote %d teams → %s", code, len(teams), paths["teams"])

    return len(teams)


# ── Batch runner ──────────────────────────────────────────────────────────────

def run(
    season: Optional[int] = None,
    competition: Optional[str] = None,
) -> None:
    """
    Fetch teams for all (or one) league competition(s).

    season      : API season start year. None → current season.
    competition : Single competition code. None → all LEAGUE_COMPETITIONS.
    """
    if season is not None:
        season_str = f"{season}-{season + 1}"
        api_season = season
    else:
        paths      = get_season_paths()
        season_str = paths["season"]
        api_season = get_current_season_start_year()

    if competition:
        code_upper = competition.upper()
        if is_tournament(code_upper):
            logger.error(
                "%s is a tournament code — use fetch_worldCup.py / fetch_Euro.py.",
                code_upper,
            )
            return
        codes = [code_upper]
    else:
        codes = [c for c in LEAGUE_COMPETITIONS if not is_tournament(c)]

    for code in codes:
        fetch_teams_for_competition(
            code,
            api_season=api_season,
            season_str=season_str,
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch teams + squads for leagues.")
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Season start year (e.g. 2024 → 2024-2025). Default: current.",
    )
    parser.add_argument(
        "--competition",
        type=str,
        default=None,
        metavar="CODE",
        help="Fetch only this competition code, e.g. PL, BL1.",
    )
    args = parser.parse_args()
    run(season=args.season, competition=args.competition)
