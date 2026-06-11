#!/usr/bin/env python3
"""
workers/fetch_competitions.py
──────────────────────────────────────────────────────────────────────────────
Fetches competition metadata, league standings, and top scorers for all
tracked competitions.

Endpoints used:
    GET /competitions/{code}           → competition metadata
    GET /competitions/{code}/standings → league table
    GET /competitions/{code}/scorers   → top scorers

Output layout
─────────────
    data/{season}/competitions.json        ← all competition metadata
    data/{season}/standings/{CODE}.json    ← league table (TOTAL + HOME + AWAY)
    data/{season}/scorers/{CODE}.json      ← top N scorers per competition

Changes vs previous version:
    • competitions.json now retains `emblem` (was incorrectly stripped)
    • area.flag URL now retained in competition metadata
    • Scorers endpoint added (was completely missing before)
    • Standings include `form` string per team row

Sync schedule:
    --mode competitions  →  weekly  (Monday 00:00 UTC)
    --mode standings     →  hourly
    --mode scorers       →  daily
    --mode all           →  all three (default)
"""

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from config import (
    COMPETITION_STRIP_FIELDS,
    SCORERS_LIMIT,
    TRACKED_COMPETITIONS,
    get_season_paths,
)
from workers.utils import fetch, safe_write, strip_fields

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("fetch_competitions")


# ── COMPETITION METADATA ──────────────────────────────────────────────────────

def flatten_competition(raw: dict) -> dict:
    """
    Flatten one competition payload from GET /competitions/{code}.

    Retains: id, name, code, type, emblem (logo URL — was incorrectly stripped),
             area (with flag URL), currentSeason.
    Strips:  lastUpdated, _links, seasons (historical array).
    """
    raw = dict(raw)
    strip_fields(raw, COMPETITION_STRIP_FIELDS)

    area           = raw.get("area") or {}
    current_season = raw.get("currentSeason") or {}

    return {
        "id":     raw.get("id"),
        "name":   raw.get("name"),
        "code":   raw.get("code"),
        "type":   raw.get("type"),
        "emblem": raw.get("emblem"),    # RETAINED — competition logo URL

        "area": {
            "id":   area.get("id"),
            "name": area.get("name"),
            "code": area.get("code"),
            "flag": area.get("flag"),  # RETAINED — country flag URL
        },

        "currentSeason": {
            "id":              current_season.get("id"),
            "startDate":       current_season.get("startDate"),
            "endDate":         current_season.get("endDate"),
            "currentMatchday": current_season.get("currentMatchday"),
            "winner":          current_season.get("winner"),  # null mid-season
        },
    }


def fetch_all_competitions() -> list[dict]:
    """Fetch metadata for every tracked competition."""
    logger.info("Fetching competition metadata ...")
    competitions = []
    for code in TRACKED_COMPETITIONS:
        logger.info("  Fetching: %s", code)
        data = fetch(f"/competitions/{code}")
        if data is None:
            logger.warning("  Failed to fetch %s — skipping", code)
            continue
        competitions.append(flatten_competition(data))
    return competitions


# ── STANDINGS ─────────────────────────────────────────────────────────────────

def flatten_standing_row(row: dict) -> dict:
    """
    Flatten one row in a league table.
    Retains all stat columns including form string.
    """
    team = row.get("team") or {}
    return {
        "position":       row.get("position"),
        "team": {
            "id":        team.get("id"),
            "name":      team.get("name"),
            "shortName": team.get("shortName"),
            "tla":       team.get("tla"),
            "crest":     team.get("crest"),  # RETAINED — logo for standings table
        },
        "playedGames":    row.get("playedGames"),
        "form":           row.get("form"),   # e.g. "W,D,W,L,W"
        "won":            row.get("won"),
        "draw":           row.get("draw"),
        "lost":           row.get("lost"),
        "points":         row.get("points"),
        "goalsFor":       row.get("goalsFor"),
        "goalsAgainst":   row.get("goalsAgainst"),
        "goalDifference": row.get("goalDifference"),
    }


def flatten_standings(raw_data: dict, code: str) -> dict | None:
    """
    Flatten the full standings response.
    Returns None for CUP competitions with no league table.
    Includes TOTAL, HOME, and AWAY standing types.
    """
    standings_list = raw_data.get("standings", [])
    if not standings_list:
        logger.info("  No standings table for %s (likely CUP format)", code)
        return None

    season = raw_data.get("season") or {}

    result = {
        "competition_code": code,
        "season": {
            "id":              season.get("id"),
            "startDate":       season.get("startDate"),
            "endDate":         season.get("endDate"),
            "currentMatchday": season.get("currentMatchday"),
        },
        "standings": [],
    }

    for standing in standings_list:
        table = standing.get("table", [])
        result["standings"].append({
            "stage": standing.get("stage"),
            "type":  standing.get("type"),   # TOTAL | HOME | AWAY
            "group": standing.get("group"),  # null for regular leagues, group name for CL
            "table": [flatten_standing_row(row) for row in table],
        })

    return result


def fetch_all_standings(standings_dir: str) -> None:
    """Fetch standings for every tracked competition."""
    for code in TRACKED_COMPETITIONS:
        logger.info("Fetching standings for %s ...", code)
        data = fetch(f"/competitions/{code}/standings")

        if data is None:
            logger.warning("  Standings fetch failed for %s — preserving existing file", code)
            continue

        flattened = flatten_standings(data, code)
        if flattened is None:
            continue

        out_path = f"{standings_dir}/{code}.json"
        safe_write(out_path, flattened)


# ── SCORERS ───────────────────────────────────────────────────────────────────

def flatten_scorer(scorer: dict) -> dict:
    """
    Flatten one entry from the scorers response.

    API returns:
        player: {id, name, firstName, lastName, dateOfBirth, nationality, position,
                 shirtNumber, lastUpdated}
        team:   {id, name, shortName, tla, crest, ...}
        playedMatches, goals, assists, penalties
    """
    player = scorer.get("player") or {}
    team   = scorer.get("team") or {}
    return {
        "player": {
            "id":          player.get("id"),
            "name":        player.get("name"),
            "firstName":   player.get("firstName"),
            "lastName":    player.get("lastName"),
            "dateOfBirth": player.get("dateOfBirth"),
            "nationality": player.get("nationality"),
            "position":    player.get("position"),
            "shirtNumber": player.get("shirtNumber"),
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


def fetch_all_scorers(scorers_dir: str) -> None:
    """
    Fetch top scorers for every tracked competition and write one file each.
    Competitions that return no scorer data (e.g. WC between tournaments)
    are skipped gracefully.
    """
    for code in TRACKED_COMPETITIONS:
        logger.info("Fetching scorers for %s ...", code)
        data = fetch(
            f"/competitions/{code}/scorers",
            params={"limit": SCORERS_LIMIT},
        )

        if data is None:
            logger.warning("  Scorers fetch failed for %s — preserving existing file", code)
            continue

        raw_scorers = data.get("scorers", [])
        if not raw_scorers:
            logger.info("  No scorers data for %s — skipping", code)
            continue

        season = data.get("season") or {}
        result = {
            "competition_code": code,
            "season": {
                "id":              season.get("id"),
                "startDate":       season.get("startDate"),
                "endDate":         season.get("endDate"),
                "currentMatchday": season.get("currentMatchday"),
            },
            "count":   len(raw_scorers),
            "scorers": [flatten_scorer(s) for s in raw_scorers],
        }

        out_path = f"{scorers_dir}/{code}.json"
        safe_write(out_path, result)

    logger.info("Scorers fetch complete.")


# ── MAIN RUN ──────────────────────────────────────────────────────────────────

def run(mode: str = "all") -> None:
    """
    Execute the fetch pipeline.

    mode = "all"           → competitions + standings + scorers (default)
    mode = "competitions"  → metadata only  (weekly)
    mode = "standings"     → league tables only  (hourly)
    mode = "scorers"       → top scorers only  (daily)
    """
    paths         = get_season_paths()
    season        = paths["season"]
    comp_file     = paths["competitions"]
    standings_dir = paths["standings_dir"]
    scorers_dir   = paths["scorers_dir"]

    logger.info(
        "=== fetch_competitions [mode=%s, season=%s] started at %s ===",
        mode, season, datetime.now(timezone.utc).isoformat(),
    )

    if mode in ("all", "competitions"):
        competitions = fetch_all_competitions()
        if not competitions:
            logger.error("No competitions fetched — aborting write")
            sys.exit(1)
        safe_write(comp_file, competitions)

    if mode in ("all", "standings"):
        fetch_all_standings(standings_dir)

    if mode in ("all", "scorers"):
        fetch_all_scorers(scorers_dir)

    logger.info(
        "=== fetch_competitions [mode=%s, season=%s] complete ===",
        mode, season,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Fetch competition metadata, standings, and/or scorers."
    )
    parser.add_argument(
        "--mode",
        choices=["all", "competitions", "standings", "scorers"],
        default="all",
        help=(
            "all           = competitions + standings + scorers (default)  |  "
            "competitions  = metadata only  |  "
            "standings     = league tables only  |  "
            "scorers       = top scorers only"
        ),
    )
    args = parser.parse_args()
    run(mode=args.mode)