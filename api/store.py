"""
api/store.py — the single in-memory data store.

Imported by both api/main.py (to populate at startup)
and by all routers (to read from per request).
Keeping this in its own module breaks the circular import
that would otherwise occur between main.py and the routers.
"""

from typing import Any

store: dict[str, Any] = {
    "competitions": [],   # list of competition objects
    "matches":      [],   # list of all match objects across all competitions
    "teams":        {},   # {str(team_id): team_object}
    "standings":    {},   # {competition_code: standings_object}
    "_meta":        {},   # last_synced timestamps keyed by resource name
}