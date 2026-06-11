"""
api/routers/matches.py

Endpoints:
  GET /api/v1/matches                ?date=YYYY-MM-DD
                                     ?status=LIVE|FINISHED|...
                                     ?dateFrom=...&dateTo=...
  GET /api/v1/matches/{id}           single match detail
"""

from datetime import date, datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from api.main import store
from api.utils import error_400, error_404, list_response

router = APIRouter(tags=["matches"])

VALID_STATUSES = {
    "SCHEDULED", "TIMED", "IN_PLAY", "PAUSED",
    "FINISHED", "SUSPENDED", "POSTPONED", "CANCELLED", "AWARDED", "LIVE",
}


def _match_date(match: dict) -> str | None:
    """Return the UTC date portion (YYYY-MM-DD) of a match's utcDate."""
    utc = match.get("utcDate") or ""
    return utc[:10] if len(utc) >= 10 else None


# ── GLOBAL MATCH LIST ─────────────────────────────────────────────────────────

@router.get("/matches")
def list_matches(
    date:     str | None = Query(default=None, description="YYYY-MM-DD — all matches on this date"),
    status:   str | None = Query(default=None, description="LIVE|FINISHED|IN_PLAY|PAUSED|..."),
    dateFrom: str | None = Query(default=None, description="YYYY-MM-DD inclusive start"),
    dateTo:   str | None = Query(default=None, description="YYYY-MM-DD exclusive end"),
):
    """
    Global match feed across all tracked competitions.

    Filter options (combinable):
    - ?date=YYYY-MM-DD — exact date
    - ?status=LIVE     — IN_PLAY + PAUSED combined; or any single status
    - ?dateFrom=...&dateTo=... — date range (dateTo exclusive)
    """

    # Validate status
    if status and status.upper() not in VALID_STATUSES:
        return error_400(
            f"Unknown status '{status}'. "
            f"Valid values: {', '.join(sorted(VALID_STATUSES))}"
        )

    # Validate date formats
    for label, val in [("date", date), ("dateFrom", dateFrom), ("dateTo", dateTo)]:
        if val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                return error_400(f"Invalid date format for '{label}': '{val}'. Expected YYYY-MM-DD.")

    # Conflict check
    if date and (dateFrom or dateTo):
        return error_400("Cannot combine 'date' with 'dateFrom'/'dateTo'. Use one or the other.")

    results = list(store["matches"])  # shallow copy so we don't mutate the store

    # Apply date filter
    if date:
        results = [m for m in results if _match_date(m) == date]

    # Apply date range filter
    if dateFrom:
        results = [m for m in results if (_match_date(m) or "") >= dateFrom]
    if dateTo:
        results = [m for m in results if (_match_date(m) or "") < dateTo]

    # Apply status filter
    status_upper = status.upper() if status else None
    if status_upper == "LIVE":
        results = [m for m in results if m.get("status") in ("IN_PLAY", "PAUSED")]
    elif status_upper:
        results = [m for m in results if m.get("status") == status_upper]

    # Build active filters dict (only include params that were provided)
    filters: dict = {}
    if date:
        filters["date"] = date
    if dateFrom:
        filters["dateFrom"] = dateFrom
    if dateTo:
        filters["dateTo"] = dateTo
    if status:
        filters["status"] = status_upper

    return list_response(results, filters=filters)


# ── SINGLE MATCH ──────────────────────────────────────────────────────────────

@router.get("/matches/{match_id}")
def get_match(match_id: int):
    """Return full detail for a single match by its numeric ID."""
    for match in store["matches"]:
        if match.get("id") == match_id:
            return match
    return error_404(f"Match with id {match_id} not found.")