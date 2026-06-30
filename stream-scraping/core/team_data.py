import json
import random
import sys
from pathlib import Path

# === 1. Dynamically locate your root API folder ===
# This file is at: D:\test\football\API\stream-scraping\core\team_data.py
# The API root is three levels up.
API_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(API_ROOT))

# === 2. Locate the data root ===
# NOTE: there is no single "teams_dir" -- workers/tournament_paths.py writes
# one teams.json PER COMPETITION, scattered across several shapes:
#   data/{season}/{CODE}/teams.json            (leagues, e.g. data/2025-2026/PL/teams.json)
#   data/world-cup/world-cup-{year}/teams.json
#   data/euros/euro-{year}/teams.json
# config.get_season_paths() has no "teams_dir" key (that was the bug --
# KeyError on every run), and even a correct single path would be wrong:
# each teams.json holds the WHOLE squad list as one array (written by
# fetch_teams_for_competition() via safe_write(paths["teams"], teams)),
# not one file per team. So instead of resolving one directory, we just
# glob for every teams.json under data/ and flatten them all together.
DATA_DIR = API_ROOT / "data"

# === Random logo placeholders ===
LOGOS = [
    "https://raw.githubusercontent.com/gowrapavan/Goal4u/main/public/assets/img/tv-logo/aves.png",
    "https://raw.githubusercontent.com/gowrapavan/Goal4u/main/public/assets/img/tv-logo/benfica.png",
    "https://raw.githubusercontent.com/gowrapavan/Goal4u/main/public/assets/img/tv-logo/braga.png",
    "https://raw.githubusercontent.com/gowrapavan/Goal4u/main/public/assets/img/tv-logo/fcboavista.png",
    "https://raw.githubusercontent.com/gowrapavan/Goal4u/main/public/assets/img/tv-logo/maritimo.png",
    "https://raw.githubusercontent.com/gowrapavan/Goal4u/main/public/assets/img/tv-logo/porto.png",
    "https://raw.githubusercontent.com/gowrapavan/Goal4u/main/public/assets/img/tv-logo/sporting.png",
    "https://raw.githubusercontent.com/gowrapavan/Goal4u/main/public/assets/img/tv-logo/valencia.png",
]

TEAM_DATA = []

def random_logo():
    return random.choice(LOGOS)

def load_team_data():
    """
    Reads every teams.json written by workers/fetch_teams.py /
    fetch_worldCup.py / fetch_Euro.py, wherever it lives under data/,
    and flattens all the squad arrays into one TEAM_DATA list.

    Each file looks like:
        {"_meta": {...}, "data": [ {id, name, shortName, crest, ...}, ... ]}
    -- one file per competition, holding the WHOLE squad as an array
    (not one file per team), so we just glob + unwrap + extend.
    """
    global TEAM_DATA
    if TEAM_DATA:
        return TEAM_DATA

    if not DATA_DIR.exists():
        print(f"⚠️ Local data directory not found at: {DATA_DIR}")
        return TEAM_DATA

    team_files = sorted(DATA_DIR.glob("**/teams.json"))

    if not team_files:
        print(f"⚠️ No teams.json files found under: {DATA_DIR}")
        return TEAM_DATA

    seen_ids: set = set()
    for file_path in team_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            # safe_write wraps the real list under "data"
            teams = payload.get("data") or []
            for team in teams:
                tid = team.get("id")
                if tid is not None:
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                TEAM_DATA.append(team)
        except Exception as e:
            print(f"⚠️ Failed to load local team file {file_path}: {e}")

    print(f"✅ Loaded {len(TEAM_DATA)} teams from {len(team_files)} teams.json files")
    return TEAM_DATA

def find_team_crest(team_name):
    """Find crest URL for given team name safely, protecting against nulls."""
    if not team_name:
        return random_logo()
        
    team_name_low = str(team_name).lower()
    
    # 1. First pass: Check for exact matches
    for team in TEAM_DATA:
        # Using .get('key') or "" ensures we don't crash if a field is explicitly null
        team_full = (team.get("name") or "").lower()
        team_short = (team.get("shortName") or "").lower()
        
        if team_name_low in team_full or team_name_low in team_short:
            return team.get("crest") or random_logo()
            
    # 2. Second pass: Check if the first word of the team matches
    parts = team_name_low.split()
    if parts:
        first_word = parts[0]
        for team in TEAM_DATA:
            team_full = (team.get("name") or "").lower()
            if first_word in team_full and first_word != "":
                return team.get("crest") or random_logo()
                
    # 3. Fallback
    return random_logo()