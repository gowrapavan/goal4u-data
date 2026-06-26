"""
config_reference.py — Reference config template.

This is NOT the file you run.  It documents exactly what keys / callables
the refactored workers import from your real config.py so you can verify your
config exports everything the new code needs.

Copy the shape below into your actual config.py (keep your existing logic,
just make sure these names exist and have the correct types).
"""

from __future__ import annotations
from datetime import date


# ── Competition registries ────────────────────────────────────────────────────

# Leagues only — do NOT include WC or EC here.
# These are the codes the league workers (fetch_competitions, fetch_teams,
# fetch_matches, audit_matches) will iterate over.
LEAGUE_COMPETITIONS: list[str] = [
    "PL",   # Premier League
    "PD",   # La Liga
    "BL1",  # Bundesliga
    "FL1",  # Ligue 1
    "SA",   # Serie A
    "CL",   # UEFA Champions League
    "ELC",  # Championship  (as-per-season as noted in page 1)
]

# TRACKED_COMPETITIONS was the old combined list.
# Keep it for backward-compat if anything still imports it,
# but the workers now use LEAGUE_COMPETITIONS.
TRACKED_COMPETITIONS: list[str] = LEAGUE_COMPETITIONS  # alias


# ── Tournament year registry ──────────────────────────────────────────────────

# THIS is the single source of truth for tournament years.
# workers/tournament_paths.py reads this — NEVER derives years from season strings.
TOURNAMENT_YEARS: dict[str, int] = {
    "WC": 2026,   # FIFA World Cup 2026
    "EC": 2024,   # UEFA Euro 2028  (update when needed)
}


# ── Season resolution ─────────────────────────────────────────────────────────

# The month (inclusive) from which we consider the NEW season to be "current".
# Football seasons run roughly Aug–May.  By June the old season is finished and
# pre-season prep for the next one begins, so June is the right switchover:
#
#   Jan–May  2026  →  season start year = 2025  (2025-2026 still running)
#   Jun–Dec  2026  →  season start year = 2026  (2026-2027 is next / current)
#
# Set to 6 (June).  Change to 7 if you want July to still be "old season".
_NEW_SEASON_FROM_MONTH: int = 6


def get_current_season_start_year() -> int:
    """
    Return the start year of the current (or upcoming) football season.

    Examples (with _NEW_SEASON_FROM_MONTH = 6):
        May  2026  →  2025   (2025-2026 season still active)
        Jun  2026  →  2026   (2026-2027 is next, treat as current for data prep)
        Aug  2026  →  2026   (2026-2027 season underway)

    To fetch the *previous* completed season pass --season explicitly:
        python main.py --season 2025   →  data/2025-2026/
    """
    today = date.today()
    return today.year if today.month >= _NEW_SEASON_FROM_MONTH else today.year - 1


def get_season_paths() -> dict[str, str]:
    """
    Return path fragments for the current league season.

    Keys:
        season     : "2026-2027"
        data_root  : "data/2026-2027"
    """
    start = get_current_season_start_year()
    season = f"{start}-{start + 1}"
    return {
        "season":    season,
        "data_root": f"data/{season}",
    }


# ── Field strip lists ─────────────────────────────────────────────────────────
# Fields to remove from raw API dicts before persisting.

COMPETITION_STRIP_FIELDS: list[str] = [
    "seasons",
    "lastUpdated",
]

MATCH_STRIP_FIELDS: list[str] = [
    "lastUpdated",
]

PERSON_STRIP_FIELDS: list[str] = [
    "lastUpdated",
]

TEAM_STRIP_FIELDS: list[str] = [
    "lastUpdated",
]


# ── Scorers limit ─────────────────────────────────────────────────────────────

SCORERS_LIMIT: int = 10