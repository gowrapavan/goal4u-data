#!/usr/bin/env python3
"""
workers/audit_matches.py
──────────────────────────────────────────────────────────────────────────────
Smart match auditor — patches stale/unfinished match records in existing
JSON files WITHOUT re-fetching the entire competition.

Philosophy (read before modifying):
────────────────────────────────────
A full fetch_matches.py run costs 8 API calls (one per competition) plus
the 6-second throttle between them = ~50 seconds minimum. That's fine once
per day, but wasteful if you just want to patch a handful of recently-played
matches.

This script:
  1. Reads each existing data/{season}/matches/{CODE}.json
  2. Identifies matches that need updating (see AUDIT CRITERIA below)
  3. Fetches only those matches individually via GET /matches/{id}
  4. Patches them in-place in the local list
  5. Re-sorts chronologically and writes the file back atomically

AUDIT CRITERIA — a match is re-fetched if ANY of these are true:
  • status is IN_PLAY or PAUSED  (live right now)
  • status is TIMED or SCHEDULED and utcDate is in the past (should be finished)
  • status is FINISHED but goals list is empty AND score.fullTime shows goals
    (data arrived late / was null when originally fetched)
  • status is FINISHED but lineup/bench are both empty (API filled them post-match)
  • utcDate is within LOOKBACK_HOURS hours in the past AND status != FINISHED
    (catch recently played matches that haven't updated yet)

Matches that are:
  • FINISHED with goals populated → skipped (already complete)
  • SCHEDULED with utcDate far in the future → skipped (not played yet)
  • POSTPONED / CANCELLED → skipped unless within LOOKBACK_HOURS
    (status can change — e.g. rescheduled — but we don't chase these aggressively)

Team/competition/season metadata attached to each match is refreshed from the
fetched payload, not from a separate lookup.

Output:
    data/{season}/matches/{CODE}.json  ← patched in-place (same file, atomic write)

Sync schedule (GitHub Actions):
    Every 15 minutes  →  python workers/audit_matches.py --mode live
    Every 3 hours     →  python workers/audit_matches.py --mode recent
    Daily 06:00 UTC   →  python workers/audit_matches.py --mode all
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

from config import TRACKED_COMPETITIONS, get_season_paths
from workers.fetch_matches import flatten_match
from workers.utils import fetch, safe_write

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("audit_matches")


# ── CONSTANTS ─────────────────────────────────────────────────────────────────

# How far back to look for matches that might need updating
LOOKBACK_HOURS_LIVE   = 6     # --mode live:   only matches from last 6 hours
LOOKBACK_HOURS_RECENT = 72    # --mode recent: last 3 days
LOOKBACK_HOURS_ALL    = 168   # --mode all:    last 7 days (whole matchweek)

# Statuses that are definitively stale / need checking
LIVE_STATUSES      = {"IN_PLAY", "PAUSED"}
SCHEDULED_STATUSES = {"TIMED", "SCHEDULED"}
FINISHED_STATUS    = "FINISHED"

# Statuses we never bother re-fetching (too far out / admin state)
SKIP_STATUSES = {"CANCELLED", "AWARDED", "WALKOVER", "SUSPENDED"}


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _parse_utc(date_str: str | None) -> datetime | None:
    """Parse ISO-8601 UTC string → aware datetime. Returns None if unparseable."""
    if not date_str:
        return None
    try:
        # Handle both "2025-08-16T14:00:00Z" and "2025-08-16T14:00:00+00:00"
        s = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None


def _is_stale(match: dict, now: datetime, lookback_hours: int) -> bool:
    """
    Decide if a match record needs to be re-fetched from the API.

    Logic:
      LIVE/PAUSED           → always re-fetch
      FINISHED with missing data → re-fetch if within lookback window
      TIMED/SCHEDULED past  → re-fetch if utcDate is in the past and within window
      Future SCHEDULED      → skip
      POSTPONED             → skip (manual re-fetch if needed)
      CANCELLED/AWARDED/etc → skip
    """
    status   = match.get("status", "")
    utc_date = _parse_utc(match.get("utcDate"))

    # Always grab live matches
    if status in LIVE_STATUSES:
        return True

    # Skip statuses we never auto-update
    if status in SKIP_STATUSES:
        return False

    # No date — can't make a decision, skip
    if utc_date is None:
        return False

    cutoff = now - timedelta(hours=lookback_hours)

    # Only look at matches within the lookback window
    if utc_date < cutoff:
        return False

    # Future matches — not played yet
    if utc_date > now:
        # Edge case: if it's SCHEDULED but within 2 hours of now — might have started
        if status in SCHEDULED_STATUSES and (utc_date - now) < timedelta(hours=2):
            return True
        return False

    # utcDate is in the past from here on

    if status in SCHEDULED_STATUSES:
        # Was scheduled but time has passed — clearly needs update
        return True

    if status == FINISHED_STATUS:
        # Check if the result data is actually populated
        score     = match.get("score") or {}
        full_time = score.get("fullTime") or {}
        home_g    = full_time.get("home")
        away_g    = full_time.get("away")

        # Score is null — data didn't arrive when originally fetched
        if home_g is None and away_g is None:
            return True

        # Goals happened but list is empty — arrived late
        goals_recorded = (home_g or 0) + (away_g or 0)
        goals_in_list  = len(match.get("goals") or [])
        if goals_recorded > 0 and goals_in_list == 0:
            return True

        # Lineup/bench empty on a finished match (API populates post-match)
        home_team = match.get("homeTeam") or {}
        away_team = match.get("awayTeam") or {}
        if (
            not home_team.get("lineup")
            and not home_team.get("bench")
            and not away_team.get("lineup")
            and not away_team.get("bench")
            and utc_date > cutoff  # only within lookback window
        ):
            return True

        return False

    return False


def _read_existing_matches(path: str) -> list[dict]:
    """
    Read an existing matches JSON file.
    Returns [] if file doesn't exist, is unreadable, or has unexpected structure.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("File not found: %s — run fetch_matches.py first", path)
        return []

    try:
        with open(p, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read %s: %s", path, exc)
        return []

    # safe_write wraps everything in {"_meta": ..., "data": [...]}
    data = payload.get("data")
    if not isinstance(data, list):
        logger.error("Unexpected structure in %s — data is not a list", path)
        return []

    return data


# ── CORE AUDIT FUNCTION ───────────────────────────────────────────────────────

def audit_competition(
    code: str,
    matches_dir: str,
    lookback_hours: int,
) -> tuple[int, int, int]:
    """
    Audit and patch matches for one competition.

    Returns (stale_count, updated_count, skipped_count).
    """
    path    = f"{matches_dir}/{code}.json"
    now     = datetime.now(timezone.utc)
    matches = _read_existing_matches(path)

    if not matches:
        logger.info("  %s: no existing matches — skipping audit", code)
        return 0, 0, 0

    # Build a quick id → index map for O(1) in-place replacement
    id_to_idx: dict[int, int] = {
        m.get("id"): i for i, m in enumerate(matches) if m.get("id")
    }

    stale_matches = [
        m for m in matches
        if _is_stale(m, now, lookback_hours)
    ]

    logger.info(
        "  %s: %d total, %d stale (lookback=%dh)",
        code, len(matches), len(stale_matches), lookback_hours,
    )

    if not stale_matches:
        return 0, 0, len(matches)

    updated = 0
    skipped = 0

    for stale in stale_matches:
        match_id   = stale.get("id")
        match_date = stale.get("utcDate", "?")
        home       = (stale.get("homeTeam") or {}).get("name", "?")
        away       = (stale.get("awayTeam") or {}).get("name", "?")

        logger.info(
            "    Re-fetching match %s (%s vs %s on %s, status=%s) ...",
            match_id, home, away, match_date, stale.get("status"),
        )

        raw = fetch(f"/matches/{match_id}")

        if raw is None:
            logger.warning(
                "    Failed to fetch match %s — leaving existing record", match_id
            )
            skipped += 1
            continue

        # The single-match endpoint wraps the match in a top-level object
        # GET /matches/{id} returns the match directly (not under a "matches" key)
        # but some versions nest it — handle both
        match_data = raw if raw.get("id") else raw.get("match") or raw

        if not match_data.get("id"):
            logger.warning("    Unexpected response structure for match %s — skipping", match_id)
            skipped += 1
            continue

        try:
            flattened = flatten_match(match_data, code)
        except Exception as exc:
            logger.warning("    flatten_match failed for %s: %s — skipping", match_id, exc)
            skipped += 1
            continue

        # Patch in-place
        idx = id_to_idx.get(match_id)
        if idx is not None:
            matches[idx] = flattened
            updated += 1
            logger.info(
                "    ✓ Patched match %s → status=%s, score=%s-%s",
                match_id,
                flattened.get("status"),
                (flattened.get("score") or {}).get("fullTime", {}).get("home"),
                (flattened.get("score") or {}).get("fullTime", {}).get("away"),
            )
        else:
            # Match wasn't in the existing file (edge case: new match added mid-season)
            logger.info("    Match %s not in existing file — appending", match_id)
            matches.append(flattened)
            updated += 1

    if updated > 0:
        # Re-sort chronologically before writing back
        matches.sort(key=lambda m: m.get("utcDate") or "")
        if safe_write(path, matches):
            logger.info("  %s: wrote %d total matches (%d updated)", code, len(matches), updated)
        else:
            logger.error("  %s: failed to write patched file", code)
    else:
        logger.info("  %s: no updates needed", code)

    return len(stale_matches), updated, skipped


# ── MAIN RUN ──────────────────────────────────────────────────────────────────

def run(mode: str = "recent") -> None:
    """
    Run the audit across all tracked competitions.

    mode = "live"    →  lookback 6h   — catch matches in play right now
    mode = "recent"  →  lookback 72h  — patch last 3 days
    mode = "all"     →  lookback 168h — patch entire last matchweek
    """
    lookback_map = {
        "live":   LOOKBACK_HOURS_LIVE,
        "recent": LOOKBACK_HOURS_RECENT,
        "all":    LOOKBACK_HOURS_ALL,
    }
    lookback_hours = lookback_map[mode]

    paths       = get_season_paths()
    season      = paths["season"]
    matches_dir = paths["matches_dir"]

    logger.info(
        "=== audit_matches [mode=%s, lookback=%dh, season=%s] started at %s ===",
        mode, lookback_hours, season, datetime.now(timezone.utc).isoformat(),
    )

    total_stale   = 0
    total_updated = 0
    total_skipped = 0

    for code in TRACKED_COMPETITIONS:
        stale, updated, skipped = audit_competition(code, matches_dir, lookback_hours)
        total_stale   += stale
        total_updated += updated
        total_skipped += skipped

    logger.info(
        "=== audit_matches complete [mode=%s, season=%s]: "
        "%d stale found, %d updated, %d skipped ===",
        mode, season, total_stale, total_updated, total_skipped,
    )

    if total_stale > 0 and total_updated == 0:
        logger.warning(
            "Found %d stale matches but updated 0 — check API key and rate limits",
            total_stale,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Audit and patch stale match records without a full re-fetch."
    )
    parser.add_argument(
        "--mode",
        choices=["live", "recent", "all"],
        default="recent",
        help=(
            "live   = matches from last 6h (for cron every 15min)  |  "
            "recent = last 3 days (every 3h)  |  "
            "all    = last 7 days (daily)"
        ),
    )
    args = parser.parse_args()
    run(mode=args.mode)
