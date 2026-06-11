# config.py — single source of truth for the entire project
# ─────────────────────────────────────────────────────────────────────────────
# Season resolution rule (get_current_season_string):
#   • Month >= July  →  "{year}-{year+1}"   e.g. 2025-07 → "2025-2026"
#   • Month <  July  →  "{year-1}-{year}"   e.g. 2026-06 → "2025-2026"
#
# Full data directory layout per season:
#   data/{season}/
#     competitions.json              ← all 8 competition metadata objects
#     standings/{CODE}.json          ← league table (TOTAL + HOME + AWAY)
#     matches/{CODE}.json            ← all matches for the competition
#     scorers/{CODE}.json            ← top scorers per competition
#     teams/{CODE}/{team_id}.json    ← full team profile + squad
#
# API tier note (football-data.org free / TIER_ONE):
#   GET /competitions/{code}/teams  → returns full team list WITH squad on
#   all tiers for the tracked competitions below. Use this instead of
#   individual GET /teams/{id} calls which return 403 for most teams on
#   the free tier.

from datetime import datetime, timezone


# ── TRACKED COMPETITIONS ──────────────────────────────────────────────────────

TRACKED_COMPETITIONS: list[str] = [
    "PL",   # Premier League           (England)
    "PD",   # La Liga                  (Spain)
    "BL1",  # Bundesliga               (Germany)
    "SA",   # Serie A                  (Italy)
    "FL1",  # Ligue 1                  (France)
    "CL",   # UEFA Champions League
    "EC",   # UEFA European Championship
    "WC",   # FIFA World Cup
]

# How many top scorers to fetch per competition (API default is 10, max varies)
SCORERS_LIMIT = 20


# ── CLEVER SEASON RESOLVER ────────────────────────────────────────────────────

def get_current_season_string() -> str:
    """
    Return the active football season as a "YYYY-YYYY" folder name.

    Rule:
      - Month >= July  →  new season has started  →  "{year}-{year+1}"
      - Month < July   →  still in the season that started last year
                          →  "{year-1}-{year}"

    e.g.  run on 2026-06-10  →  "2025-2026"
          run on 2026-08-01  →  "2026-2027"
    """
    now  = datetime.now(timezone.utc)
    year = now.year
    return f"{year}-{year + 1}" if now.month >= 7 else f"{year - 1}-{year}"


# ── SEASON-AWARE PATH FACTORY ─────────────────────────────────────────────────

def get_season_paths(season: str | None = None) -> dict[str, str]:
    """
    Return every base path used by the worker scripts for the given season.
    Pass season=None (default) to auto-resolve from the current date.

    Keys returned:
        season          – "2025-2026"
        root            – "data/2025-2026"
        competitions    – "data/2025-2026/competitions.json"
        standings_dir   – "data/2025-2026/standings"
        matches_dir     – "data/2025-2026/matches"
        scorers_dir     – "data/2025-2026/scorers"
        teams_dir       – "data/2025-2026/teams"

    Directory creation is handled automatically by safe_write() which calls
    Path(path).parent.mkdir(parents=True, exist_ok=True) before every write.
    """
    if season is None:
        season = get_current_season_string()

    root = f"data/{season}"
    return {
        "season":        season,
        "root":          root,
        "competitions":  f"{root}/competitions.json",
        "standings_dir": f"{root}/standings",
        "matches_dir":   f"{root}/matches",
        "scorers_dir":   f"{root}/scorers",
        "teams_dir":     f"{root}/teams",
    }


# ── FIELD STRIP LISTS ─────────────────────────────────────────────────────────
# Only strip fields that are genuinely useless for any consumer of the data.
# When in doubt, KEEP the field — disk is cheap, missing data is not fixable.

# Competition-level fields to drop
COMPETITION_STRIP_FIELDS: list[str] = [
    "lastUpdated",
    "_links",
    "seasons",    # long historical array — bloats competitions.json, not needed
    # NOTE: "emblem" is KEPT — useful for UI competition logos
]

# Match-level top-level fields to drop
MATCH_STRIP_FIELDS: list[str] = [
    "lastUpdated",
    "_links",
    # NOTE: "referees" is KEPT — useful for display and analysis
    # NOTE: "odds"     is KEPT — useful for context (available on some tiers)
]

# Team-level fields to drop (only genuinely useless contact/meta fields)
TEAM_STRIP_FIELDS: list[str] = [
    "lastUpdated",
    "_links",
    "address",    # physical postal address — no UI value
    "phone",
    "email",
    # KEPT (compared to the previous over-aggressive strip list):
    # "crest"               → retained — logo URL used everywhere
    # "website"             → retained — external link
    # "founded"             → retained — club history display
    # "clubColors"          → retained — useful for theming
    # "activeCompetitions"  → retained — shows which comps team is in
    # "runningCompetitions" → retained — same
    # "marketValue"         → retained — squad value display
    # "staff"               → retained — non-playing staff
    # "dateOfBirth"         → retained on players
    # "contract"            → retained on players
]

# Fields to strip from in-match person references (scorer, assister, booked)
# These are minimal refs — the full player profile is in teams/{CODE}/{id}.json
PERSON_STRIP_FIELDS: list[str] = [
    "lastUpdated",
    "_links",
    # KEPT: id, name, position, shirtNumber, nationality — all useful in match events
    # KEPT: dateOfBirth — useful for age display in match events
]