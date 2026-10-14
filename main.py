#!/usr/bin/env python3
"""
main.py — Single-command full data pipeline for goal4u-data.

Two separate flows (as designed in architecture notes):
  League flow    : competitions → teams → tm_scraper → matches
  Tournament flow: worldcup | euro  (each is all-in-one)

Step order
──────────
  1. competitions   league competition info + standings + scorers
  2. teams          club squads (per competition)
  3. tm_scraper     Transfermarkt league info, market values, player profiles
  4. matches        fixtures + results  (postpone-aware)
  5. worldcup       World Cup all-in-one  (independent of league season)
  6. euro           UEFA Euro all-in-one  (independent of league season)

  tm_scraper runs after teams so that teams.json is on disk for player ID
  enrichment.  It writes to data/league_info/, data/team_informations/, and
  data/player_information/.

Season behaviour
────────────────
  No --season         → current season auto-resolved by config.py
  --season 2024       → historical 2024-2025  (league steps only)
  --only worldcup     → fetch only WC data (year from config.TOURNAMENT_YEARS)
  --only euro         → fetch only Euro data
  --only tm_scraper   → Transfermarkt scrape only (all leagues, current season)
  --competition PL    → restrict league steps to one competition

  Tournament steps (worldcup, euro) are always independent of --season
  because tournaments have their own year in config.TOURNAMENT_YEARS.

tm_scraper flags  (only affect the tm_scraper step)
────────────────
  --fullscrape     re-fetch and overwrite all TM JSON data even if it exists
                   (image assets on disk are always skipped)

Usage
─────
  python main.py                                          # full pipeline, current season
  python main.py --season 2024                            # historical 2024-2025 leagues
  python main.py --only matches                           # matches only
  python main.py --only tm_scraper                        # TM scrape only, all leagues
  python main.py --only tm_scraper --competition PL       # TM scrape, PL only
  python main.py --only tm_scraper --fullscrape           # re-fetch all TM data
  python main.py --only worldcup                          # World Cup only
  python main.py --only worldcup euro                     # both tournaments
  python main.py --skip teams worldcup euro               # leagues, no teams, no tournaments
  python main.py --competition PL --only matches          # PL matches only
  python main.py --only worldcup --mode standings         # WC standings only
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


def _run_tm_scraper(
    season: Optional[int]  = None,
    competition: Optional[str] = None,
    fullscrape: bool = False,
    **_,
) -> None:
    """
    Run the Transfermarkt scraper (tm_scraper.runner) for one or all leagues.

    Derives the season string from --season (e.g. 2024 → "2024-2025") or
    falls back to the current season from config.get_season_paths().

    --competition restricts to a single league code (PL, PD, SA, BL1, FL1).
    --fullscrape is forwarded as force=True to re-fetch all JSON data files.
    Images on disk are always skipped regardless of fullscrape.

    Soft dependency: teams.json must already exist on disk for the requested
    season/league (written by the 'teams' step) so player IDs can be resolved.
    """
    from config import get_season_paths
    from tm_scraper.runner import run_season
    from tm_scraper.config_tm import LEAGUE_MAPPING

    if season is not None:
        season_str = f"{season}-{season + 1}"
    else:
        season_str = get_season_paths()["season"]

    if competition:
        code = competition.upper()
        if code not in LEAGUE_MAPPING:
            logger.error(
                "[tm_scraper] Unknown league code: %s (valid: %s)",
                code, list(LEAGUE_MAPPING),
            )
            return
        logger.info("[tm_scraper] Running for league=%s  season=%s  fullscrape=%s",
                    code, season_str, fullscrape)
        run_season(code, season_str, force=fullscrape)
    else:
        leagues = list(LEAGUE_MAPPING.keys())
        logger.info("[tm_scraper] Running for all leagues=%s  season=%s  fullscrape=%s",
                    leagues, season_str, fullscrape)
        for code in leagues:
            run_season(code, season_str, force=fullscrape)


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
        "tm_scraper",
        "Transfermarkt league info, market values, player profiles",
        _run_tm_scraper,
        ["teams"],   # needs teams.json on disk for player ID enrichment
    ),
    (
        "matches",
        "League match fixtures + results  (postpone-aware)",
        _run_matches,
        [],
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
LEAGUE_STEPS           = {"competitions", "teams", "tm_scraper", "matches"}
TOURNAMENT_STEPS       = {"worldcup", "euro"}


# ── Pipeline runner ───────────────────────────────────────────────────────────

def run_pipeline(
    only: Optional[list[str]] = None,
    skip: Optional[list[str]] = None,
    season: Optional[int]     = None,
    competition: Optional[str]= None,
    mode: str                 = "all",
    fullscrape: bool          = False,
) -> int:
    """
    Run the pipeline and return exit code (0 = success, 1 = any failure).

    `mode` is forwarded to competition + tournament steps (e.g. "standings" to
    refresh only standings without re-downloading matches).

    `fullscrape` is forwarded exclusively to the tm_scraper step — when True it
    re-fetches all Transfermarkt JSON data files even if they already have data.
    Image assets on disk are always skipped regardless of this flag.
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
    logger.info("  tm_scraper:  fullscrape=%s", fullscrape)
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
                fullscrape=fullscrape,
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
            "  python main.py --only tm_scraper --fullscrape              # re-fetch all TM data\n"
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

    # ── tm_scraper flags ──────────────────────────────────────────────────────
    tm_group = parser.add_argument_group(
        "tm_scraper options",
        "These flags are forwarded only to the tm_scraper step.",
    )
    tm_group.add_argument(
        "--fullscrape",
        action="store_true",
        help=(
            "Re-fetch and overwrite all Transfermarkt JSON data files even if "
            "they already have data. Image assets on disk are always skipped."
        ),
    )

    args = parser.parse_args()
    code = run_pipeline(
        only=args.only,
        skip=args.skip,
        season=args.season,
        competition=args.competition,
        mode=args.mode,
        fullscrape=args.fullscrape,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()