#!/usr/bin/env python3
"""
main.py — Single-command full data pipeline for goal4u-data.

Two separate flows (as designed in architecture notes):
  League flow    : competitions → teams → matches → match_links
  Tournament flow: worldcup | euro  (each is all-in-one, then match_links)

Step order
──────────
  1. competitions   league competition info + standings + scorers
  2. teams          club squads (per competition)
  3. matches        fixtures + results  (postpone-aware)
  4. match_links    yallashoot URL index  (requires matches to exist first)
  5. worldcup       World Cup all-in-one  (independent of league season)
  6. euro           UEFA Euro all-in-one  (independent of league season)

  match_stats scrapes one yallashoot page per match using N parallel workers
  (default 8).  It is wired into the pipeline but also safe to run standalone.

Season behaviour
────────────────
  No --season         → current season auto-resolved by config.py
  --season 2024       → historical 2024-2025  (league steps only)
  --only worldcup     → fetch only WC data (year from config.TOURNAMENT_YEARS)
  --only euro         → fetch only Euro data
  --competition PL    → restrict league steps to one competition

  Tournament steps (worldcup, euro) are always independent of --season
  because tournaments have their own year in config.TOURNAMENT_YEARS.

  match_links runs after matches (league) or after worldcup/euro (tournaments).
  It reads the matches.json that was just written so IDs are always fresh.

match_stats concurrency flags  (only affect the match_stats step)
─────────────────────────────
  --workers N      parallel scrape threads (default 8, safe range 4–12)
  --limit N        max matches to scrape this run (default: all pending)
  --checkpoint N   save stats.json every N successes (default 25)
  --force          re-scrape matches already in stats.json

Usage
─────
  python main.py                                          # full pipeline, current season
  python main.py --season 2024                            # historical 2024-2025 leagues
  python main.py --only matches match_links               # matches + links only
  python main.py --only worldcup                          # World Cup only
  python main.py --only worldcup euro                     # both tournaments
  python main.py --skip teams worldcup euro               # leagues, no teams, no tournaments
  python main.py --competition PL --only matches          # PL matches only
  python main.py --only worldcup --mode standings         # WC standings only
  python main.py --only match_stats --workers 12          # fast stats scrape, 12 threads
  python main.py --only match_stats --competition PL      # stats for PL only
  python main.py --only match_stats --limit 100           # scrape up to 100 matches
  python main.py --only match_stats --force               # re-scrape everything
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("main")


# ── Step callables ────────────────────────────────────────────────────────────

def _run_competitions(
    season: Optional[int],
    competition: Optional[str],
    mode: str = "all",
    **_,
) -> None:
    from workers.fetch_competitions import run
    run(mode=mode, season=season, competition=competition)


def _run_teams(
    season: Optional[int],
    competition: Optional[str],
    **_,
) -> None:
    from workers.fetch_teams import run
    run(season=season, competition=competition)


def _run_matches(
    season: Optional[int],
    competition: Optional[str],
    **_,
) -> None:
    from workers.fetch_matches import run, run_historical
    if season is not None:
        run_historical(season, competition=competition)
    else:
        run(competition=competition)


def _run_match_links(
    season: Optional[int],
    competition: Optional[str],
    **_,
) -> None:
    """
    Fetch yallashoot match-page URLs and cross-reference them against
    matches.json to produce match_stats_links.json.

    Runs for leagues (using the current / specified season) and tournaments
    (WC, EC — year always from config.TOURNAMENT_YEARS, season arg ignored).

    When --competition is set only that code is processed.
    When no --competition is set, all LEAGUE_COMPETITIONS are processed plus
    any tournament codes that have their own fetch already wired in
    (WC if worldcup ran, EC if euro ran).  To keep this simple we only process
    league codes here; tournament match_links are triggered by --only match_links
    combined with --competition WC|EC when you want them explicitly.
    """
    from workers.fetch_match_links import process_one
    from config import LEAGUE_COMPETITIONS, get_season_paths
    from workers.tournament_paths import is_tournament

    if season is not None:
        season_str = f"{season}-{season + 1}"
    else:
        season_str = get_season_paths()["season"]

    if competition:
        # Single competition — works for both leagues and tournaments
        process_one(competition.upper(), season_str=season_str)
    else:
        # All league competitions (tournaments handled via --competition WC/EC)
        codes = [c for c in LEAGUE_COMPETITIONS if not is_tournament(c)]
        for code in codes:
            process_one(code, season_str=season_str)
            time.sleep(2)   # polite gap between yallashoot page scrapes

def _run_match_stats(
    season: Optional[int],
    competition: Optional[str],
    mode: str = "all",
    workers: int = 8,
    limit: Optional[int] = None,
    checkpoint: int = 25,
    force: bool = False,
    **_,
) -> None:
    from workers.fetch_match_stats import run_competition_audit, run_all_competitions_audit
    from config import get_season_paths

    season_str = f"{season}-{season + 1}" if season else get_season_paths()["season"]

    if competition:
        run_competition_audit(
            code=competition.upper(),
            season_str=season_str,
            workers=workers,
            limit=limit,
            checkpoint_n=checkpoint,
            force=force,
        )
    else:
        run_all_competitions_audit(
            season_str=season_str,
            workers=workers,
            limit=limit,
            checkpoint_n=checkpoint,
            force=force,
        )


        
def _run_worldcup(
    mode: str = "all",
    **_,               # absorbs season / competition kwargs so callers are uniform
) -> None:
    from workers.fetch_worldCup import run
    run(mode=mode)


def _run_euro(
    mode: str = "all",
    **_,
) -> None:
    from workers.fetch_Euro import run
    run(mode=mode)


# ── Step registry ─────────────────────────────────────────────────────────────
# Tuple: (name, short description, callable, [hard-dependency names])

STEPS = [
    (
        "competitions",
        "League competition info + standings + scorers",
        _run_competitions,
        [],
    ),
    (
        "teams",
        "Club info + full squads",
        _run_teams,
        [],
    ),
    (
        "matches",
        "League match fixtures + results  (postpone-aware)",
        _run_matches,
        [],
    ),
    (
        "match_links",
        "Yallashoot URL index per competition  (uses matches.json)",
        _run_match_links,
        [],   # soft dependency on matches — we warn but don't hard-block
    ),
    (
        "match_stats",
        "Scrape detailed stats from yallashoot",
        _run_match_stats,
        ["match_links"], # Depends on match_links being finished first
    ),
    (
        "worldcup",
        "World Cup all-in-one  (year from config.TOURNAMENT_YEARS['WC'])",
        _run_worldcup,
        [],
    ),
    (
        "euro",
        "UEFA Euro all-in-one  (year from config.TOURNAMENT_YEARS['EC'])",
        _run_euro,
        [],
    ),
]

STEP_NAMES: list[str]  = [s[0] for s in STEPS]
LEAGUE_STEPS           = {"competitions", "teams", "matches", "match_links"}
TOURNAMENT_STEPS       = {"worldcup", "euro"}


# ── Pipeline runner ───────────────────────────────────────────────────────────

def run_pipeline(
    only: Optional[list[str]] = None,
    skip: Optional[list[str]] = None,
    season: Optional[int]     = None,
    competition: Optional[str]= None,
    mode: str                 = "all",
    workers: int              = 8,
    limit: Optional[int]      = None,
    checkpoint: int           = 25,
    force: bool               = False,
) -> int:
    """
    Run the pipeline and return exit code (0 = success, 1 = any failure).

    `mode` is forwarded to competition + tournament steps (e.g. "standings" to
    refresh only standings without re-downloading matches).

    `workers`, `limit`, `checkpoint`, `force` are forwarded exclusively to the
    match_stats step and have no effect on any other step.
    """
    from config import get_season_paths  # type: ignore[import]

    if season is not None:
        season_label = f"{season}-{season + 1}  (historical)"
    else:
        season_label = f"{get_season_paths()['season']}  (current)"

    started_at = datetime.now(timezone.utc)

    logger.info("=" * 72)
    logger.info("main.py pipeline started  |  season: %s", season_label)
    if competition:
        logger.info("  competition filter: %s", competition)
    logger.info(
        "  match_stats: workers=%d  limit=%s  checkpoint=%d  force=%s",
        workers, limit if limit is not None else "all", checkpoint, force,
    )
    logger.info("=" * 72)

    # Resolve active steps
    active: list[str] = only if only else STEP_NAMES
    active = [n for n in active if n not in (skip or [])]

    # Warn if --season is combined with tournament steps (season is ignored there)
    if season is not None and any(s in active for s in TOURNAMENT_STEPS):
        logger.warning(
            "--season is ignored for tournament steps (worldcup, euro). "
            "Tournament years come from config.TOURNAMENT_YEARS."
        )

    # Soft-warn if match_links is active but matches isn't
    if "match_links" in active and "matches" not in active:
        logger.warning(
            "match_links step is active but matches step is not — "
            "match_stats_links.json will be cross-referenced against whatever "
            "matches.json is already on disk (may be stale)."
        )

    completed: set[str] = set()
    results: list[tuple[str, str, float]] = []

    for name, desc, fn, deps in STEPS:
        if name not in active:
            logger.info("  –  %-14s  skipped (not selected)", name)
            results.append((name, "skipped", 0.0))
            continue

        # Hard dependency check
        blocking = [d for d in deps if d in active and d not in completed]
        if blocking:
            logger.warning(
                "  ✗  %-14s  skipped — dependency not completed: %s",
                name, ", ".join(blocking),
            )
            results.append((name, f"skipped (dep: {', '.join(blocking)})", 0.0))
            continue

        logger.info("")
        logger.info("┌─ Step: %-12s  %s", name.upper(), desc)
        logger.info("│")

        t0     = time.monotonic()
        status = "ok"

        try:
            # Pass all context; each step function ignores what it doesn't need
            fn(
                season=season,
                competition=competition,
                mode=mode,
                workers=workers,
                limit=limit,
                checkpoint=checkpoint,
                force=force,
            )
            completed.add(name)
        except SystemExit as exc:
            status = f"failed (exit {exc.code})"
            logger.error("│  %s exited with code %s", name, exc.code)
        except Exception as exc:
            status = f"failed ({type(exc).__name__}: {exc})"
            logger.error("│  %s raised:", name, exc_info=True)

        elapsed = time.monotonic() - t0
        results.append((name, status, elapsed))
        marker = "✓" if status == "ok" else "✗"
        logger.info("│")
        logger.info("└─ %s  %s  (%.1fs)", marker, name, elapsed)

    # Summary
    total = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info("")
    logger.info("=" * 72)
    logger.info("Pipeline complete — %.0fs  |  season: %s", total, season_label)
    logger.info("=" * 72)
    logger.info("")
    logger.info("  %-14s  %-40s  %s", "STEP", "STATUS", "TIME")
    logger.info("  " + "-" * 62)

    for name, status, elapsed in results:
        if status == "ok":
            marker, t = "✓", f"{elapsed:.1f}s"
        elif "skipped" in status:
            marker, t = "–", ""
        else:
            marker, t = "✗", f"{elapsed:.1f}s"
        logger.info("  %s  %-14s  %-40s  %s", marker, name, status, t)

    logger.info("")

    failures = [n for n, s, _ in results if s.startswith("failed")]
    if failures:
        logger.error("Failed steps: %s", ", ".join(failures))
        return 1
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    step_list = "\n".join(
        f"  {name:<14}  {desc}"
        for name, desc, _, _ in STEPS
    )

    parser = argparse.ArgumentParser(
        description=(
            "goal4u-data pipeline — fetches leagues and/or tournament data "
            "and writes structured JSON into the correct folder layout."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Steps (in run order):\n{step_list}\n\n"
            "Examples:\n"
            "  python main.py                                              # full pipeline\n"
            "  python main.py --season 2024                               # historical 2024-2025\n"
            "  python main.py --only worldcup                             # WC only\n"
            "  python main.py --only worldcup --mode matches              # WC matches only\n"
            "  python main.py --skip worldcup euro                        # leagues only\n"
            "  python main.py --competition PL --only matches             # PL matches\n"
            "  python main.py --only match_links --competition WC         # WC links only\n"
            "  python main.py --skip match_links                          # skip link scraping\n"
            "  python main.py --only match_stats --workers 12             # fast stats, 12 threads\n"
            "  python main.py --only match_stats --competition PL         # PL stats only\n"
            "  python main.py --only match_stats --limit 100              # cap at 100 matches\n"
            "  python main.py --only match_stats --force                  # re-scrape all\n"
            "  python main.py --only match_stats --workers 8 --checkpoint 50  # checkpoint every 50\n"
        ),
    )

    parser.add_argument(
        "--season",
        type=int,
        default=None,
        metavar="YEAR",
        help=(
            "Start year of a historical league season "
            "(e.g. --season 2024 → data/2024-2025/). "
            "Omit for the current season. Ignored by worldcup / euro steps."
        ),
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--only",
        nargs="+",
        metavar="STEP",
        choices=STEP_NAMES,
        help="Run only these steps.",
    )
    group.add_argument(
        "--skip",
        nargs="+",
        metavar="STEP",
        choices=STEP_NAMES,
        help="Skip these steps and run everything else.",
    )

    parser.add_argument(
        "--competition",
        type=str,
        default=None,
        metavar="CODE",
        help=(
            "Restrict league steps to one competition code, e.g. PL or BL1. "
            "For match_links this also works with tournament codes: --competition WC. "
            "Has no effect on worldcup / euro steps."
        ),
    )

    parser.add_argument(
        "--mode",
        type=str,
        default="all",
        metavar="MODE",
        help=(
            "Section mode forwarded to competition + tournament steps. "
            "Values: all | info | standings | scorers | teams | matches. "
            "Default: all."
        ),
    )

    # ── match_stats concurrency flags ─────────────────────────────────────────
    stats_group = parser.add_argument_group(
        "match_stats options",
        "These flags are forwarded only to the match_stats step.",
    )
    stats_group.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help=(
            "Parallel scrape threads for match_stats (default: 8). "
            "Safe range: 4–12 with a Webshare rotating proxy."
        ),
    )
    stats_group.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Max matches to scrape per match_stats run. "
            "Omit to scrape all pending matches (no cap)."
        ),
    )
    stats_group.add_argument(
        "--checkpoint",
        type=int,
        default=25,
        metavar="N",
        help=(
            "Save stats.json every N successful scrapes (default: 25). "
            "Protects against data loss if a run is interrupted."
        ),
    )
    stats_group.add_argument(
        "--force",
        action="store_true",
        help="Re-scrape matches that already have an entry in stats.json.",
    )

    args = parser.parse_args()
    code = run_pipeline(
        only=args.only,
        skip=args.skip,
        season=args.season,
        competition=args.competition,
        mode=args.mode,
        workers=args.workers,
        limit=args.limit,
        checkpoint=args.checkpoint,
        force=args.force,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()