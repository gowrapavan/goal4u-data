#!/usr/bin/env python3
"""
workers/audit_matches.py — Smart audit engine for the goal4u-data pipeline.

Audit rules (from architecture notes, Page 2)
─────────────────────────────────────────────
  matches  : past matches → fetch once (skip if FINISHED with full data)
             current/upcoming → update daily
             POSTPONED → always re-fetch (auto-detected)
  standings: update weekly  (triggered when matches change, or standalone)
  scorers  : update weekly  (wired to the same cycle as standings)
  teams    : update once every ~2 months  (separate GitHub Actions schedule)

  All audits apply only to the ONGOING season — not the next season.
  Tagging: each audit run carries an explicit `tag` so GitHub Actions YAMLs
  and logs can distinguish what was audited.

Tags (used by GHA --tag argument)
──────────────────────────────────
  matches          daily match staleness audit
  competition-stats  weekly standings + scorers refresh
  teams            bi-monthly teams refresh (calls fetch_teams, not this file)

Usage:
    python -m workers.audit_matches --tag matches --mode recent
    python -m workers.audit_matches --tag matches --mode live
    python -m workers.audit_matches --tag matches --mode all
    python -m workers.audit_matches --tag competition-stats
    python -m workers.audit_matches --tag competition-stats --competition PL
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, ".")

from config import (  # type: ignore[import]
    LEAGUE_COMPETITIONS,
    get_current_season_start_year,
    get_season_paths,
)
from workers.fetch_competitions import (
    fetch_standing,
    fetch_scorers,
)
from workers.fetch_matches import flatten_match
from workers.tournament_paths import get_data_paths, is_tournament
from workers.utils import fetch, safe_write

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("audit_matches")

# ── Staleness windows ─────────────────────────────────────────────────────────

LOOKBACK_HOURS: dict[str, int] = {
    "live":   12,    # anything in the last 12 h
    "recent": 72,    # last 3 days
    "all":    168,   # last 7 days
}

# Match status groups
LIVE_STATUSES      = {"IN_PLAY", "PAUSED"}
SCHEDULED_STATUSES = {"TIMED", "SCHEDULED"}
FINISHED_STATUS    = "FINISHED"
POSTPONED_STATUS   = "POSTPONED"
SKIP_STATUSES      = {"CANCELLED", "AWARDED", "WALKOVER", "SUSPENDED"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_utc(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_stale(match: dict, now: datetime, lookback_hours: int) -> bool:
    """
    Return True if this match record needs to be re-fetched.

    Stale when:
      • status is IN_PLAY or PAUSED (live right now)
      • status is POSTPONED (rescheduled date unknown until re-fetched)
      • status is SCHEDULED/TIMED and kick-off is within 2 hours
      • status is FINISHED but score or detail data is missing
    Not stale when:
      • utcDate is before the lookback window (too old to bother)
      • status is in SKIP_STATUSES
    """
    status   = match.get("status", "")
    utc_date = _parse_utc(match.get("utcDate"))

    # Always refresh live matches
    if status in LIVE_STATUSES:
        return True

    # Permanently skip
    if status in SKIP_STATUSES or utc_date is None:
        return False

    # Outside the lookback window → not stale
    cutoff = now - timedelta(hours=max(lookback_hours, 12))
    if utc_date < cutoff:
        return False

    # Postponed → always stale (need the new date)
    if status == POSTPONED_STATUS:
        return True

    # Future match close to kick-off
    if utc_date > now:
        return (
            status in SCHEDULED_STATUSES
            and (utc_date - now) < timedelta(hours=2)
        )

    # Past scheduled match that wasn't updated
    if status in SCHEDULED_STATUSES:
        return True

    # Finished — check data completeness
    if status == FINISHED_STATUS:
        score     = match.get("score") or {}
        full_time = score.get("fullTime") or {}
        home_g    = full_time.get("home")
        away_g    = full_time.get("away")

        if home_g is None or away_g is None:
            return True   # missing scoreline

        if ((home_g or 0) + (away_g or 0)) > 0 and not (match.get("goals") or []):
            return True   # goals happened but goals list is empty

        home_team = match.get("homeTeam") or {}
        away_team = match.get("awayTeam") or {}
        if (
            not home_team.get("lineup")
            and not home_team.get("bench")
            and not away_team.get("lineup")
            and not away_team.get("bench")
        ):
            return True   # no lineup data yet

        return False

    return True


# ── Match audit for one competition ──────────────────────────────────────────

def audit_matches_for_tournament(
    code: str,
    paths: dict,
    lookback_hours: int = 168,
) -> tuple[int, int, int]:
    """
    Incremental match audit for a tournament (WC, EC).

    Accepts a pre-built `paths` dict (from get_data_paths) so the caller
    controls the output directory — no league season string needed.

    Returns (stale_count, updated_count, skipped_count).
    """
    path = paths["matches"]
    now  = datetime.now(timezone.utc)

    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        matches: list[dict] = payload.get("data", [])
    except (FileNotFoundError, json.JSONDecodeError):
        matches = []

    if not matches:
        logger.info("%s: no existing matches.json — skipping audit (run full fetch first)", code)
        return 0, 0, 0

    id_to_idx = {m.get("id"): i for i, m in enumerate(matches) if m.get("id")}
    stale     = [m for m in matches if _is_stale(m, now, lookback_hours)]

    if not stale:
        logger.info("%s: %d matches checked, none stale", code, len(matches))
        return 0, 0, len(matches)

    logger.info(
        "%s: %d / %d matches are stale — re-fetching individually",
        code, len(stale), len(matches),
    )

    updated = 0
    skipped = 0

    for match in stale:
        match_id = match.get("id")
        raw      = fetch(f"/matches/{match_id}")
        if not raw:
            skipped += 1
            continue

        match_data = raw if raw.get("id") else raw.get("match") or raw
        if not match_data.get("id"):
            skipped += 1
            continue

        flattened = flatten_match(match_data, code)
        idx       = id_to_idx.get(match_id)
        if idx is not None:
            matches[idx] = flattened
        else:
            matches.append(flattened)
            id_to_idx[match_id] = len(matches) - 1
        updated += 1

    if updated > 0:
        matches.sort(key=lambda m: m.get("utcDate") or "")
        safe_write(path, matches)
        logger.info("%s: audit updated %d matches", code, updated)

    return len(stale), updated, skipped


def audit_matches_for_competition(
    code: str,
    season_str: str,
    lookback_hours: int,
) -> tuple[int, int, int]:
    """
    Audit and patch stale matches for one competition.

    Returns (stale_count, updated_count, skipped_count).
    """
    paths = get_data_paths(code, season=season_str)
    path  = paths["matches"]
    now   = datetime.now(timezone.utc)

    # Load existing matches
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        matches: list[dict] = payload.get("data", [])
    except (FileNotFoundError, json.JSONDecodeError):
        matches = []

    if not matches:
        logger.info("%s: no existing match file — skipping audit", code)
        return 0, 0, 0

    id_to_idx    = {m.get("id"): i for i, m in enumerate(matches) if m.get("id")}
    stale        = [m for m in matches if _is_stale(m, now, lookback_hours)]

    if not stale:
        logger.info("%s: %d matches checked, none stale", code, len(matches))
        return 0, 0, len(matches)

    logger.info("%s: %d / %d matches are stale — re-fetching individually", code, len(stale), len(matches))

    updated  = 0
    skipped  = 0

    for match in stale:
        match_id = match.get("id")
        raw      = fetch(f"/matches/{match_id}")
        if not raw:
            skipped += 1
            continue

        match_data = raw if raw.get("id") else raw.get("match") or raw
        if not match_data.get("id"):
            skipped += 1
            continue

        flattened = flatten_match(match_data, code)
        idx       = id_to_idx.get(match_id)
        if idx is not None:
            matches[idx] = flattened
        else:
            matches.append(flattened)
            id_to_idx[match_id] = len(matches) - 1
        updated += 1

    if updated > 0:
        matches.sort(key=lambda m: m.get("utcDate") or "")
        safe_write(path, matches)
        logger.info("%s: updated %d matches", code, updated)

    return len(stale), updated, skipped


# ── Competition-stats audit (standings + scorers) ─────────────────────────────

def audit_competition_stats(
    code: str,
    season_str: str,
    api_season: int,
) -> None:
    """
    Re-fetch standings AND scorers for `code`.
    Called on the weekly schedule.
    """
    logger.info("%s: refreshing standings + scorers [tag=competition-stats]", code)
    fetch_standing(code, api_season, season_str)
    fetch_scorers(code, api_season, season_str)


# ── Top-level tag dispatchers ─────────────────────────────────────────────────

def run_matches_audit(mode: str = "recent", competition: Optional[str] = None) -> None:
    """
    Tag: matches
    Daily job — audit stale/live/upcoming matches.
    Only runs against the current (ongoing) season.
    """
    lookback_hours = LOOKBACK_HOURS.get(mode, LOOKBACK_HOURS["recent"])
    season_str     = get_season_paths()["season"]

    # Only leagues — tournaments are not on a rolling season calendar
    if competition:
        code_upper = competition.upper()
        if is_tournament(code_upper):
            logger.error(
                "audit --tag matches does not apply to tournament code %s "
                "(tournaments have their own fetch scripts).",
                code_upper,
            )
            return
        codes = [code_upper]
    else:
        codes = [c for c in LEAGUE_COMPETITIONS if not is_tournament(c)]

    total_stale = total_updated = total_skipped = 0
    for code in codes:
        stale, updated, skipped = audit_matches_for_competition(code, season_str, lookback_hours)
        total_stale   += stale
        total_updated += updated
        total_skipped += skipped

    logger.info(
        "audit-matches done | mode=%s | stale=%d updated=%d skipped=%d",
        mode, total_stale, total_updated, total_skipped,
    )


def run_competition_stats_audit(competition: Optional[str] = None) -> None:
    """
    Tag: competition-stats
    Weekly job — refresh standings + scorers.
    Only runs against the current (ongoing) season.
    """
    season_str = get_season_paths()["season"]
    api_season = get_current_season_start_year()

    if competition:
        code_upper = competition.upper()
        if is_tournament(code_upper):
            logger.error(
                "audit --tag competition-stats does not apply to tournament code %s.",
                code_upper,
            )
            return
        codes = [code_upper]
    else:
        codes = [c for c in LEAGUE_COMPETITIONS if not is_tournament(c)]

    for code in codes:
        audit_competition_stats(code, season_str, api_season)

    logger.info("audit-competition-stats done | codes=%s", ", ".join(codes))


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Audit pipeline — refresh stale data for the ongoing season.",
    )
    parser.add_argument(
        "--tag",
        choices=["matches", "competition-stats"],
        required=True,
        help=(
            "Which audit to run:\n"
            "  matches          — refresh stale / live / postponed match records\n"
            "  competition-stats — refresh standings + scorers (weekly)\n"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["live", "recent", "all"],
        default="recent",
        help="Staleness window for --tag matches (default: recent = last 72h).",
    )
    parser.add_argument(
        "--competition",
        type=str,
        default=None,
        metavar="CODE",
        help="Target a single competition code, e.g. PL, BL1.",
    )
    args = parser.parse_args()

    if args.tag == "matches":
        run_matches_audit(mode=args.mode, competition=args.competition)
    elif args.tag == "competition-stats":
        run_competition_stats_audit(competition=args.competition)