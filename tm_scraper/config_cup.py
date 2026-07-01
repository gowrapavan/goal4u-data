"""
tm_scraper/config_cup.py
──────────────────────────────────────────────────────────────────────────────
Config for CUP competitions (Champions League, Euro, World Cup) — as opposed
to the LEAGUE competitions handled by config_tm.py.

STORAGE POLICY — read this before touching anything below
─────────────────────────────────────────────────────────
This module creates ZERO new top-level data folders. Every cup/national-team
record lands inside the exact same three trees the league scraper already
uses:

    data/league_info/{CODE}/...          (CODE = "CL" | "EURO" | "WC", same
                                           shape as "GB1" | "ES1" | ... )
    data/team_informations/{team_id}/... (team_id reused from football-data
                                           when the entity already exists —
                                           e.g. a club also playing in CL —
                                           otherwise a stable synthetic id
                                           from id_registry.py)
    data/player_information/{player_id}.json  (same reuse-or-synthesize rule)
    public/assets/player_images/{player_id}.jpg
    public/assets/trophies/{safe_name}.jpg

Because the SAME team can appear in multiple competitions (a club in CL +
its domestic league, or a national side in both the World Cup and the Euro),
team_informations/{team_id}/ can hold several squad files side by side —
one per competition+season, distinguished only by filename:

    2024-2025-squad.json      ← league squad (existing convention, untouched)
    cl-2025-2026.json         ← Champions League 25/26 squad
    world-cup-2026.json       ← World Cup 2026 squad
    euro-2024.json            ← Euro 2024 squad

team_metadata.json is shared/overwritten-in-place exactly like the league
scraper does — if a club already has one from the league run, the cup
runner reuses it as-is instead of re-fetching.

See id_registry.py for how team_id / player_id get assigned to entities
that have no football-data.org ID (national teams, non-tracked clubs).
"""

from __future__ import annotations

from tm_scraper.config_tm import (
    BASE_DATA_DIR, TM_BASE_URL,
    LEAGUE_INFO_DIR, TEAM_INFO_DIR, PLAYER_INFO_DIR,
    PLAYER_IMAGES_DIR, TROPHY_IMAGES_DIR,
)

# ── Cup competition mapping: local code → TM parameters ──────────────────────
CUP_MAPPING = {
    "CL":   {"tm_name": "uefa-champions-league", "tm_id": "CL"},
    "EURO": {"tm_name": "uefa-euro",              "tm_id": "EURO"},
    "WC":   {"tm_name": "world-cup",              "tm_id": "FIWC"},
}

# Short slug used in squad filenames (deliberately NOT the same as tm_name,
# per your requested filenames: "cl-2025-2026.json", "world-cup-2026.json",
# "euro-2024.json"). "NT" is used only by the standalone weltrangliste-based
# national-team scrape (run_national_teams), which isn't tied to one specific
# tournament edition.
CUP_SLUG = {
    "CL":   "cl",
    "EURO": "euro",
    "WC":   "world-cup",
    "NT":   "national-team",
}

# ── ID registry (single file, NOT a new folder) ───────────────────────────────
# Holds synthetic IDs for teams/players with no football-data.org ID.
ID_REGISTRY_PATH = BASE_DATA_DIR / "id_registry.json"


# ── Season-label resolution ───────────────────────────────────────────────────
# TM's saison_id → the human tournament year is one year off for WC/EURO
# (confirmed from your examples: WC saison_id=2021 → 2022 World Cup,
# EURO saison_id=2023 → Euro 2024), and CL labels as the usual "YYYY-YYYY+1"
# season span. season_id=None means "current/upcoming edition, no saison_id
# published on TM yet" — used only for filenames when you haven't confirmed
# the real saison_id for a not-yet-started tournament.
def cup_season_label(code: str, season_id: str | None) -> str:
    """Return the filename-safe season label for a competition edition."""
    if season_id is None:
        return "current"
    if code == "CL":
        y = int(season_id)
        return f"{y}-{y + 1}"
    if code in ("WC", "EURO"):
        return str(int(season_id) + 1)
    return str(season_id)


# ── URL builders ───────────────────────────────────────────────────────────────
def cup_homepage_url(tm_name: str, tm_id: str, season_id: str | None = None) -> str:
    base = f"{TM_BASE_URL}/{tm_name}/startseite/pokalwettbewerb/{tm_id}"
    return f"{base}?saison_id={season_id}" if season_id else base


def cup_participants_url(tm_name: str, tm_id: str, season_id: str) -> str:
    """Participating-teams page (still-in + eliminated tables). saison_id required."""
    return f"{TM_BASE_URL}/{tm_name}/teilnehmer/pokalwettbewerb/{tm_id}/saison_id/{season_id}"


def cup_market_values_url(tm_name: str, tm_id: str, season_id: str) -> str:
    """Most valuable players in THIS edition of the tournament."""
    return f"{TM_BASE_URL}/{tm_name}/marktwerte/pokalwettbewerb/{tm_id}/saison_id/{season_id}"


def cup_top_scorers_url(tm_name: str, tm_id: str, season_id: str) -> str:
    """Top scorers for THIS edition (torschuetzenliste)."""
    return f"{TM_BASE_URL}/{tm_name}/torschuetzenliste/pokalwettbewerb/{tm_id}/saison_id/{season_id}"


def cup_top_scorers_alltime_url(tm_name: str, tm_id: str) -> str:
    """All-time top scorers across every edition of this cup. No season slug."""
    return f"{TM_BASE_URL}/{tm_name}/ewigetorschuetzenliste/pokalwettbewerb/{tm_id}"


# ── National-team ranking (FIFA weltrangliste — source of national rosters) ──
def national_ranking_url(page: int = 1) -> str:
    """
    FIFA world-ranking list — every national team TM knows, each linking to
    its own team ('verein') page. TM treats a national team exactly like a
    club for team_info/squad-list purposes, so parse_team_info /
    parse_squad_links from tm_parsers.py work unchanged on these pages.
    """
    base = f"{TM_BASE_URL}/statistik/weltrangliste"
    return base if page <= 1 else f"{base}?page={page}"