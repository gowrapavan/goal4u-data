#!/usr/bin/env python3
"""
workers/fetch_matches.py — League match fetcher.

Writes matches.json per competition:
    data/{season}/{CODE}/matches.json

Features
────────
• Postpone detection: if a match in the existing file is POSTPONED it is
  automatically re-fetched on the next run so the rescheduled date is captured.
• Custom fetch: --season and --competition filters let you target exactly one
  competition for a re-pull without touching others.
• Historical fetch: --season 2024 uses a longer timeout + monthly chunk
  fallback (some plans return 403 for full-season dumps of older seasons).

Usage:
    python -m workers.fetch_matches                        # current season, all leagues
    python -m workers.fetch_matches --season 2024          # historical 2024-2025
    python -m workers.fetch_matches --competition PL       # one competition (current season)
    python -m workers.fetch_matches --competition PL --season 2024
"""

from __future__ import annotations

import calendar
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, ".")

from config import (  # type: ignore[import]
    LEAGUE_COMPETITIONS,
    MATCH_STRIP_FIELDS,
    PERSON_STRIP_FIELDS,
    get_current_season_start_year,
    get_season_paths,
)
from workers.tournament_paths import get_data_paths, is_tournament
from workers.utils import fetch, get_api_token, safe_write, strip_fields

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_matches")

# ── Score / statistics keys ───────────────────────────────────────────────────

SCORE_KEYS = [
    "winner", "duration", "fullTime", "halfTime",
    "regularTime", "extraTime", "penalties",
]

STATS_SIDE_KEYS = [
    "shots", "shots_on_goal", "shots_off_goal", "possession", "fouls",
    "corner_kicks", "yellow_cards", "yellow_red_cards", "red_cards",
    "saves", "offsides",
]

_STAT_KEY_MAP: dict[str, str] = {
    "Ball Possession":  "possession",
    "Total Shots":      "shots",
    "Shots on Goal":    "shots_on_goal",
    "Shots off Goal":   "shots_off_goal",
    "Fouls":            "fouls",
    "Corner Kicks":     "corner_kicks",
    "Yellow Cards":     "yellow_cards",
    "Yellow/Red Cards": "yellow_red_cards",
    "Red Cards":        "red_cards",
    "Goalkeeper Saves": "saves",
    "Offsides":         "offsides",
}

POSTPONED_STATUS = "POSTPONED"


# ── Flatten helpers ───────────────────────────────────────────────────────────

def flatten_score(score: dict | None) -> dict:
    if score is None:
        result = {k: None for k in SCORE_KEYS}
        for k in ("fullTime", "halfTime", "regularTime", "extraTime", "penalties"):
            result[k] = {"home": None, "away": None}
        return result
    out: dict = {}
    for key in SCORE_KEYS:
        val = score.get(key)
        if key in ("fullTime", "halfTime", "regularTime", "extraTime", "penalties"):
            out[key] = (
                {"home": val.get("home"), "away": val.get("away")}
                if val else {"home": None, "away": None}
            )
        else:
            out[key] = val
    return out


def flatten_statistics(raw_stats) -> dict:
    base: dict = {
        "home": {k: None for k in STATS_SIDE_KEYS},
        "away": {k: None for k in STATS_SIDE_KEYS},
    }
    if not raw_stats:
        return base
    if isinstance(raw_stats, dict):
        return raw_stats
    for stat in raw_stats:
        stat_type = stat.get("type", "")
        our_key   = _STAT_KEY_MAP.get(stat_type)
        if not our_key:
            continue
        for side in ("home", "away"):
            val = stat.get(side)
            if isinstance(val, str) and val.endswith("%"):
                val = float(val.rstrip("%"))
            base[side][our_key] = val
    return base


def flatten_person_ref(person: dict | None) -> dict | None:
    if not person:
        return None
    p = dict(person)
    strip_fields(p, PERSON_STRIP_FIELDS)
    return p


def flatten_team_in_match(team: dict | None) -> dict | None:
    if not team:
        return None
    coach_raw = team.get("coach")
    coach = (
        {
            "id":          coach_raw.get("id"),
            "name":        coach_raw.get("name"),
            "nationality": coach_raw.get("nationality"),
        }
        if coach_raw else None
    )
    return {
        "id":          team.get("id"),
        "name":        team.get("name"),
        "shortName":   team.get("shortName"),
        "tla":         team.get("tla"),
        "crest":       team.get("crest"),
        "leagueRank":  team.get("leagueRank"),
        "formation":   team.get("formation"),
        "coach":       coach,
        "lineup":      [flatten_person_ref(p) for p in (team.get("lineup") or [])],
        "bench":       [flatten_person_ref(p) for p in (team.get("bench")  or [])],
    }


def flatten_match(raw: dict, competition_code: str) -> dict:
    """Flatten one raw match dict into the canonical match shape."""
    raw = dict(raw)
    strip_fields(raw, MATCH_STRIP_FIELDS)

    area_raw  = raw.get("area")        or {}
    comp_raw  = raw.get("competition") or {}
    season_raw= raw.get("season")      or {}
    home_raw  = raw.get("homeTeam")    or {}
    away_raw  = raw.get("awayTeam")    or {}
    odds_raw  = raw.get("odds")

    # Statistics can come back in two shapes depending on the API endpoint
    home_stats = home_raw.get("statistics")
    away_stats = away_raw.get("statistics")
    if isinstance(home_stats, dict) and isinstance(away_stats, dict):
        statistics = {
            "home": {k: home_stats.get(k) for k in STATS_SIDE_KEYS},
            "away": {k: away_stats.get(k) for k in STATS_SIDE_KEYS},
        }
    else:
        statistics = flatten_statistics(raw.get("statistics"))

    return {
        "id":               raw.get("id"),
        "competition_code": competition_code,
        "area": {
            "id":   area_raw.get("id"),
            "name": area_raw.get("name"),
            "code": area_raw.get("code"),
            "flag": area_raw.get("flag"),
        },
        "competition": {
            "id":     comp_raw.get("id"),
            "name":   comp_raw.get("name"),
            "code":   comp_raw.get("code"),
            "type":   comp_raw.get("type"),
            "emblem": comp_raw.get("emblem"),
        },
        "season": {
            "id":              season_raw.get("id"),
            "startDate":       season_raw.get("startDate"),
            "endDate":         season_raw.get("endDate"),
            "currentMatchday": season_raw.get("currentMatchday"),
            "winner":          season_raw.get("winner"),
        },
        "utcDate":    raw.get("utcDate"),
        "status":     raw.get("status"),
        "matchday":   raw.get("matchday"),
        "stage":      raw.get("stage"),
        "group":      raw.get("group"),
        "minute":     raw.get("minute"),
        "injuryTime": raw.get("injuryTime"),
        "attendance": raw.get("attendance"),
        "venue":      raw.get("venue"),
        "homeTeam":   flatten_team_in_match(home_raw),
        "awayTeam":   flatten_team_in_match(away_raw),
        "score":      flatten_score(raw.get("score")),
        "statistics": statistics,
        "goals": [
            {
                "minute":     g.get("minute"),
                "injuryTime": g.get("injuryTime"),
                "type":       g.get("type"),
                "team":       {"id": (g.get("team") or {}).get("id"), "name": (g.get("team") or {}).get("name")},
                "scorer":     flatten_person_ref(g.get("scorer")),
                "assist":     flatten_person_ref(g.get("assist")),
                "score":      g.get("score"),
            }
            for g in (raw.get("goals") or [])
        ],
        "bookings": [
            {
                "minute": b.get("minute"),
                "team":   {"id": (b.get("team") or {}).get("id"), "name": (b.get("team") or {}).get("name")},
                "player": flatten_person_ref(b.get("player")),
                "card":   b.get("card"),
            }
            for b in (raw.get("bookings") or [])
        ],
        "substitutions": [
            {
                "minute":    s.get("minute"),
                "team":      {"id": (s.get("team") or {}).get("id"), "name": (s.get("team") or {}).get("name")},
                "playerOut": flatten_person_ref(s.get("playerOut")),
                "playerIn":  flatten_person_ref(s.get("playerIn")),
            }
            for s in (raw.get("substitutions") or [])
        ],
        "referees": [
            {
                "id":          r.get("id"),
                "name":        r.get("name"),
                "type":        r.get("type"),
                "nationality": r.get("nationality"),
            }
            for r in (raw.get("referees") or [])
        ],
        "odds": {
            "homeWin": (odds_raw or {}).get("homeWin"),
            "draw":    (odds_raw or {}).get("draw"),
            "awayWin": (odds_raw or {}).get("awayWin"),
        },
    }


# ── Postpone detection ────────────────────────────────────────────────────────

def _load_existing_matches(path: str) -> list[dict]:
    """Return the list of matches already on disk (empty list if missing/corrupt)."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload.get("data", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _has_postponed(matches: list[dict]) -> bool:
    """Return True if any match in the list is POSTPONED."""
    return any(m.get("status") == POSTPONED_STATUS for m in matches)


# ── Historical fetch (month-by-month fallback) ────────────────────────────────

_HIST_REQUEST_TIMEOUT  = 60
_HIST_MIN_INTERVAL     = 6.0
_hist_last_request_time: float = 0.0


def _fetch_large(endpoint: str, params: dict | None = None, retries: int = 3) -> dict | None:
    """Like fetch() but with a longer timeout for large historical dumps."""
    global _hist_last_request_time
    elapsed = time.monotonic() - _hist_last_request_time
    if elapsed < _HIST_MIN_INTERVAL:
        time.sleep(_HIST_MIN_INTERVAL - elapsed)
    _hist_last_request_time = time.monotonic()

    token = get_api_token()
    url   = f"https://api.football-data.org/v4{endpoint}"

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url,
                headers={"X-Auth-Token": token},
                params=params,
                timeout=_HIST_REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = int(resp.headers.get("X-RequestCounter-Reset", 60))
                time.sleep(wait)
                _hist_last_request_time = time.monotonic() - _HIST_MIN_INTERVAL
                continue
            if resp.status_code == 403:
                return None
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return None
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def _fetch_month(code: str, year: int, month: int) -> list[dict]:
    date_from = f"{year}-{month:02d}-01"
    date_to   = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    data = _fetch_large(
        f"/competitions/{code}/matches",
        params={"dateFrom": date_from, "dateTo": date_to},
    )
    return [flatten_match(m, code) for m in data.get("matches", [])] if data else []


def _fetch_season_by_month(code: str, season: int) -> list[dict]:
    """Month-by-month fallback: Aug–Dec of season year, Jan–Jul of season+1."""
    matches: list[dict] = []
    for month in range(8, 13):
        matches += _fetch_month(code, season, month)
    for month in range(1, 8):
        matches += _fetch_month(code, season + 1, month)
    return matches


# ── Current-season fetch ──────────────────────────────────────────────────────

def fetch_matches_for_competition(
    code: str,
    api_season: int,
    *,
    paths: Optional[dict] = None,
    season_str: Optional[str] = None,
) -> list[dict]:
    """
    Fetch the full match list for one competition (current or specified season).

    If there are POSTPONED matches in the existing file they are automatically
    included in the stale set so this function effectively re-fetches them.
    (A full competition dump from the API always contains the latest status.)
    """
    data = fetch(f"/competitions/{code}/matches", params={"season": api_season})
    if not data:
        logger.warning("%s: no match data returned", code)
        return []

    matches = [flatten_match(m, code) for m in data.get("matches", [])]
    matches.sort(key=lambda m: m.get("utcDate") or "")

    if paths is None and season_str:
        paths = get_data_paths(code, season=season_str)

    if paths:
        safe_write(paths["matches"], matches)

    return matches


# ── Historical fetch ──────────────────────────────────────────────────────────

def fetch_matches_historical(code: str, season: int) -> list[dict]:
    """
    Fetch historical matches for `code` + `season`.

    Tries a single full-season dump first; falls back to month-by-month if the
    API returns 403 (tier restriction on large historical dumps).
    """
    data = _fetch_large(f"/competitions/{code}/matches", params={"season": season})
    if data:
        matches = [flatten_match(m, code) for m in data.get("matches", [])]
    else:
        logger.info("%s: full-season dump failed, falling back to month-by-month", code)
        matches = _fetch_season_by_month(code, season)

    matches.sort(key=lambda x: x.get("utcDate") or "")
    return matches


# ── Batch runners ─────────────────────────────────────────────────────────────

def run(competition: Optional[str] = None) -> None:
    """Fetch matches for the current season (all leagues or one competition)."""
    paths_cfg  = get_season_paths()
    season_str = paths_cfg["season"]
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
        out_paths = get_data_paths(code, season=season_str)
        fetch_matches_for_competition(
            code,
            api_season,
            paths=out_paths,
            season_str=season_str,
        )


def run_historical(season: int, competition: Optional[str] = None) -> None:
    """Fetch historical matches for a past season (all leagues or one competition)."""
    season_str = f"{season}-{season + 1}"

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
        matches = fetch_matches_historical(code, season)
        if matches:
            out_path = get_data_paths(code, season=season_str)["matches"]
            safe_write(out_path, matches)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch league matches.")
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Season start year for historical fetch (e.g. 2024 → 2024-2025).",
    )
    parser.add_argument(
        "--competition",
        type=str,
        default=None,
        metavar="CODE",
        help="Fetch only this competition code, e.g. PL, BL1.",
    )
    args = parser.parse_args()

    if args.season is not None:
        run_historical(args.season, competition=args.competition)
    else:
        run(competition=args.competition)
