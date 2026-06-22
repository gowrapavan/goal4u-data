#!/usr/bin/env python3
"""
workers/fetch_matches.py
──────────────────────────────────────────────────────────────────────────────
Fetches all match fixtures, results, and live data for every tracked
competition and writes one file per competition.

What the API actually returns per match (and what we now keep):
────────────────────────────────────────────────────────────────
Top level:
    area, competition, season, id, utcDate, status, minute, injuryTime,
    attendance, venue, matchday, stage, group, lastUpdated,
    homeTeam, awayTeam, score, goals, bookings, substitutions,
    referees, odds, statistics

homeTeam / awayTeam (within a match):
    id, name, shortName, tla, crest, coach (id/name/nationality),
    leagueRank, formation, lineup (array of players), bench (array)

PREVIOUSLY DROPPED (now retained):
    • season      — startDate / endDate / currentMatchday / winner
    • area        — which country/region the match is in
    • competition — id, name, code, type, emblem
    • referees    — referee name, nationality, type
    • odds        — homeWin, draw, awayWin (when available)
    • crest       — team logo URLs inside homeTeam/awayTeam
    • coach       — in-match coach reference
    • leagueRank  — team's current league position
    • formation   — e.g. "4-3-3"
    • lineup      — starting XI player refs
    • bench       — substitute player refs

Output layout
─────────────
    data/{season}/matches/{COMP_CODE}.json

Sync schedule: every 15 minutes via GitHub Actions.

CLI
───
    # Current season (default — used by the 15-min GitHub Actions cron):
    python workers/fetch_matches.py

    # Historical season — uses a higher timeout + browser User-Agent +
    # monthly-chunk fallback for reliability (fetch_historical_matches.py
    # has been merged into this file, so there's nothing else to run):
    python workers/fetch_matches.py --season 2024
    python workers/fetch_matches.py --season 2023
"""

import calendar
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, ".")

from config import (
    MATCH_STRIP_FIELDS,
    PERSON_STRIP_FIELDS,
    TRACKED_COMPETITIONS,
    get_season_paths,
)
from workers.utils import fetch, get_api_token, safe_write, strip_fields

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("fetch_matches")


# ── SCHEMA KEYS (for null-normalization) ─────────────────────────────────────

SCORE_KEYS = [
    "winner", "duration",
    "fullTime", "halfTime", "regularTime", "extraTime", "penalties",
]

STATS_SIDE_KEYS = [
    "shots", "shots_on_goal", "shots_off_goal",
    "possession", "fouls", "corner_kicks",
    "yellow_cards", "yellow_red_cards", "red_cards",
    "saves", "offsides",
]

# Key mapping from API stat type strings to our snake_case keys
_STAT_KEY_MAP: dict[str, str] = {
    "Ball Possession":   "possession",
    "Total Shots":       "shots",
    "Shots on Goal":     "shots_on_goal",
    "Shots off Goal":    "shots_off_goal",
    "Fouls":             "fouls",
    "Corner Kicks":      "corner_kicks",
    "Yellow Cards":      "yellow_cards",
    "Yellow/Red Cards":  "yellow_red_cards",
    "Red Cards":         "red_cards",
    "Goalkeeper Saves":  "saves",
    "Offsides":          "offsides",
    # snake_case fallback (some API versions use these)
    "ball_possession":   "possession",
    "shots":             "shots",
    "shots_on_goal":     "shots_on_goal",
    "shots_off_goal":    "shots_off_goal",
    "fouls":             "fouls",
    "corner_kicks":      "corner_kicks",
    "yellow_cards":      "yellow_cards",
    "yellow_red_cards":  "yellow_red_cards",
    "red_cards":         "red_cards",
    "saves":             "saves",
    "offsides":          "offsides",
}


# ── FLATTEN HELPERS ───────────────────────────────────────────────────────────

def flatten_score(score: dict | None) -> dict:
    """Normalise the score block. All sub-keys always present (None if missing)."""
    if score is None:
        result = {k: None for k in SCORE_KEYS}
        for k in ("fullTime", "halfTime", "regularTime", "extraTime", "penalties"):
            result[k] = {"home": None, "away": None}
        return result

    out: dict = {}
    for key in SCORE_KEYS:
        val = score.get(key)
        if key in ("fullTime", "halfTime", "regularTime", "extraTime", "penalties"):
            if val is None:
                out[key] = {"home": None, "away": None}
            else:
                out[key] = {"home": val.get("home"), "away": val.get("away")}
        else:
            out[key] = val
    return out


def flatten_statistics(raw_stats) -> dict:
    """
    Normalise match statistics.

    The API returns stats as a list of {type, home, away} objects.
    We convert to: {"home": {"shots": N, ...}, "away": {...}}
    Handles both list format (from API) and dict format (from cached files).
    """
    base: dict = {
        "home": {k: None for k in STATS_SIDE_KEYS},
        "away": {k: None for k in STATS_SIDE_KEYS},
    }
    if not raw_stats:
        return base
    # Already flattened (cached file re-processing)
    if isinstance(raw_stats, dict):
        return raw_stats
    # Raw API list format
    for stat in raw_stats:
        stat_type = stat.get("type", "")
        our_key = _STAT_KEY_MAP.get(stat_type)
        if not our_key:
            continue
        for side in ("home", "away"):
            val = stat.get(side)
            if isinstance(val, str) and val.endswith("%"):
                val = float(val.rstrip("%"))
            base[side][our_key] = val
    return base


def flatten_person_ref(person: dict | None) -> dict | None:
    """
    Minimal person reference used in goals/bookings/substitutions/lineup/bench.

    Keeps: id, name, position, shirtNumber, nationality, dateOfBirth.
    Strips: lastUpdated, _links (via PERSON_STRIP_FIELDS).
    Full profiles live in data/{season}/teams/{CODE}/{id}.json.
    """
    if not person:
        return None
    p = dict(person)
    strip_fields(p, PERSON_STRIP_FIELDS)
    return p


def flatten_team_in_match(team: dict | None) -> dict | None:
    """
    Full team reference as it appears inside a match object.

    Retains: id, name, shortName, tla, crest, leagueRank,
             formation, coach (id/name/nationality),
             lineup (list of player refs), bench (list of player refs).

    These fields are all present in the API response but were previously
    stripped to just {id, name, shortName, tla}. That threw away formation,
    lineup, bench, coach, crest, and leagueRank for every match.
    """
    if not team:
        return None

    coach_raw = team.get("coach")
    coach = None
    if coach_raw:
        coach = {
            "id":          coach_raw.get("id"),
            "name":        coach_raw.get("name"),
            "nationality": coach_raw.get("nationality"),
        }

    lineup = [flatten_person_ref(p) for p in (team.get("lineup") or [])]
    bench  = [flatten_person_ref(p) for p in (team.get("bench") or [])]

    return {
        "id":          team.get("id"),
        "name":        team.get("name"),
        "shortName":   team.get("shortName"),
        "tla":         team.get("tla"),
        "crest":       team.get("crest"),       # logo URL — retained
        "leagueRank":  team.get("leagueRank"),  # current table position
        "formation":   team.get("formation"),   # e.g. "4-3-3"
        "coach":       coach,
        "lineup":      lineup,                  # starting XI
        "bench":       bench,                   # substitutes
    }


def flatten_goals(goals: list | None) -> list:
    if not goals:
        return []
    return [
        {
            "minute":     g.get("minute"),
            "injuryTime": g.get("injuryTime"),
            "type":       g.get("type"),   # REGULAR | PENALTY | OWN_GOAL
            "team":       {
                "id":   (g.get("team") or {}).get("id"),
                "name": (g.get("team") or {}).get("name"),
            },
            "scorer":     flatten_person_ref(g.get("scorer")),
            "assist":     flatten_person_ref(g.get("assist")),
            "score":      g.get("score"),  # {"home": N, "away": N} at moment of goal
        }
        for g in goals
    ]


def flatten_bookings(bookings: list | None) -> list:
    if not bookings:
        return []
    return [
        {
            "minute": b.get("minute"),
            "team":   {
                "id":   (b.get("team") or {}).get("id"),
                "name": (b.get("team") or {}).get("name"),
            },
            "player": flatten_person_ref(b.get("player")),
            "card":   b.get("card"),  # YELLOW | RED | YELLOW_RED
        }
        for b in bookings
    ]


def flatten_substitutions(subs: list | None) -> list:
    if not subs:
        return []
    return [
        {
            "minute":    s.get("minute"),
            "team":      {
                "id":   (s.get("team") or {}).get("id"),
                "name": (s.get("team") or {}).get("name"),
            },
            "playerOut": flatten_person_ref(s.get("playerOut")),
            "playerIn":  flatten_person_ref(s.get("playerIn")),
        }
        for s in subs
    ]


def flatten_referees(referees: list | None) -> list:
    """Normalise the referees list — now retained (was previously stripped)."""
    if not referees:
        return []
    return [
        {
            "id":          r.get("id"),
            "name":        r.get("name"),
            "type":        r.get("type"),        # REFEREE | ASSISTANT_REFEREE_N1 | etc.
            "nationality": r.get("nationality"),
        }
        for r in referees
    ]


def flatten_match(raw: dict, competition_code: str) -> dict:
    """
    Full flatten pipeline for a single match object.

    Previously dropped fields that are now retained:
        season, area, competition, referees, odds,
        crest/coach/leagueRank/formation/lineup/bench inside homeTeam/awayTeam
    """
    raw = dict(raw)  # shallow copy — avoid mutating the original
    strip_fields(raw, MATCH_STRIP_FIELDS)  # only removes lastUpdated + _links

    # Pull sub-objects out cleanly
    area_raw        = raw.get("area") or {}
    comp_raw        = raw.get("competition") or {}
    season_raw      = raw.get("season") or {}
    home_raw        = raw.get("homeTeam") or {}
    away_raw        = raw.get("awayTeam") or {}
    score_raw       = raw.get("score")
    statistics_raw  = raw.get("statistics")
    odds_raw        = raw.get("odds")

    # Statistics can also be nested inside homeTeam/awayTeam on some tiers
    home_stats = home_raw.get("statistics")
    away_stats = away_raw.get("statistics")
    if isinstance(home_stats, dict) and isinstance(away_stats, dict):
        statistics = {
            "home": {k: home_stats.get(k) for k in STATS_SIDE_KEYS},
            "away": {k: away_stats.get(k) for k in STATS_SIDE_KEYS},
        }
    else:
        statistics = flatten_statistics(statistics_raw)

    return {
        # Identity
        "id":               raw.get("id"),
        "competition_code": competition_code,

        # Context — previously all dropped
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

        # Scheduling
        "utcDate":    raw.get("utcDate"),
        "status":     raw.get("status"),   # SCHEDULED|LIVE|IN_PLAY|PAUSED|FINISHED|etc.
        "matchday":   raw.get("matchday"),
        "stage":      raw.get("stage"),
        "group":      raw.get("group"),

        # Live match data
        "minute":     raw.get("minute"),
        "injuryTime": raw.get("injuryTime"),
        "attendance": raw.get("attendance"),
        "venue":      raw.get("venue"),

        # Teams — now includes crest, coach, leagueRank, formation, lineup, bench
        "homeTeam": flatten_team_in_match(home_raw),
        "awayTeam": flatten_team_in_match(away_raw),

        # Match result
        "score":      flatten_score(score_raw),
        "statistics": statistics,

        # Events
        "goals":         flatten_goals(raw.get("goals")),
        "bookings":      flatten_bookings(raw.get("bookings")),
        "substitutions": flatten_substitutions(raw.get("substitutions")),

        # Referees — now retained
        "referees": flatten_referees(raw.get("referees")),

        # Odds — always present as an object (values null when not available on this tier).
        # Never written as null so the UI can safely do match.odds?.homeWin without crashing.
        "odds": {
            "homeWin": (odds_raw or {}).get("homeWin"),
            "draw":    (odds_raw or {}).get("draw"),
            "awayWin": (odds_raw or {}).get("awayWin"),
        },
    }


# ── FETCH PIPELINE ────────────────────────────────────────────────────────────

def fetch_matches_for_competition(code: str, api_season: int) -> list[dict]:
    """
    Fetch all matches for one competition for the given `api_season` start
    year (e.g. 2026 -> the 2026-2027 season).

    api_season is ALWAYS passed explicitly — never omitted — because
    football-data.org's own "current season" pointer rolls over to the
    new season at a different time for each competition. Without this,
    some competitions (e.g. PD, CL) keep returning the previous season's
    finished matches long after others (e.g. PL, FL1) have already rolled
    over, even though both were fetched on the same day with no override.
    See config.get_current_season_start_year() for the full explanation.

    Returns [] on failure (Conditional Fallback — existing file unchanged).
    """
    logger.info("Fetching matches for %s (season=%s) ...", code, api_season)
    data = fetch(f"/competitions/{code}/matches", params={"season": api_season})

    if data is None:
        logger.warning("  Failed to fetch matches for %s — preserving existing file", code)
        return []

    raw_matches = data.get("matches", [])
    logger.info("  %d matches returned for %s", len(raw_matches), code)
    return [flatten_match(m, code) for m in raw_matches]


# ── HISTORICAL SEASON FETCH (merged from fetch_historical_matches.py) ────────
#
# Triggered via: python workers/fetch_matches.py --season 2024
#
# Resilience notes (kept from the original fetch_historical_matches.py, added
# after repeated ConnectionResetError(10054) on Windows):
#   - Single season-level requests can return 300-400+ matches in one payload,
#     which is the worst case for a flaky/intercepted connection (AV/firewall
#     TLS inspection, stale keep-alive, etc).
#   - PowerShell's Invoke-RestMethod succeeded against the same endpoint while
#     Python's requests (default UA, 20s timeout) did not, so this uses its
#     own request helper (_fetch_large) with a longer timeout and a browser
#     User-Agent — WITHOUT touching workers/utils.fetch(), so the live/
#     current-season path above is completely unaffected.
#   - If the season-level call still fails, falls back to fetching
#     month-by-month using dateFrom/dateTo, which is far more reliable for
#     large competitions.

_HIST_REQUEST_TIMEOUT = 60  # seconds — historical payloads are big, give it room
_HIST_MIN_REQUEST_INTERVAL = 6.0  # stay under the free-tier 10 req/min
_hist_last_request_time = 0.0

_HIST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _hist_throttle() -> None:
    global _hist_last_request_time
    elapsed = time.monotonic() - _hist_last_request_time
    if elapsed < _HIST_MIN_REQUEST_INTERVAL:
        time.sleep(_HIST_MIN_REQUEST_INTERVAL - elapsed)
    _hist_last_request_time = time.monotonic()


def _fetch_large(endpoint: str, params: dict | None = None, retries: int = 3) -> dict | None:
    """
    Local equivalent of workers.utils.fetch(), used only for historical fetches.

    Differences from the shared fetch():
      - longer timeout (60s vs 20s) for big historical payloads
      - explicit browser User-Agent (the default python-requests UA appears
        to get reset on large responses on some networks/AV setups, even
        though it works fine for small live-fetch calls)

    Same return contract as utils.fetch(): parsed JSON dict on 200,
    None on any failure (caller treats None as Conditional Fallback).
    """
    _hist_throttle()

    token = get_api_token()
    url = f"https://api.football-data.org/v4{endpoint}"
    headers = {
        "X-Auth-Token": token,
        "User-Agent": _HIST_USER_AGENT,
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=_HIST_REQUEST_TIMEOUT)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                wait = int(resp.headers.get("X-RequestCounter-Reset", 60))
                logger.warning(
                    "Rate limited on %s — waiting %ds (attempt %d/%d)",
                    endpoint, wait, attempt, retries,
                )
                time.sleep(wait)
                global _hist_last_request_time
                _hist_last_request_time = time.monotonic() - _HIST_MIN_REQUEST_INTERVAL
                continue

            if resp.status_code == 403:
                logger.warning(
                    "403 Forbidden on %s — not available on current API tier/season. Skipping.",
                    endpoint,
                )
                return None

            if resp.status_code >= 500:
                wait = 2 ** attempt
                logger.warning(
                    "Server error %d on %s — retrying in %ds (attempt %d/%d)",
                    resp.status_code, endpoint, wait, attempt, retries,
                )
                time.sleep(wait)
                continue

            logger.error("API returned %d for %s — aborting.", resp.status_code, url)
            return None

        except requests.RequestException as exc:
            wait = 2 ** attempt
            logger.warning(
                "Network error on attempt %d/%d for %s: %s — retrying in %ds",
                attempt, retries, endpoint, exc, wait,
            )
            if attempt == retries:
                logger.error("All %d retries exhausted for %s.", retries, url)
                return None
            time.sleep(wait)

    return None


def _season_folder(season: int) -> str:
    return f"{season}-{season + 1}"


def _fetch_month(code: str, year: int, month: int) -> list[dict]:
    """Fetch one calendar month of matches for a competition via dateFrom/dateTo."""
    last_day = calendar.monthrange(year, month)[1]
    date_from = f"{year}-{month:02d}-01"
    date_to = f"{year}-{month:02d}-{last_day}"

    logger.info("  Fetching %s %s..%s (monthly fallback)...", code, date_from, date_to)

    data = _fetch_large(
        f"/competitions/{code}/matches",
        params={"dateFrom": date_from, "dateTo": date_to},
    )

    if not data:
        logger.warning("  No data for %s %s..%s", code, date_from, date_to)
        return []

    return [flatten_match(match, code) for match in data.get("matches", [])]


def _fetch_season_by_month(code: str, season: int) -> list[dict]:
    """
    Fallback path: fetch a full season (Aug of `season` through Jul of
    `season + 1`) one month at a time and merge the results.
    """
    matches: list[dict] = []

    for month in range(8, 13):  # Aug-Dec of start year
        matches += _fetch_month(code, season, month)

    for month in range(1, 8):  # Jan-Jul of following year
        matches += _fetch_month(code, season + 1, month)

    return matches


def fetch_competition_matches_historical(code: str, season: int) -> list[dict]:
    logger.info("Fetching %s historical season %s...", code, season)

    data = _fetch_large(
        f"/competitions/{code}/matches",
        params={"season": season},
    )

    if data:
        matches = [flatten_match(match, code) for match in data.get("matches", [])]
        matches.sort(key=lambda x: x.get("utcDate") or "")
        return matches

    logger.warning(
        "Season-level fetch failed for %s season %s — falling back to monthly chunks",
        code, season,
    )

    matches = _fetch_season_by_month(code, season)
    matches.sort(key=lambda x: x.get("utcDate") or "")

    return matches


def run_historical(season: int) -> None:
    """Fetch one historical season for all tracked competitions."""
    folder = _season_folder(season)
    matches_dir = f"data/{folder}/matches"

    Path(matches_dir).mkdir(parents=True, exist_ok=True)

    written = 0

    for code in TRACKED_COMPETITIONS:
        matches = fetch_competition_matches_historical(code, season)

        if not matches:
            logger.warning("No matches returned for %s season %s", code, season)
            continue

        out_path = f"{matches_dir}/{code}.json"

        if safe_write(out_path, matches):
            written += 1

    logger.info("Historical fetch complete: %d competitions written", written)


def run() -> None:
    """Fetch the current season for all tracked competitions."""
    from config import get_current_season_start_year

    paths       = get_season_paths()
    season      = paths["season"]
    matches_dir = paths["matches_dir"]
    api_season  = get_current_season_start_year()   # e.g. 2026 for "2026-2027"

    logger.info(
        "=== fetch_matches started [season=%s, api_season=%s] at %s ===",
        season, api_season, datetime.now(timezone.utc).isoformat(),
    )

    written       = 0
    failed        = 0
    total_matches = 0

    for code in TRACKED_COMPETITIONS:
        matches = fetch_matches_for_competition(code, api_season)

        if not matches:
            failed += 1
            continue

        # Sort chronologically
        matches.sort(key=lambda m: m.get("utcDate") or "")

        out_path = f"{matches_dir}/{code}.json"
        if safe_write(out_path, matches):
            total_matches += len(matches)
            written += 1
        else:
            failed += 1

    logger.info(
        "=== fetch_matches complete [season=%s]: "
        "%d/%d competitions written, %d matches total ===",
        season, written, len(TRACKED_COMPETITIONS), total_matches,
    )

    if written == 0:
        logger.error("All competitions failed. Check API key and network.")
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Fetch match fixtures and results. "
            "Omit --season for the current season (used by the 15-min cron). "
            "Pass --season to fetch a historical season."
        )
    )
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help=(
            "Start year of a historical season to fetch "
            "(e.g. --season 2024 writes to data/2024-2025/matches/). "
            "Omit to fetch the current season."
        ),
    )
    args = parser.parse_args()

    if args.season is not None:
        # Historical season — uses a longer timeout + browser User-Agent +
        # monthly-chunk fallback for reliability (see run_historical above).
        run_historical(args.season)
    else:
        run()