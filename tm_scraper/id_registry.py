"""
tm_scraper/id_registry.py
──────────────────────────────────────────────────────────────────────────────
Thread-safe ID allocator for entities that have NO football-data.org ID —
national teams (Portugal, England, ...) and any club that shows up in a cup
competition but isn't tracked by the 5-league football-data pipeline.

Resolution order (enforced by the CALLER in cup_runner.py, not here):
  1. Try to match the entity by name against team_index / player_index —
     these are built from data/{season}/{league}/teams.json, i.e. real
     football-data IDs. If found, use that ID. This registry is never
     consulted for entities that already have a football-data ID — a club
     playing in both its domestic league and the Champions League keeps
     ONE id and ONE team_informations/{id}/ folder.
  2. Only if that lookup misses does the caller ask THIS registry for an id.

Once an entity gets a synthetic id here, it's permanent: persisted to
data/id_registry.json (one small file — not a new folder tree) so
"Portugal" resolves to the exact same team_informations/{id}/ folder every
time, across every competition and every future run.

Synthetic ids start at 1,000,000 — well clear of football-data's own id
space — so there's no collision risk with real football-data ids.

Thread safety matters here specifically because runner concurrency
(TEAM_WORKERS × PLAYER_WORKERS) means multiple never-seen-before entities
can request a new id in the same instant; the lock + immediate persist
prevents two workers from ever being handed the same id.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from threading import Lock

logger = logging.getLogger("id_registry")

_TEAM_START = 1_000_000
_PLAYER_START = 1_000_000

# os.replace() on Windows can raise PermissionError / WinError 5 ("Access is
# denied") if the destination file is momentarily held open by something
# OUTSIDE this process — Windows Search Indexer, antivirus real-time
# scanning, OneDrive/Dropbox sync, a backup agent, etc. This registry gets
# rewritten on every single new team/player assignment during a run, so it's
# exactly the kind of small, frequently-rewritten file that collides with a
# transient external lock like that. The fix is NOT more in-process locking
# (self._lock already fully serializes writes from THIS process) — it's a
# short retry-with-backoff around the replace step, since the external lock
# is normally released within milliseconds.
_SAVE_MAX_ATTEMPTS = 6
_SAVE_BASE_DELAY = 0.15  # seconds; doubles each attempt, ± jitter


class IdRegistry:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = Lock()
        self._data = self._load()

    # ── persistence ───────────────────────────────────────────────────────
    def _load(self) -> dict:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                raw.setdefault("teams", {})
                raw.setdefault("players", {})
                raw.setdefault("next_team_id", _TEAM_START)
                raw.setdefault("next_player_id", _PLAYER_START)
                return raw
            except Exception:
                pass  # corrupt file → start fresh rather than crash the run
        return {
            "teams": {},
            "players": {},
            "next_team_id": _TEAM_START,
            "next_player_id": _PLAYER_START,
        }

    def _save(self) -> bool:
        """
        Atomically persist self._data to disk. Retries the replace step on
        transient PermissionError/OSError (Windows file-lock contention —
        see module docstring above). Returns True on success, False if all
        retries are exhausted.

        IMPORTANT: a False return does NOT mean the ID assignment was lost —
        self._data already has it in memory, and the caller (get_or_create_*)
        still hands back the correct id. Only the on-disk file is stale until
        the NEXT successful save (which happens on the very next new
        assignment). A crash before any successful save would lose the
        run's synthetic assignments, so don't kill -9 the process mid-run if
        this warning shows up repeatedly.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Unique tmp filename per attempt (pid + random) rather than a fixed
        # ".tmp" suffix — cheap defense-in-depth against two IdRegistry
        # instances (e.g. two cup_runner.py processes launched in separate
        # terminals at once) writing to the same tmp path simultaneously.
        # NOTE: this does NOT make cross-process writes fully safe — two
        # separate processes still have independent Locks and independent
        # in-memory next_id counters, so running two cup_runner invocations
        # concurrently can still race on *which* id a brand-new name gets.
        # Avoid running more than one cup_runner/runner process against the
        # same data/ directory at the same time.
        tmp = f"{self._path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"

        payload = json.dumps(self._data, ensure_ascii=False, indent=2)

        last_exc: Exception | None = None
        for attempt in range(1, _SAVE_MAX_ATTEMPTS + 1):
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self._path)
                return True
            except (PermissionError, OSError) as exc:
                last_exc = exc
                # Clean up our own tmp file before retrying so repeated
                # failures don't leak a pile of tmp files behind.
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if attempt < _SAVE_MAX_ATTEMPTS:
                    delay = _SAVE_BASE_DELAY * (2 ** (attempt - 1))
                    delay += random.uniform(0, delay * 0.5)
                    logger.debug(
                        "[id_registry] save attempt %d/%d failed (%s) — retrying in %.2fs",
                        attempt, _SAVE_MAX_ATTEMPTS, exc, delay,
                    )
                    time.sleep(delay)

        logger.warning(
            "[id_registry] Could not persist %s after %d attempts (%s). "
            "The new ID is still valid for this run — it's held in memory — "
            "but the on-disk file is stale until the next successful save.",
            self._path, _SAVE_MAX_ATTEMPTS, last_exc,
        )
        return False

    # ── public API ────────────────────────────────────────────────────────
    def get_or_create_team_id(self, name: str) -> int:
        key = name.lower().strip()
        with self._lock:
            existing = self._data["teams"].get(key)
            if existing is not None:
                return existing
            new_id = self._data["next_team_id"]
            self._data["teams"][key] = new_id
            self._data["next_team_id"] = new_id + 1
            self._save()  # best-effort; see _save() docstring
            return new_id

    def get_or_create_player_id(self, name: str) -> int:
        key = name.lower().strip()
        with self._lock:
            existing = self._data["players"].get(key)
            if existing is not None:
                return existing
            new_id = self._data["next_player_id"]
            self._data["players"][key] = new_id
            self._data["next_player_id"] = new_id + 1
            self._save()  # best-effort; see _save() docstring
            return new_id