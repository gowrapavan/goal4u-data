# workers/utils.py — shared utilities for all fetch workers
#
# Path-agnostic: nothing here depends on seasons or directories.
# Season-aware paths are resolved in config.get_season_paths().

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Minimum gap between consecutive API requests on the free tier.
# The free tier allows 10 requests/minute. We space them at least 6s apart
# to stay safely under the limit and avoid 429s during bulk fetches.
_MIN_REQUEST_INTERVAL = 6.0   # seconds
_last_request_time: float = 0.0


# ── HTTP ──────────────────────────────────────────────────────────────────────

def get_api_token() -> str:
    """Read the API key from the environment. Exit immediately if absent."""
    token = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    if not token:
        logger.error("FOOTBALL_DATA_API_KEY env var is not set")
        sys.exit(1)
    return token


def _throttle() -> None:
    """
    Enforce a minimum gap of _MIN_REQUEST_INTERVAL seconds between API calls.
    This prevents 429 rate-limit errors on the free tier (10 req/min).
    Called automatically by fetch() before every request.
    """
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def fetch(
    endpoint: str,
    params: dict | None = None,
    retries: int = 3,
    throttle: bool = True,
) -> dict | None:
    """
    Fetch one endpoint from the football-data.org v4 API.

    Returns the parsed JSON dict on success (HTTP 200).
    Returns None on any failure — callers treat None as a Conditional Fallback
    and leave existing files on disk untouched.

    Rate-limit / back-off behaviour:
      • Built-in throttling (_throttle()) keeps requests ≥ 6s apart.
      • 429  →  read X-RequestCounter-Reset header, sleep, retry (up to retries).
      • 5xx  →  exponential back-off (2s, 4s, 8s), retry (up to retries).
      • 403  →  API tier restriction; log clearly and return None immediately.
      • 4xx (other) →  log and return None immediately (retrying won't help).
      • Network error →  exponential back-off, retry (up to retries).

    Pass throttle=False to skip the inter-request delay (e.g. in unit tests).
    """
    if throttle:
        _throttle()

    token   = get_api_token()
    url     = f"https://api.football-data.org/v4{endpoint}"
    headers = {"X-Auth-Token": token}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                # X-RequestCounter-Reset tells us exactly how many seconds to wait
                wait = int(resp.headers.get("X-RequestCounter-Reset", 60))
                logger.warning(
                    "Rate limited on %s — waiting %ds (attempt %d/%d)",
                    endpoint, wait, attempt, retries,
                )
                time.sleep(wait)
                # Reset the throttle timer so the next request after sleep is immediate
                global _last_request_time
                _last_request_time = time.monotonic() - _MIN_REQUEST_INTERVAL
                continue

            if resp.status_code == 403:
                # Tier restriction — this team/endpoint is not accessible on the
                # current plan. Retrying won't help.
                logger.warning(
                    "403 Forbidden on %s — endpoint not available on current API tier. "
                    "Skipping (Conditional Fallback).",
                    endpoint,
                )
                return None

            if resp.status_code >= 500:
                wait = 2 ** attempt
                logger.warning(
                    "Server error %d on %s — retrying in %ds (attempt %d/%d)",
                    resp.status_code, endpoint, wait, attempt, retries,
                )
                time.sleep(wait)
                continue

            # Other 4xx — retrying won't help
            logger.error(
                "API returned %d for %s — aborting (Conditional Fallback).",
                resp.status_code, url,
            )
            return None

        except requests.RequestException as exc:
            wait = 2 ** attempt
            logger.warning(
                "Network error on attempt %d/%d for %s: %s — retrying in %ds",
                attempt, retries, endpoint, exc, wait,
            )
            if attempt == retries:
                logger.error(
                    "All %d retries exhausted for %s — Conditional Fallback.",
                    retries, url,
                )
                return None
            time.sleep(wait)

    return None


# ── SAFE FILE WRITER ──────────────────────────────────────────────────────────

def safe_write(path: str, data: Any) -> bool:
    """
    Atomically write `data` to `path` as pretty-printed JSON.

    Envelope format:
        {
          "_meta": { "last_synced": "<ISO-8601>", "source": "football-data.org v4" },
          "data":  <data>
        }

    Guarantees:
      • data=None  →  log error, do NOT touch the existing file, return False.
      • Writes to a .tmp sibling first, then os.replace() for atomic rename.
      • Parent directories created automatically.

    Returns True on success, False on any failure.
    """
    if data is None:
        logger.error("safe_write: data is None — preserving existing file at %s", path)
        return False

    Path(path).parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "_meta": {
            "last_synced": datetime.now(timezone.utc).isoformat(),
            "source":      "football-data.org v4",
        },
        "data": data,
    }

    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        logger.info("Wrote %s (%d bytes)", path, os.path.getsize(path))
        return True
    except OSError as exc:
        logger.error("Failed to write %s: %s", path, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ── HELPERS ───────────────────────────────────────────────────────────────────

def strip_fields(obj: dict, fields: list[str]) -> dict:
    """Remove keys in `fields` from `obj` in-place and return obj."""
    for field in fields:
        obj.pop(field, None)
    return obj


def strip_fields_list(items: list[dict], fields: list[str]) -> list[dict]:
    """Apply strip_fields to every item in a list."""
    return [strip_fields(item, fields) for item in items]


def normalize_nulls(obj: dict, schema_keys: list[str]) -> dict:
    """Ensure every key in schema_keys exists in obj (set to None if missing)."""
    for key in schema_keys:
        if key not in obj:
            obj[key] = None
    return obj