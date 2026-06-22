# config.py — single source of truth for the entire project
# ─────────────────────────────────────────────────────────────────────────────
# Season resolution rule (get_current_season_string):
#   • Month >= June  →  "{year}-{year+1}"   e.g. 2026-06 → "2026-2027"
#   • Month <  June  →  "{year-1}-{year}"   e.g. 2026-05 → "2025-2026"
#
# Rationale: European leagues (PL, La Liga, Bundesliga, Serie A, Ligue 1,
# Champions League) all finish by late May. By June the current season is
# over and football-data.org begins returning the NEXT season's scheduled
# fixtures for those competitions — so June must map to the new season folder.
# The old cutoff of July was one month too late and caused June fetches to
# write 2026-2027 API data into the 2025-2026 folder.
#
# Full data directory layout per season:
#   data/{season}/
#     competitions.json              ← all 8 competition metadata objects
#     standings/{CODE}.json          ← league table (TOTAL + HOME + AWAY)
#     matches/{CODE}.json            ← all matches for the competition
#     scorers/{CODE}.json            ← top scorers per competition
#     teams/{CODE}/{team_id}.json    ← full team profile + squad

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
      - Month >= June  →  season is over / next season scheduled
                          →  "{year}-{year+1}"
      - Month <  June  →  still in the season that started last year
                          →  "{year-1}-{year}"

    e.g.  run on 2026-05-31  →  "2025-2026"   (season still in progress)
          run on 2026-06-01  →  "2026-2027"   (season over, API serves next)
          run on 2026-08-01  →  "2026-2027"   (new season underway)

    Why June and not July?
    European leagues finish by late May. football-data.org starts returning
    the next season's scheduled fixtures in June — so writing June data to
    the old season folder would mix 2026-2027 matches into 2025-2026 files.
    """
    now  = datetime.now(timezone.utc)
    year = now.year
    return f"{year}-{year + 1}" if now.month >= 6 else f"{year - 1}-{year}"


def get_current_season_start_year() -> int:
    """
    Return the start-year integer for the season get_current_season_string()
    resolves to, e.g. "2026-2027" -> 2026.

    WHY THIS EXISTS:
    football-data.org maintains its OWN internal "current season" pointer
    per competition, and that pointer does not roll over to the new season
    at the same moment for every competition. In June 2026, PL and FL1 had
    already rolled to 2026-2027 (returning empty/scheduled fixtures), while
    PD and CL had not yet rolled over (still returning finished 2025-2026
    matches) — even though all requests were made on the same day with no
    season param at all.

    If a worker calls the API without an explicit ?season=YYYY, it gets
    whatever season the API *thinks* is current for that specific
    competition — which may not match the season folder we're about to
    write into. The fix is to NEVER omit the season param: always pass
    this value explicitly, even when fetching the "current" season, so
    every competition is forced to return the same season we're saving to.
    """
    return int(get_current_season_string().split("-")[0])


# ── SEASON-AWARE PATH FACTORY ─────────────────────────────────────────────────

def get_season_paths(season: str | None = None) -> dict[str, str]:
    """
    Return every base path used by the worker scripts for the given season.
    Pass season=None (default) to auto-resolve from the current date.

    Keys returned:
        season          – "2026-2027"
        root            – "data/2026-2027"
        competitions    – "data/2026-2027/competitions.json"
        standings_dir   – "data/2026-2027/standings"
        matches_dir     – "data/2026-2027/matches"
        scorers_dir     – "data/2026-2027/scorers"
        teams_dir       – "data/2026-2027/teams"

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