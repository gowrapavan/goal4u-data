#!/usr/bin/env python3
"""
workers/fetch_teams.py
──────────────────────────────────────────────────────────────────────────────
Fetches complete team profiles (club info + coach + full squad) for every
team in every tracked competition.

WHY THIS APPROACH (important — read before modifying):
───────────────────────────────────────────────────────
The previous version called GET /teams/{id}?squad=true for every team.
On the free tier (TIER_ONE) this returns HTTP 403 for most teams — only a
handful of "featured" teams are accessible via that endpoint on the free plan.

The CORRECT approach for the free tier is:
    GET /competitions/{code}/teams

This endpoint returns ALL teams in the competition with full squad data
(coach, players with dateOfBirth/nationality/shirtNumber/marketValue/contract)
in a single API call, and it works on all tiers.

The previous run logged: "119 written, 85 failed" — every single 403 failure
was a victim of calling /teams/{id} directly. This script eliminates all 403s
by using the competition-scoped teams endpoint instead.

Output layout
─────────────
    data/{season}/teams/{COMP_CODE}/{team_id}.json

Examples:
    data/2025-2026/teams/PL/57.json      ← Arsenal FC (full squad)
    data/2025-2026/teams/PL/65.json      ← Manchester City FC
    data/2025-2026/teams/CL/86.json      ← Real Madrid CF
    data/2025-2026/teams/WC/762.json     ← Argentina

Sync schedule: daily at 06:00 UTC via GitHub Actions.
"""

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from config import TEAM_STRIP_FIELDS, TRACKED_COMPETITIONS, get_season_paths
from workers.utils import fetch, safe_write, strip_fields

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("fetch_teams")


# ── FLATTEN ───────────────────────────────────────────────────────────────────

def flatten_player(player: dict) -> dict:
    """
    Normalise one squad member from the API response.

    The competition/teams endpoint returns players with:
        id, name, firstName, lastName, position, dateOfBirth,
        nationality, shirtNumber, marketValue, contract

    We keep ALL of these. Missing keys default to None.
    Extra keys from future API versions are passed through unchanged.
    """
    # Keys we always want to guarantee are present
    GUARANTEED = [
        "id", "name", "firstName", "lastName",
        "position", "dateOfBirth", "nationality",
        "shirtNumber", "marketValue", "contract",
    ]
    out = {}
    # First pass: guaranteed keys with None fallback
    for key in GUARANTEED:
        out[key] = player.get(key)
    # Second pass: pass through any extra keys the API adds
    for k, v in player.items():
        if k not in out:
            out[k] = v
    return out


def flatten_coach(coach: dict | None) -> dict | None:
    """
    Normalise the coach sub-object.
    Keeps: id, firstName, lastName, name, nationality, dateOfBirth, contract.
    """
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
    """
    Full flatten pipeline for one team object.

    Strips only the fields listed in TEAM_STRIP_FIELDS (address, phone, email,
    lastUpdated, _links). Everything else — crest, website, founded, clubColors,
    marketValue, activeCompetitions, runningCompetitions, staff — is retained.

    Squad members and coach are normalised through their own flatten functions.
    Any top-level keys not explicitly handled are passed through unchanged so
    future API additions are never silently dropped.
    """
    # Work on a shallow copy to avoid mutating the original response dict
    raw = dict(raw)
    strip_fields(raw, TEAM_STRIP_FIELDS)

    area  = raw.pop("area", None) or {}
    coach = raw.pop("coach", None)
    squad = raw.pop("squad", None) or []

    team: dict = {
        # Identity
        "id":        raw.pop("id", None),
        "name":      raw.pop("name", None),
        "shortName": raw.pop("shortName", None),
        "tla":       raw.pop("tla", None),

        # Branding / info — ALL retained
        "crest":       raw.pop("crest", None),      # logo URL
        "website":     raw.pop("website", None),
        "founded":     raw.pop("founded", None),
        "clubColors":  raw.pop("clubColors", None),
        "venue":       raw.pop("venue", None),

        # Geography — full area object + convenience shortcut
        "area": {
            "id":   area.get("id"),
            "name": area.get("name"),
            "code": area.get("code"),
            "flag": area.get("flag"),
        },
        "area_code": area.get("code"),   # shortcut for the FastAPI router

        # Competition membership (retained — shows which comps team is in)
        "activeCompetitions":  raw.pop("activeCompetitions", None),
        "runningCompetitions": raw.pop("runningCompetitions", None),

        # Squad value
        "marketValue": raw.pop("marketValue", None),

        # People
        "coach": flatten_coach(coach),
        "squad": [flatten_player(p) for p in squad],
        "staff": raw.pop("staff", []),
    }

    # Pass through any remaining keys not explicitly handled above
    # (future-proofing against API additions)
    for k, v in raw.items():
        if k not in team:
            team[k] = v

    return team


# ── FETCH PIPELINE ────────────────────────────────────────────────────────────

def fetch_and_write_teams_for_competition(
    code: str,
    teams_dir: str,
) -> tuple[int, int]:
    """
    Fetch all teams for competition `code` using a single API call to:
        GET /competitions/{code}/teams

    This endpoint returns the full team roster (coach + squad with player
    attributes) for ALL teams in the competition on all API tiers — no
    individual /teams/{id} calls needed, no 403s.

    Writes one file per team:
        {teams_dir}/{code}/{team_id}.json

    Returns (written_count, failed_count).
    """
    logger.info("Fetching teams for competition %s ...", code)

    # Single call — gets all teams + squads for this competition
    data = fetch(f"/competitions/{code}/teams")

    if data is None:
        logger.warning(
            "  Teams fetch failed for %s — preserving any existing files", code
        )
        return 0, 0

    raw_teams = data.get("teams", [])
    logger.info("  %d teams returned for %s", len(raw_teams), code)

    if not raw_teams:
        logger.warning("  Empty teams list for %s — nothing to write", code)
        return 0, 0

    written  = 0
    failed   = 0
    seen_ids: set[int] = set()

    for raw in raw_teams:
        team_id = raw.get("id")
        if not team_id or team_id in seen_ids:
            continue
        seen_ids.add(team_id)

        team_name = raw.get("name", "unknown")

        try:
            flattened = flatten_team(raw)
        except Exception as exc:
            logger.warning(
                "  flatten_team failed for team %s (%s): %s — skipping",
                team_id, team_name, exc,
            )
            failed += 1
            continue

        # data/{season}/teams/{CODE}/{team_id}.json
        out_path = f"{teams_dir}/{code}/{team_id}.json"

        if safe_write(out_path, flattened):
            written += 1
        else:
            failed += 1

    logger.info(
        "  %s: %d written, %d failed (of %d total)",
        code, written, failed, len(raw_teams),
    )
    return written, failed


def run() -> None:
    paths     = get_season_paths()
    season    = paths["season"]
    teams_dir = paths["teams_dir"]

    logger.info(
        "=== fetch_teams started [season=%s] at %s ===",
        season, datetime.now(timezone.utc).isoformat(),
    )

    total_written = 0
    total_failed  = 0
    comps_ok      = 0

    for code in TRACKED_COMPETITIONS:
        written, failed = fetch_and_write_teams_for_competition(code, teams_dir)
        total_written += written
        total_failed  += failed
        if written > 0:
            comps_ok += 1

    logger.info(
        "=== fetch_teams complete [season=%s]: "
        "%d files written across %d/%d competitions, %d failed ===",
        season, total_written, comps_ok, len(TRACKED_COMPETITIONS), total_failed,
    )

    if total_written == 0:
        logger.error(
            "Zero team files written — check FOOTBALL_DATA_API_KEY"
        )
        sys.exit(1)


if __name__ == "__main__":
    run()