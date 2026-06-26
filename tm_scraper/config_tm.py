"""
tm_scraper/config_tm.py
──────────────────────────────────────────────────────────────────────────────
Central configuration for all TM scraper paths, league mappings, and aliases.
"""

from pathlib import Path

# ── Base directories ──────────────────────────────────────────────────────────
BASE_DATA_DIR     = Path("data")
ASSETS_DIR        = Path("public") / "assets"
PLAYER_IMAGES_DIR = ASSETS_DIR / "player_images"
TROPHY_IMAGES_DIR = ASSETS_DIR / "trophies"

# ── Output directories (flat layout) ─────────────────────────────────────────
TEAM_INFO_DIR    = BASE_DATA_DIR / "team_informations"
PLAYER_INFO_DIR  = BASE_DATA_DIR / "player_information"
LEAGUE_INFO_DIR  = BASE_DATA_DIR / "league_info"

# Auto-create all directories
for _d in (
    TEAM_INFO_DIR, PLAYER_INFO_DIR, LEAGUE_INFO_DIR,
    PLAYER_IMAGES_DIR, TROPHY_IMAGES_DIR,
):
    _d.mkdir(parents=True, exist_ok=True)

# ── Transfermarkt base URL ────────────────────────────────────────────────────
TM_BASE_URL = "https://www.transfermarkt.co.in"

# ── League mappings: local API code → TM parameters ──────────────────────────
LEAGUE_MAPPING = {
    "PD":  {"tm_name": "laliga",          "tm_id": "ES1"},
    "PL":  {"tm_name": "premier-league",  "tm_id": "GB1"},
    "SA":  {"tm_name": "serie-a",         "tm_id": "IT1"},
    "BL1": {"tm_name": "bundesliga",      "tm_id": "L1"},
    "FL1": {"tm_name": "ligue-1",         "tm_id": "FR1"},
}

# ── Team name aliases  ────────────────────────────────────────────────────────
# Maps local API shortName/name  →  Transfermarkt team name
# Extend this whenever the scraper logs "Unmatched team".
TEAM_ALIASES: dict[str, str] = {
    # ── LaLiga (PD) ──────────────────────────────────────────────────────────
    "Barça":            "FC Barcelona",
    "Atleti":           "Atlético de Madrid",
    "Espanyol":         "RCD Espanyol",
    "Alavés":           "Deportivo Alavés",
    "Real Betis":       "Real Betis Balompié",
    "Celta":            "Celta de Vigo",
    "Sevilla FC":       "Sevilla FC",
    "Leganés":          "CD Leganés",
    "Real Valladolid":  "Real Valladolid",
    "Getafe":           "Getafe CF",
    "Osasuna":          "CA Osasuna",
    "Mallorca":         "RCD Mallorca",

    # ── Bundesliga (BL1) ─────────────────────────────────────────────────────
    "Mainz":            "1. FSV Mainz 05",
    "M'gladbach":       "Borussia M'gladbach",
    "Mönchengladbach":  "Borussia M'gladbach",
    "Bremen":           "Werder Bremen",
    "Köln":             "1. FC Köln",
    "Koln":             "1. FC Köln",

    # ── Ligue 1 (FL1) ────────────────────────────────────────────────────────
    "PSG":              "Paris Saint-Germain",
    "Paris SG":         "Paris Saint-Germain",
    "Brest":            "Stade Brestois 29",
    "St. Etienne":      "Saint-Étienne",
    "Saint-Etienne":    "Saint-Étienne",
    "Rennes":           "Stade Rennais FC",
    "Lens":             "RC Lens",
    "Reims":            "Stade de Reims",
    "Le Havre AC":      "Le Havre AC",

    # ── Premier League (PL) ──────────────────────────────────────────────────
    "Brighton":         "Brighton Hove Albion",
    "Brighton & Hove Albion": "Brighton Hove Albion",
    "Nottm Forest":     "Nottingham Forest",
    "Nott'm Forest":    "Nottingham Forest",
    "Nottingham":       "Nottingham Forest",
    "Spurs":            "Tottenham Hotspur",
    "Wolves":           "Wolverhampton Wanderers",
    "Wolverhampton":    "Wolverhampton Wanderers",
    "Man City":         "Manchester City",
    "Man United":       "Manchester United",
    "Man Utd":          "Manchester United",
    "West Ham":         "West Ham United",
    "Newcastle":        "Newcastle United",
    "Sheffield Utd":    "Sheffield United",

    # ── Serie A (SA) ─────────────────────────────────────────────────────────
    "Inter":            "Inter Milan",
    "Internazionale":   "Inter Milan",
    "Verona":           "Hellas Verona",
    "Hellas":           "Hellas Verona",
    "Spezia":           "Spezia Calcio",
}

# ── TM URL builders ───────────────────────────────────────────────────────────
def league_url(tm_name: str, tm_id: str, season_year: str) -> str:
    return f"{TM_BASE_URL}/{tm_name}/startseite/wettbewerb/{tm_id}/saison_id/{season_year}"

def league_metadata_url(tm_name: str, tm_id: str) -> str:
    """Overview page — contains squad stats, market value totals, etc."""
    return f"{TM_BASE_URL}/{tm_name}/startseite/wettbewerb/{tm_id}"

def league_top_scorers_url(tm_name: str, tm_id: str) -> str:
    """Golden boot winners — shows every season's top scorer in one table; no season filter."""
    return f"{TM_BASE_URL}/{tm_name}/torschuetzenkoenige/wettbewerb/{tm_id}"

def league_successful_players_url(tm_name: str, tm_id: str) -> str:
    return f"{TM_BASE_URL}/{tm_name}/erfolgreichstespieler/wettbewerb/{tm_id}"

def league_all_champions_url(tm_name: str, tm_id: str) -> str:
    return f"{TM_BASE_URL}/{tm_name}/alle-meister/wettbewerb/{tm_id}"

def league_championship_managers_url(tm_name: str, tm_id: str) -> str:
    return f"{TM_BASE_URL}/{tm_name}/erfolgreichstetrainer/wettbewerb/{tm_id}"

def league_market_values_url(tm_name: str, tm_id: str) -> str:
    """Market values page — TM always shows current squad values; no season filter needed."""
    return f"{TM_BASE_URL}/{tm_name}/marktwerte/wettbewerb/{tm_id}"

def league_players_of_year_url(tm_name: str, tm_id: str) -> str:
    return f"{TM_BASE_URL}/{tm_name}/spieler-des-jahres/wettbewerb/{tm_id}"

def league_table_url(tm_name: str, tm_id: str, season_year: str) -> str:
    return f"{TM_BASE_URL}/{tm_name}/tabelle/wettbewerb/{tm_id}/saison_id/{season_year}"

def league_transfers_url(tm_name: str, tm_id: str, season_year: str) -> str:
    return f"{TM_BASE_URL}/{tm_name}/transfers/wettbewerb/{tm_id}/saison_id/{season_year}"