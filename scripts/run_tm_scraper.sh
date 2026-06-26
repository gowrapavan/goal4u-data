#!/usr/bin/env bash
# scripts/run_tm_scraper.sh
# ──────────────────────────────────────────────────────────────────────────
# Shared driver for the Transfermarkt scraper step (main.py --only tm_scraper).
# Used by both .github/workflows/tm-scraper-yearly.yml and
# .github/workflows/tm-scraper-manual.yml so the looping logic lives in one
# place instead of being duplicated in YAML.
#
# Env vars (all optional, all strings):
#
#   SEASON            Start year, e.g. "2024" -> season "2024-2025".
#                      Empty -> see SCAN_ALL_SEASONS below.
#
#   LEAGUES           Comma-separated league codes, e.g. "PL,PD,SA".
#                      Empty -> all leagues (main.py's own default).
#
#   FULLSCRAPE        "true" -> pass --fullscrape (force re-fetch every
#                      JSON file even if it already has data). Image
#                      assets on disk are still always skipped — that's
#                      handled inside runner.py, not here.
#
#   SCAN_ALL_SEASONS  "true" -> when SEASON is empty, discover every
#                      season folder already present under data/
#                      (pattern: YYYY-YYYY) and run tm_scraper once per
#                      season found. "false" -> empty SEASON just means
#                      "current season" (config.py auto-resolves it).
#
# Adjust the SEASON_DIR_REGEX below if your data/ layout uses a different
# season folder naming convention.
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

SEASON="${SEASON:-}"
LEAGUES="${LEAGUES:-}"
FULLSCRAPE="${FULLSCRAPE:-false}"
SCAN_ALL_SEASONS="${SCAN_ALL_SEASONS:-false}"

SEASON_DIR_REGEX='.*/[0-9]{4}-[0-9]{4}$'

FULLSCRAPE_FLAG=()
if [ "$FULLSCRAPE" = "true" ]; then
  FULLSCRAPE_FLAG=(--fullscrape)
fi

LEAGUE_ARR=()
if [ -n "$LEAGUES" ]; then
  IFS=',' read -ra LEAGUE_ARR <<< "$LEAGUES"
fi

run_for_season() {
  # $1 = season start year, or "" for current season
  local season_arg="$1"
  local season_opt=()
  if [ -n "$season_arg" ]; then
    season_opt=(--season "$season_arg")
  fi

  if [ -z "$LEAGUES" ]; then
    echo "▶ tm_scraper  season=${season_arg:-current}  leagues=ALL  fullscrape=$FULLSCRAPE"
    python main.py --only tm_scraper "${season_opt[@]}" "${FULLSCRAPE_FLAG[@]}"
  else
    for code in "${LEAGUE_ARR[@]}"; do
      code="$(echo "$code" | xargs)"   # trim whitespace
      [ -z "$code" ] && continue
      echo "▶ tm_scraper  season=${season_arg:-current}  league=$code  fullscrape=$FULLSCRAPE"
      python main.py --only tm_scraper "${season_opt[@]}" --competition "$code" "${FULLSCRAPE_FLAG[@]}"
    done
  fi
}

if [ -n "$SEASON" ]; then
  echo "Season explicitly given: $SEASON"
  run_for_season "$SEASON"

elif [ "$SCAN_ALL_SEASONS" = "true" ]; then
  echo "No season given — scanning data/ for existing season folders (pattern YYYY-YYYY)..."
  mapfile -t SEASONS < <(
    find data -maxdepth 1 -mindepth 1 -type d -regextype posix-extended -regex "$SEASON_DIR_REGEX" -printf '%f\n' 2>/dev/null | sort
  )

  if [ "${#SEASONS[@]}" -eq 0 ]; then
    echo "No season folders found under data/ — falling back to current season only."
    run_for_season ""
  else
    echo "Found seasons: ${SEASONS[*]}"
    for s in "${SEASONS[@]}"; do
      run_for_season "${s%%-*}"
    done
  fi

else
  echo "No season given, scan disabled — running current season only."
  run_for_season ""
fi

echo "✅ tm_scraper run(s) complete."