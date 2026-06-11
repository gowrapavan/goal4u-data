"""
api/main.py — FastAPI entry point

Loads all JSON data files into memory at startup.
No disk reads occur per request (FR-2.1, NFR-1.1).
"""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.store import store  # isolated store — no circular import

logger = logging.getLogger(__name__)


# ── DATA LOADER ───────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        logger.warning("Data file not found: %s — endpoint will return empty results", path)
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load %s: %s", path, exc)
        return None


def load_all_data() -> None:
    """Called once at startup — populates the in-memory store (FR-2.1)."""

    raw = _load_json("data/competitions.json")
    if raw:
        store["competitions"] = raw.get("data", [])
        store["_meta"]["competitions"] = raw.get("_meta", {}).get("last_synced")
        logger.info("Loaded %d competitions", len(store["competitions"]))

    # matches — one file per competition: data/matches/{CODE}.json
    matches_dir = Path("data/matches")
    loaded_match_files = 0
    if matches_dir.exists():
        for mf in matches_dir.glob("*.json"):
            raw = _load_json(str(mf))
            if raw:
                comp_matches = raw.get("data", [])
                store["matches"].extend(comp_matches)
                store["_meta"].setdefault("matches", {})[mf.stem] = raw.get("_meta", {}).get("last_synced")
                loaded_match_files += 1
    logger.info("Loaded %d matches from %d competition files", len(store["matches"]), loaded_match_files)

    # teams — one file per competition: data/teams/{CODE}.json
    # Each file contains an array of team objects; index them by team id for O(1) lookup.
    teams_dir = Path("data/teams")
    loaded_teams = 0
    if teams_dir.exists():
        for tf in teams_dir.glob("*.json"):
            raw = _load_json(str(tf))
            if raw:
                for team in raw.get("data", []):
                    tid = team.get("id")
                    if tid:
                        store["teams"][str(tid)] = team
                        loaded_teams += 1
    logger.info("Loaded %d teams", loaded_teams)

    standings_dir = Path("data/standings")
    loaded_standings = 0
    if standings_dir.exists():
        for sf in standings_dir.glob("*.json"):
            raw = _load_json(str(sf))
            if raw:
                standings = raw.get("data", {})
                code = standings.get("competition_code") or sf.stem
                store["standings"][code] = standings
                loaded_standings += 1
    logger.info("Loaded standings for %d competitions", loaded_standings)


# ── LIFESPAN ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_all_data()
    yield


# ── APP ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="GOAL4U Sports API",
    version="1.0.0",
    description="Zero-rate-limit caching proxy for football data.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Routers imported AFTER app creation to avoid circular issues
from api.routers import competitions, matches, teams  # noqa: E402

app.include_router(competitions.router, prefix="/api/v1")
app.include_router(matches.router,      prefix="/api/v1")
app.include_router(teams.router,        prefix="/api/v1")


# ── HEALTH ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    """Returns 200 + last-sync timestamps for all data files. (FR-2.2)"""
    return {
        "status": "ok",
        "counts": {
            "competitions": len(store["competitions"]),
            "matches":      len(store["matches"]),
            "teams":        len(store["teams"]),
            "standings":    len(store["standings"]),
        },
        "last_synced": store["_meta"],
    }


@app.get("/", tags=["meta"])
def root():
    return {"api": "GOAL4U Sports API", "version": "v1", "docs": "/docs"}