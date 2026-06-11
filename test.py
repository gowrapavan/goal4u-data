from pathlib import Path

# Folders
folders = [
    "data",
    "data/standings",
    "workers",
    ".github/workflows",
    "api",
    "api/routers",
]

# Files
files = [
    "data/competitions.json",
    "data/matches.json",
    "data/teams.json",
    "data/standings/PL.json",
    "data/standings/BL1.json",
    "workers/fetch_competitions.py",
    "workers/fetch_matches.py",
    "workers/fetch_teams.py",
    ".github/workflows/sync_matches.yml",
    ".github/workflows/sync_standings.yml",
    "api/main.py",
    "api/routers/competitions.py",
    "api/routers/matches.py",
    "api/routers/teams.py",
]

# Create folders
for folder in folders:
    Path(folder).mkdir(parents=True, exist_ok=True)

# Create files
for file in files:
    Path(file).touch(exist_ok=True)

print("Project structure created successfully.")