#!/usr/bin/env python3
"""
workers/tournament_paths.py — canonical path routing for ALL data output.

Folder contracts
────────────────
League:
    data/{season}/{CODE}/
        competitionInfo.json
        matches.json
        standing.json
        topScorer.json
        teams.json

Tournament (World Cup):
    data/world-cup/world-cup-{year}/
        competitionInfo.json
        matches.json
        teams.json
        standing.json
        scorers.json

Tournament (Euros):
    data/euros/euro-{year}/
        <same shape as WC>

Key design decisions
────────────────────
• Tournament year comes from config.TOURNAMENT_YEARS — NEVER derived from a
  league season string like "2026-2027".
• League paths include the competition CODE as a sub-folder so each competition
  is self-contained and can be fetched / audited independently.
• All path keys are strings (not Path objects) so callers can pass them straight
  to safe_write() and open() without conversion.
"""

from __future__ import annotations

from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

TOURNAMENT_CODES: frozenset[str] = frozenset({"WC", "EC"})

_DISPLAY_NAMES: dict[str, str] = {
    "WC":  "FIFA World Cup",
    "EC":  "UEFA Euro",
    "PL":  "Premier League",
    "PD":  "La Liga",
    "SA":  "Serie A",
    "FL1": "Ligue 1",
    "BL1": "Bundesliga",
    "CL":  "UEFA Champions League",
    "ELC": "Championship",
}

# Root directories for tournament data (outside the league season tree)
_TOURNAMENT_ROOTS: dict[str, str] = {
    "WC": "data/world-cup",
    "EC": "data/euros",
}

# Folder slug pattern per tournament code — {year} substituted at runtime
_TOURNAMENT_SLUG: dict[str, str] = {
    "WC": "world-cup-{year}",
    "EC": "euro-{year}",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_tournament(code: str) -> bool:
    """Return True if `code` is a one-off tournament (WC, EC) not a league."""
    return code.upper() in TOURNAMENT_CODES


def get_display_title(code: str, tournament_year: Optional[int] = None) -> str:
    """
    Human-readable competition name.

    For tournaments pass `tournament_year` to get e.g. "FIFA World Cup 2026".
    For leagues the year is already encoded in the season folder.
    """
    code = code.upper()
    base = _DISPLAY_NAMES.get(code, code)
    if is_tournament(code) and tournament_year is not None:
        return f"{base} {tournament_year}"
    return base


def get_tournament_year(code: str) -> int:
    """
    Authoritative tournament year from config.TOURNAMENT_YEARS.

    NEVER derives the year from a league season string — that was the old bug.
    Raises KeyError with a helpful message if the code isn't registered.
    """
    from config import TOURNAMENT_YEARS  # type: ignore[import]

    code = code.upper()
    if code not in TOURNAMENT_YEARS:
        raise KeyError(
            f"Tournament code {code!r} is not in config.TOURNAMENT_YEARS. "
            f"Add it before running the pipeline."
        )
    return TOURNAMENT_YEARS[code]


# ── Main path builder ─────────────────────────────────────────────────────────

def get_data_paths(
    code: str,
    *,
    season: Optional[str] = None,
    tournament_year: Optional[int] = None,
) -> dict[str, str]:
    """
    Return the canonical output paths for a given competition code.

    Parameters
    ----------
    code            : Competition code, e.g. "PL", "WC", "EC"
    season          : League season string, e.g. "2026-2027"  (leagues only)
    tournament_year : Override the year for a tournament; if omitted the year
                      is looked up from config.TOURNAMENT_YEARS automatically.

    Path keys returned
    ------------------
    root                top-level folder for this competition
    competition_info    competitionInfo.json
    matches             matches.json
    standing            standing.json
    scorers             topScorer.json (league) / scorers.json (tournament)
    teams               teams.json  — ONE file for the full squad list

    All paths are relative to the project root (no leading slash).
    """
    code = code.upper()

    if is_tournament(code):
        # ── Tournament path ───────────────────────────────────────────────────
        if tournament_year is None:
            tournament_year = get_tournament_year(code)

        slug = _TOURNAMENT_SLUG[code].format(year=tournament_year)
        root = f"{_TOURNAMENT_ROOTS[code]}/{slug}"

        return {
            "root":               root,
            "competition_info":   f"{root}/competitionInfo.json",
            "matches":            f"{root}/matches.json",
            "standing":           f"{root}/standing.json",
            "scorers":            f"{root}/scorers.json",
            "teams":              f"{root}/teams.json",
        }

    else:
        # ── League path ───────────────────────────────────────────────────────
        if season is None:
            raise ValueError(
                f"League code {code!r} requires a `season` argument "
                f"(e.g. season='2026-2027')."
            )

        root = f"data/{season}/{code}"

        return {
            "root":               root,
            "competition_info":   f"{root}/competitionInfo.json",
            "matches":            f"{root}/matches.json",
            "standing":           f"{root}/standing.json",
            "scorers":            f"{root}/topScorer.json",   # leagues use topScorer
            "teams":              f"{root}/teams.json",
        }