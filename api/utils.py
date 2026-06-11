"""
api/utils.py — shared response helpers for all routers.

All error and list shapes are defined here so every router
produces consistent JSON (FR-3.2, FR-3.3).
"""

from fastapi.responses import JSONResponse

from api.store import store


# ── STANDARD ERROR RESPONSES ─────────────────────────────────────────────────

def error_404(message: str) -> JSONResponse:
    """{"error": "...", "status": 404}"""
    return JSONResponse(status_code=404, content={"error": message, "status": 404})


def error_400(message: str) -> JSONResponse:
    """{"error": "...", "status": 400}"""
    return JSONResponse(status_code=400, content={"error": message, "status": 400})


# ── STANDARD LIST RESPONSE ────────────────────────────────────────────────────

def list_response(results: list, filters: dict | None = None) -> dict:
    """
    Wrap a list in the standard envelope (FR-3.2):
      {"count": N, "filters": {...}, "results": [...]}
    """
    return {
        "count":   len(results),
        "filters": filters or {},
        "results": results,
    }


# ── COMPETITION LOOKUP ────────────────────────────────────────────────────────

def get_competition_or_404(code: str):
    """
    Look up a competition by code (case-insensitive).
    Returns the competition dict on success, or a 404 JSONResponse.
    """
    code_upper = code.upper()
    for comp in store["competitions"]:
        if comp.get("code") == code_upper:
            return comp
    return error_404(
        f"Competition with code '{code_upper}' not found. "
        "Use GET /api/v1/competitions to see all available codes."
    )