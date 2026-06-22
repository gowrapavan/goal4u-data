#!/usr/bin/env python3
"""
main.py — single-command full-season data pipeline.

Runs all workers in dependency order:

  1. competitions  →  competition metadata + standings + scorers
  2. teams         →  club info + full squads
  3. matches       →  fixtures + results          ← match_links depends on this
  4. match_links   →  YallaShoot URL mapping      ← needs matches on disk first

Season behaviour
────────────────
  No --season flag   →  current season (auto-resolved by config.py)
                         e.g. June 2026 → writes to data/2026-2027/
  --season 2024      →  historical 2024-2025 season
                         writes to data/2024-2025/ using the same folder layout

  Under the hood each worker already handles season routing:
    fetch_competitions : run(season=2024)  → standings + scorers only
                         (bare competition metadata has no historical API variant)
    fetch_teams        : run(season=2024)  → ?season=2024 on the teams endpoint
    fetch_matches      : run_historical(2024) → longer timeout + monthly fallback
    fetch_match_links  : process_one(code, season_str="2024-2025")
                         → uses historical matches file + saves to correct folder

Usage:
    python main.py                                    # current season (auto)
    python main.py --season 2024                      # historical 2024-2025
    python main.py --skip match_links                 # skip URL scraping
    python main.py --skip teams match_links           # skip multiple steps
    python main.py --only matches                     # run just one step
    python main.py --season 2023 --only matches       # historical, one step
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("main")


# ── Individual step callables ─────────────────────────────────────────────────
# Each callable accepts season: int | None.
#   None  → auto-resolve current season from config (default)
#   int   → fetch that historical season, e.g. 2024 → 2024-2025 folder

def _run_competitions(season: int | None = None) -> None:
    from workers.fetch_competitions import run
    run(mode="all", season=season)


def _run_teams(season: int | None = None) -> None:
    from workers.fetch_teams import run
    run(season=season)


def _run_matches(season: int | None = None) -> None:
    from workers.fetch_matches import run, run_historical
    if season is not None:
        run_historical(season)   # longer timeout + monthly-chunk fallback
    else:
        run()


def _run_match_links(season: int | None = None) -> None:
    from workers.fetch_match_links import process_one, COMPETITIONS
    # match_links uses the string folder format, e.g. "2024-2025"
    season_str = f"{season}-{season + 1}" if season is not None else None
    for i, code in enumerate(COMPETITIONS):
        if i > 0:
            time.sleep(2)   # be polite between large HTML scrapes
        process_one(code, season_str=season_str)


# ── Step registry ─────────────────────────────────────────────────────────────
# Each entry: (name, short description, callable, [dependency step names])
# Steps run in list order; dependency names must appear earlier in the list.

STEPS: list[tuple[str, str, object, list[str]]] = [
    (
        "competitions",
        "Competition metadata + standings + scorers",
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
        "Match fixtures + results",
        _run_matches,
        [],
    ),
    (
        "match_links",
        "YallaShoot URL mapping  [requires: matches]",
        _run_match_links,
        ["matches"],   # match_links fuzzy-matches against the matches files
    ),
]

STEP_NAMES: list[str] = [s[0] for s in STEPS]


# ── Pipeline runner ───────────────────────────────────────────────────────────

def run_pipeline(
    only: list[str] | None = None,
    skip: list[str] | None = None,
    season: int | None = None,
) -> int:
    """
    Execute the pipeline and return an exit code (0 = all OK, 1 = any failure).

    Steps whose hard dependencies didn't complete successfully are skipped
    automatically — e.g. if --only match_links is passed without matches having
    run this session, a clear warning is logged rather than producing bad output.
    """
    from config import get_season_paths

    # Human-readable season label for logging
    if season is not None:
        season_label = f"{season}-{season + 1}  (historical)"
    else:
        season_label = f"{get_season_paths()['season']}  (current)"

    started_at = datetime.now(timezone.utc)

    logger.info("=" * 70)
    logger.info("main.py pipeline started  |  season: %s", season_label)
    logger.info("=" * 70)

    # Resolve which step names are active this run
    active_names: list[str] = only if only else STEP_NAMES
    active_names = [n for n in active_names if n not in (skip or [])]

    completed: set[str] = set()   # steps that finished without error
    results: list[tuple[str, str, float]] = []  # (name, status, elapsed_s)

    for name, desc, fn, deps in STEPS:

        # ── Skip if not in active set ─────────────────────────────────────────
        if name not in active_names:
            logger.info("  –  %-14s  skipped (not selected)", name)
            results.append((name, "skipped", 0.0))
            continue

        # ── Skip if a hard dependency didn't complete this run ────────────────
        # Only enforce when the dep was actually supposed to run (i.e. it's in
        # active_names). If the user explicitly skipped the dep, warn but proceed
        # so they can run match_links stand-alone when matches are already on disk.
        blocking = [
            d for d in deps
            if d in active_names and d not in completed
        ]
        if blocking:
            logger.warning(
                "  ✗  %-14s  skipped — dependency failed or was skipped: %s",
                name, ", ".join(blocking),
            )
            results.append((name, f"skipped (dep failed: {', '.join(blocking)})", 0.0))
            continue

        # ── Run the step ──────────────────────────────────────────────────────
        logger.info("")
        logger.info("┌─ Step: %-12s  %s", name.upper(), desc)
        logger.info("│")

        t0     = time.monotonic()
        status = "ok"

        try:
            fn(season=season)
            completed.add(name)

        except SystemExit as exc:
            # Workers call sys.exit(1) on total failure (e.g. no API key).
            status = f"failed (exit {exc.code})"
            logger.error("│  Step %s exited with code %s — see logs above.", name, exc.code)

        except Exception as exc:
            status = f"failed ({type(exc).__name__}: {exc})"
            logger.error("│  Step %s raised an unhandled exception:", name, exc_info=True)

        elapsed = time.monotonic() - t0
        results.append((name, status, elapsed))

        marker = "✓" if status == "ok" else "✗"
        logger.info("│")
        logger.info("└─ %s  %s  (%.1fs)", marker, name, elapsed)

    # ── Summary ───────────────────────────────────────────────────────────────
    total_elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()

    logger.info("")
    logger.info("=" * 70)
    logger.info(
        "Pipeline complete — %.0fs total  |  season: %s",
        total_elapsed, season_label,
    )
    logger.info("=" * 70)
    logger.info("")
    logger.info("  %-14s  %-36s  %s", "STEP", "STATUS", "TIME")
    logger.info("  " + "-" * 60)

    for name, status, elapsed in results:
        if status == "ok":
            marker, time_str = "✓", f"{elapsed:.1f}s"
        elif "skipped" in status:
            marker, time_str = "–", ""
        else:
            marker, time_str = "✗", f"{elapsed:.1f}s"
        logger.info("  %s  %-14s  %-36s  %s", marker, name, status, time_str)

    logger.info("")

    failures = [name for name, status, _ in results if status.startswith("failed")]
    if failures:
        logger.error("Failed steps: %s", ", ".join(failures))
        return 1

    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    step_list = "\n".join(
        f"  {name:<14}  {desc}"
        for name, desc, _, deps in STEPS
    )

    parser = argparse.ArgumentParser(
        description=(
            "Full-season data pipeline — fetches competitions, teams, matches, "
            "and YallaShoot match links for the current season."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Steps (in run order):\n{step_list}\n\n"
            "Examples:\n"
            "  python main.py                             # full pipeline\n"
            "  python main.py --skip match_links          # skip URL scraping\n"
            "  python main.py --skip teams match_links    # skip multiple\n"
            "  python main.py --only matches              # one step only\n"
            "  python main.py --only matches match_links  # two steps\n"
        ),
    )

    parser.add_argument(
        "--season",
        type=int,
        default=None,
        metavar="YEAR",
        help=(
            "Start year of a historical season to fetch "
            "(e.g. --season 2024 fetches the 2024-2025 season and writes to "
            "data/2024-2025/). Omit to fetch the current season (auto-resolved "
            "from today's date via config.py)."
        ),
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--only",
        nargs="+",
        metavar="STEP",
        choices=STEP_NAMES,
        help="Run only these steps (in pipeline order).",
    )
    group.add_argument(
        "--skip",
        nargs="+",
        metavar="STEP",
        choices=STEP_NAMES,
        help="Skip these steps and run everything else.",
    )

    args  = parser.parse_args()
    code  = run_pipeline(only=args.only, skip=args.skip, season=args.season)
    sys.exit(code)


if __name__ == "__main__":
    main()