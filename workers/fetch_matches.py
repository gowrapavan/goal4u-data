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
"""

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from config import (
    MATCH_STRIP_FIELDS,
    PERSON_STRIP_FIELDS,
    TRACKED_COMPETITIONS,
    get_season_paths,
)
from workers.utils import fetch, safe_write, strip_fields

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

        # Odds — now retained (null when not available on current tier)
        "odds": {
            "homeWin": (odds_raw or {}).get("homeWin"),
            "draw":    (odds_raw or {}).get("draw"),
            "awayWin": (odds_raw or {}).get("awayWin"),
        } if odds_raw else None,
    }


# ── FETCH PIPELINE ────────────────────────────────────────────────────────────

def fetch_matches_for_competition(code: str) -> list[dict]:
    """
    Fetch all current-season matches for one competition.
    Returns [] on failure (Conditional Fallback — existing file unchanged).
    """
    logger.info("Fetching matches for %s ...", code)
    data = fetch(f"/competitions/{code}/matches")

    if data is None:
        logger.warning("  Failed to fetch matches for %s — preserving existing file", code)
        return []

    raw_matches = data.get("matches", [])
    logger.info("  %d matches returned for %s", len(raw_matches), code)
    return [flatten_match(m, code) for m in raw_matches]


def run() -> None:
    paths       = get_season_paths()
    season      = paths["season"]
    matches_dir = paths["matches_dir"]

    logger.info(
        "=== fetch_matches started [season=%s] at %s ===",
        season, datetime.now(timezone.utc).isoformat(),
    )

    written       = 0
    failed        = 0
    total_matches = 0

    for code in TRACKED_COMPETITIONS:
        matches = fetch_matches_for_competition(code)

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
    run()