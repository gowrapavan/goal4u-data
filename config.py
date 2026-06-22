"""
config.py — single source of truth for the goal4u-data pipeline.

KEY CHANGE:
WC and EC are single-year tournaments that don't fit the Aug–Jul club season model. 
The `TOURNAMENT_YEARS` registry maps a competition code to the API season year it 
should always use.

HOW TO HANDLE WC / EC IN PRACTICE:
  • WC 2026: the current season IS 2026-2027 (June 2026 onwards). WC runs June–July 
    2026. `get_current_season_start_year()` returns 2026, which matches perfectly.
  • HISTORICAL backfill (--season 2025): override WC to skip / return empty 
    (API returns 404 anyway; Conditional Fallback handles it). No data loss.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone


# ── TRACKED COMPETITIONS ──────────────────────────────────────────────────────

TRACKED_COMPETITIONS: list[str] = [
    "PL",   # Premier League           (England)
    "PD",   # La Liga                  (Spain)
    "BL1",  # Bundesliga               (Germany)
    "SA",   # Serie A                  (Italy)
    "FL1",  # Ligue 1                  (France)
    "CL",   # UEFA Champions League
    "EC",   # UEFA European Championship (2024, 2028 ...)
    "WC",   # FIFA World Cup           (2022, 2026 ...)
]

# How many top scorers to fetch per competition
SCORERS_LIMIT: int = 20


# ── TOURNAMENT YEAR REGISTRY ──────────────────────────────────────────────────
# Maps competition code → the calendar years when that tournament actually runs.
# Used by get_api_season_for_competition() to decide whether to include a
# cup competition in an API call for a given season year.
TOURNAMENT_YEARS: dict[str, list[int]] = {
    "WC": [2018, 2022, 2026, 2030],
    "EC": [2016, 2020, 2024, 2028],
}


# ── SEASON RESOLUTION ─────────────────────────────────────────────────────────

def get_current_season_string(ref: date | None = None) -> str:
    """
    Return the active football season as a "YYYY-YYYY" folder name.

    Rule:
      - Month >= June  →  season is over / next season scheduled  →  "{year}-{year+1}"
      - Month <  June  →  still in the season that started last year →  "{year-1}-{year}"
    """
    now  = ref or datetime.now(timezone.utc).date()
    year = now.year
    return f"{year}-{year + 1}" if now.month >= 6 else f"{year - 1}-{year}"


def get_current_season_start_year(ref: date | None = None) -> int:
    """
    Return the start-year integer for the season get_current_season_string()
    resolves to, e.g. "2026-2027" -> 2026.
    """
    return int(get_current_season_string(ref).split("-")[0])


def get_api_season_for_competition(code: str, ref: date | None = None) -> int | None:
    """
    Return the API ?season= value to use for a given competition code.

    For club leagues: Returns get_current_season_start_year()
    For cup tournaments (WC, EC): Returns the tournament year IF active in the 
    current season window, else returns None (triggering a safe skip/404).
    """
    season_start = get_current_season_start_year(ref)
    tournament_years = TOURNAMENT_YEARS.get(code)

    if tournament_years is None:
        # Club league — use standard season
        return season_start

    # Cup tournament — find the active tournament year, if any
    for t_year in tournament_years:
        if season_start <= t_year <= season_start + 1:
            return t_year   # e.g. 2026 for WC, 2024 for EC

    # No tournament in this season window
    return None


# ── SEASON-AWARE PATH FACTORY ─────────────────────────────────────────────────

def get_season_paths(season: str | int | None = None) -> dict[str, str]:
    """
    Return every base path used by the worker scripts for the given season.
    Accepts season as None (auto-resolves to current), an int (e.g. 2024), 
    or a string (e.g. "2024-2025").
    """
    if isinstance(season, int):
        season_str = f"{season}-{season + 1}"
    elif isinstance(season, str):
        season_str = season
    else:
        season_str = get_current_season_string()

    base = os.environ.get("DATA_DIR", "data")
    root = f"{base}/{season_str}"

    return {
        "season":          season_str,
        "base":            base,
        "root":            root,
        "competitions":    f"{root}/competitions.json",
        "standings_dir":   f"{root}/standings",
        "matches_dir":     f"{root}/matches",
        "scorers_dir":     f"{root}/scorers",
        "teams_dir":       f"{root}/teams",
        "stats_dir":       f"{root}/stats",
        "match_links_dir": f"{root}/match-links",
    }


# ── FIELD STRIP LISTS ─────────────────────────────────────────────────────────

# Competition-level fields to drop
COMPETITION_STRIP_FIELDS: list[str] = [
    "lastUpdated",
    "_links",
    "seasons",    # long historical array — bloats competitions.json, not needed
]

# Match-level top-level fields to drop
MATCH_STRIP_FIELDS: list[str] = [
    "lastUpdated",
    "_links",
]

# Team-level fields to drop
TEAM_STRIP_FIELDS: list[str] = [
    "address",
    "phone",
    "email",
    "lastUpdated",
    "_links",
]

# Fields to strip from in-match person references
PERSON_STRIP_FIELDS: list[str] = [
    "lastUpdated",
    "_links",
]