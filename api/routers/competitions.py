"""
api/routers/competitions.py

Endpoints:
  GET /api/v1/competitions
  GET /api/v1/competitions/{code}
  GET /api/v1/competitions/{code}/standings   ?type=TOTAL|HOME|AWAY
  GET /api/v1/competitions/{code}/scorers     ?limit=10
  GET /api/v1/competitions/{code}/matches     ?matchday=X  ?status=...
  GET /api/v1/competitions/{code}/teams
"""

from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from api.store import store
from api.utils import error_404, error_400, list_response, get_competition_or_404

router = APIRouter(tags=["competitions"])


# ── LIST ALL COMPETITIONS ─────────────────────────────────────────────────────

@router.get("/competitions")
def list_competitions():
    """Return all tracked competitions."""
    return list_response(store["competitions"])


# ── SINGLE COMPETITION ────────────────────────────────────────────────────────

@router.get("/competitions/{code}")
def get_competition(code: str):
    """Return a single competition by its code (e.g. PL, BL1)."""
    comp = get_competition_or_404(code)
    if isinstance(comp, JSONResponse):
        return comp
    return comp


# ── STANDINGS ─────────────────────────────────────────────────────────────────

@router.get("/competitions/{code}/standings")
def get_standings(
    code: str,
    type: Literal["TOTAL", "HOME", "AWAY"] | None = Query(default=None),
):
    """
    League table for a competition.
    ?type=TOTAL|HOME|AWAY  — filter the standings type (default: return all three).
    Returns 404 for CUP-type competitions which have no standings table.
    """
    comp = get_competition_or_404(code)
    if isinstance(comp, JSONResponse):
        return comp

    standings_data = store["standings"].get(code.upper())
    if not standings_data:
        return error_404(f"No standings available for competition '{code}'. "
                         "This may be a CUP competition or standings have not been synced yet.")

    standings_list = standings_data.get("standings", [])

    if type:
        standings_list = [s for s in standings_list if s.get("type") == type.upper()]
        if not standings_list:
            return error_400(f"No standings of type '{type}' found for '{code}'.")

    return {
        "competition_code": code.upper(),
        "season":           standings_data.get("season"),
        "filters":          {"type": type},
        "standings":        standings_list,
    }


# ── TOP SCORERS ───────────────────────────────────────────────────────────────

@router.get("/competitions/{code}/scorers")
def get_scorers(
    code: str,
    limit: int = Query(default=10, ge=1, le=50),
):
    """
    Top scorers derived from goal events in the matches data.
    Counts goals per player across FINISHED matches in this competition.
    ?limit=N — top N scorers (default 10, max 50).
    """
    comp = get_competition_or_404(code)
    if isinstance(comp, JSONResponse):
        return comp

    code_upper = code.upper()

    # Tally goals from flattened match goal events
    scorer_map: dict[int, dict] = {}   # player_id → scorer entry

    for match in store["matches"]:
        if match.get("competition_code") != code_upper:
            continue
        if match.get("status") != "FINISHED":
            continue

        for goal in match.get("goals") or []:
            if goal.get("type") == "OWN_GOAL":
                continue  # own goals don't count for the scorer

            scorer = goal.get("scorer") or {}
            scorer_id = scorer.get("id")
            if not scorer_id:
                continue

            team = goal.get("team") or {}
            assist = goal.get("assist")

            if scorer_id not in scorer_map:
                scorer_map[scorer_id] = {
                    "player":  {"id": scorer_id, "name": scorer.get("name")},
                    "team":    {"id": team.get("id"), "name": team.get("name"), "tla": team.get("tla")},
                    "goals":   0,
                    "assists": 0,
                    "penalties": 0,
                }

            scorer_map[scorer_id]["goals"] += 1
            if goal.get("type") == "PENALTY":
                scorer_map[scorer_id]["penalties"] += 1

            # Credit assist
            if assist:
                assist_id = assist.get("id")
                if assist_id and assist_id in scorer_map:
                    scorer_map[assist_id]["assists"] += 1
                elif assist_id:
                    scorer_map[assist_id] = {
                        "player":  {"id": assist_id, "name": assist.get("name")},
                        "team":    scorer_map[scorer_id]["team"],
                        "goals":   0,
                        "assists": 1,
                        "penalties": 0,
                    }

    scorers = sorted(scorer_map.values(), key=lambda x: (-x["goals"], -x["assists"]))[:limit]

    return {
        "competition_code": code_upper,
        "filters": {"limit": limit},
        "count":   len(scorers),
        "scorers": scorers,
    }


# ── COMPETITION MATCHES ───────────────────────────────────────────────────────

VALID_STATUSES = {
    "SCHEDULED", "TIMED", "IN_PLAY", "PAUSED",
    "FINISHED", "SUSPENDED", "POSTPONED", "CANCELLED", "AWARDED", "LIVE",
}


@router.get("/competitions/{code}/matches")
def get_competition_matches(
    code: str,
    matchday: int | None = Query(default=None, ge=1),
    status: str | None   = Query(default=None),
):
    """
    All matches for a competition.
    ?matchday=N — filter to a specific matchday.
    ?status=FINISHED|IN_PLAY|LIVE|... — filter by status.
    LIVE is a pseudo-status that returns IN_PLAY + PAUSED combined.
    """
    comp = get_competition_or_404(code)
    if isinstance(comp, JSONResponse):
        return comp

    if status and status.upper() not in VALID_STATUSES:
        return error_400(f"Unknown status '{status}'. "
                         f"Valid values: {', '.join(sorted(VALID_STATUSES))}")

    code_upper    = code.upper()
    status_upper  = status.upper() if status else None

    results = [m for m in store["matches"] if m.get("competition_code") == code_upper]

    if matchday is not None:
        results = [m for m in results if m.get("matchday") == matchday]

    if status_upper == "LIVE":
        results = [m for m in results if m.get("status") in ("IN_PLAY", "PAUSED")]
    elif status_upper:
        results = [m for m in results if m.get("status") == status_upper]

    filters = {}
    if matchday is not None:
        filters["matchday"] = matchday
    if status:
        filters["status"] = status_upper

    return list_response(results, filters=filters)


# ── COMPETITION TEAMS ─────────────────────────────────────────────────────────

@router.get("/competitions/{code}/teams")
def get_competition_teams(code: str):
    """
    All teams participating in a competition.
    Derived from the matches data — teams that appear in any match
    for this competition.
    """
    comp = get_competition_or_404(code)
    if isinstance(comp, JSONResponse):
        return comp

    code_upper = code.upper()
    seen_ids: set[int] = set()

    for match in store["matches"]:
        if match.get("competition_code") != code_upper:
            continue
        for side in ("homeTeam", "awayTeam"):
            team = match.get(side) or {}
            tid  = team.get("id")
            if tid:
                seen_ids.add(tid)

    # Return full team objects when available, otherwise fall back to the
    # minimal team ref extracted from matches
    teams = []
    for tid in sorted(seen_ids):
        full = store["teams"].get(str(tid))
        if full:
            teams.append(full)
        else:
            # Fallback: extract from matches data
            for match in store["matches"]:
                if match.get("competition_code") != code_upper:
                    continue
                for side in ("homeTeam", "awayTeam"):
                    t = match.get(side) or {}
                    if t.get("id") == tid:
                        teams.append(t)
                        break
                else:
                    continue
                break

    return list_response(teams)