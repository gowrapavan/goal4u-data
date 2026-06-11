"""
api/routers/teams.py

Endpoints:
  GET /api/v1/teams/{id}    ?include=squad
"""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from api.main import store
from api.utils import error_404

router = APIRouter(tags=["teams"])


@router.get("/teams/{team_id}")
def get_team(
    team_id: int,
    include: str | None = Query(default=None, description="Pass 'squad' to include full squad list"),
):
    """
    Return a team by its numeric ID.
    ?include=squad — adds the squad array to the response.
    Squad is omitted by default to keep the response lightweight.
    """
    team = store["teams"].get(str(team_id))
    if not team:
        return error_404(f"Team with id {team_id} not found.")

    if include and "squad" in include.lower():
        # Squad already present in the stored object — return as-is
        return team

    # Default: omit squad for a lean response
    lean = {k: v for k, v in team.items() if k != "squad"}
    return lean