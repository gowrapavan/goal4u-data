#!/usr/bin/env python3
"""
workers/stats365.py — Map football-data.org match IDs to 365scores IDs and write
match stats in the SAME FORMAT as last season's stats.json (the old yallashoot file),
plus per-player stats / heat maps / shot map keyed by YOUR football-data player ids,
and the player headshots saved as public/headshot/{football-data player id}.png.

For every league in config.LEAGUE_COMPETITIONS, in the CURRENT season folder:

    match_map.json   {football-data match id: 365scores id + team ids}  (+ comp365)
    stats.json       {"_meta": {...}, "data": {fd match id: entry}}     <- old format

Each entry has exactly the old keys: scraped_at, source_url, status, home_team,
away_team, score{home,away,ht_home,ht_away}, stats{...}, events[...], lineups{...},
match_id, fd_competition_code.  (Plus optional extras — turn ALL of them off with --no-extras:
    s365_id, recorded, all_stats, schema, venue, referee, player_of_the_match,
    play_time, predictions, top_performers, shots (shot map with xG), video_url;
    events get player_id / player_s365_id / added_time;
    every lineup player gets id (football-data player id), s365_id, position_detail,
    minutes, pitch, sub_on / sub_off, stats (~25 per-player stats) and heatmap;
    lineups get coach_id, coach_s365_id, confirmed and missing (injured / suspended).)

Player identity (football-data id <-> 365scores athleteId)
    data/player_map_365.json        built automatically: name + team + position matching,
                                    remembered forever ("players", "coaches", "unmatched")
    data/player_overrides_365.json  OPTIONAL manual fixes {"<365 athleteId>": <fd player id>}
                                    (use 0 to say "never match this one")
    Players that could not be matched stay id=null in stats.json and are listed under
    "unmatched" in player_map_365.json -> copy their athleteId into the overrides file.

Headshots
    public/headshot/{fd player id}.png   downloaded once per player (365 athleteId image)
    data/headshots_365.json              manifest (image version, players with no photo)
    A photo is only re-downloaded when 365 bumps its imageVersion.  The 365 "default
    silhouette" is detected and NOT saved.

Season is chosen from today's date: a season runs Aug -> May, so
    Aug 2026 .. Jul 2027  ->  2026-2027
    Aug 2027 .. Jul 2028  ->  2027-2028     (June/July stay on the season that just ended)

Checkpoint (stats.json -> "status")
    "full time"  recorded: match finished and fully saved -> NEVER fetched again
    "live" / "half time"  captured during the match      -> refreshed on every run

Usage
    python -m workers.stats365                              # all leagues, current season
    python -m workers.stats365 --competition PL             # one league
    python -m workers.stats365 --mode map                   # only map ids
    python -m workers.stats365 --mode stats                 # only fetch stats
    python -m workers.stats365 --mode images                # only (back)fill headshots for matched players
    python -m workers.stats365 --max-minutes 40             # stop gracefully after 40 min (CI)
    python -m workers.stats365 --competition PL --debug 537788
    python -m workers.stats365 --competition SA --rebuild   # re-fetch already "full time" entries
    python -m workers.stats365 --no-images                  # do not download headshots
    python -m workers.stats365 --split-details              # heavy per-player data -> match_details/{id}.json
    python -m workers.stats365 --mode remap                 # LOCAL, instant: re-match unmatched people and
                                                            # fill the new ids into the existing stats.json
    python -m workers.stats365 --no-upgrade                 # do not re-fetch old-schema entries

Upgrading: entries written by an older version of this script (no "schema": 2) are fetched
ONE more time automatically so they receive the player ids / stats / heat maps.

Manual fixes (all optional JSON files)
    data/{season}/{CODE}/match_overrides.json   {"<fd_match_id>": <365_match_id>}
    data/team_overrides_365.json                {"<fd_team_id>": <365_team_id>}

Environment
    STATS365_PROXY / WEBSHARE_PROXY_URL   proxy used on retries (helps on GitHub Actions)
    S365_ALLSCORES_URL                    override the fixtures endpoint
    STATS365_MIN_INTERVAL                 seconds between API requests (default 2.0)
    STATS365_IMG_INTERVAL                 seconds between image downloads (default 0.4)
    S365_HEADSHOT_URL                     headshot url template, {id} = 365 athleteId
    HEADSHOT_DIR                          where headshots are saved (default public/headshot)

NOTE: 365scores / koora-l are undocumented third-party services. They can change,
rate-limit or block you without notice.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
import hashlib

import requests

sys.path.insert(0, ".")

from config import LEAGUE_COMPETITIONS  # type: ignore[import]
from workers.tournament_paths import get_data_paths, is_tournament
from workers.utils import safe_write

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stats365")

# ── Config ────────────────────────────────────────────────────────────────────

KOORA_API = "https://www.koora-l.live"
S365_ALLSCORES = os.environ.get("S365_ALLSCORES_URL",
                                "https://webws.365scores.com/web/games/allscores/")

# football-data code -> 365scores competitionId.  Only SA=17 is confirmed.
# Any league missing here is auto-discovered on its first run and cached in
# match_map.json ("comp365"); check that value once.
S365_COMPETITION: dict[str, int] = {"SA": 17}

SEASON_START_MONTH = 8          # season = Aug .. Jul  (Aug 2026 -> 2026-2027)

MATCH_WINDOW_MIN = 15           # kickoff tolerance (strict pass)
WEAK_WINDOW_MIN = 5             # one-team-only fallback
LOOSE_WINDOW_H = 36             # both teams recognised, kickoff time differs
MIN_INTERVAL = float(os.environ.get("STATS365_MIN_INTERVAL", "2.0"))   # seconds between API requests
IMG_INTERVAL = float(os.environ.get("STATS365_IMG_INTERVAL", "0.4"))   # seconds between image downloads
CHECKPOINT_DAYS = 10            # save the map every N fixture days
CHECKPOINT_MATCHES = 20         # save stats every N fetched matches
MAX_STATS_TRIES = 6             # empty-stats retries before giving up on an old match...
GIVE_UP_AFTER_DAYS = 3          # ...once the match is at least this old

LIVE_STATUSES = {"IN_PLAY", "PAUSED"}
SCHEDULED_STATUSES = {"TIMED", "SCHEDULED"}
MAPPABLE_STATUSES = {"FINISHED"} | LIVE_STATUSES | SCHEDULED_STATUSES

TEAM_OVERRIDES_PATH = "data/team_overrides_365.json"
PLAYER_MAP_PATH = "data/player_map_365.json"              # athleteId -> football-data player id
PLAYER_OVERRIDES_PATH = "data/player_overrides_365.json"  # manual fixes (optional)

SCHEMA_VERSION = 2              # entries below this are re-fetched once (see --no-upgrade)

HEADSHOT_DIR = os.environ.get("HEADSHOT_DIR", "public/headshot")
HEADSHOT_URL = os.environ.get(
    "S365_HEADSHOT_URL",
    "https://imagecache.365scores.com/image/upload/f_png,w_80,h_80,c_limit,q_auto:eco,dpr_2,"
    "d_Athletes:default.png,r_max,c_thumb,g_face,z_0.65/Athletes/{id}")
HEADSHOT_MANIFEST = "data/headshots_365.json"
NO_PHOTO_RETRY_DAYS = 30        # a player whose 365 photo is the default silhouette is retried after this

TEAM_MATCH_MIN = 0.82           # name score needed inside the player's own squad
LEAGUE_MATCH_MIN = 0.95         # ...and when searching every other squad / league / season (exact full name only)
AMBIGUITY_GAP = 0.03            # two candidates closer than this -> do not guess

# ── Stat translation (koora-l stat id -> (key, English name)) ─────────────────
# Ids and Arabic names were read from 60 real matches.  Shots == on + off + blocked
# held in every team row.  Unknown future ids are kept as "stat_<id>" with the
# Arabic name until you add them here.

STATS: dict[int, tuple[str, str]] = {
    1:   ("yellow_cards",            "Yellow Cards"),
    2:   ("red_cards",               "Red Cards"),
    3:   ("shots",                   "Total Shots"),
    4:   ("shots_on_goal",           "Shots on Target"),
    5:   ("shots_off_goal",          "Shots off Target"),
    6:   ("blocked_shots",           "Blocked Shots"),
    8:   ("corner_kicks",            "Corners"),
    9:   ("offsides",                "Offsides"),
    10:  ("possession",              "Possession"),
    12:  ("fouls",                   "Fouls"),
    13:  ("free_kicks",              "Free Kicks"),
    14:  ("goal_kicks",              "Goal Kicks"),
    15:  ("throw_ins",               "Throw-ins"),
    19:  ("accurate_passes",         "Accurate Passes"),
    21:  ("total_passes",            "Total Passes"),
    23:  ("saves",                   "Goalkeeper Saves"),
    24:  ("dangerous_chances",       "Dangerous Chances"),
    25:  ("hit_woodwork",            "Hit Woodwork"),
    29:  ("substitutions",           "Substitutions"),
    11:  ("attacks",                 "Attacks"),
    36:  ("big_chances_missed",      "Big Chances Missed"),
    37:  ("fouls_won",               "Fouls Won"),
    40:  ("clearances",              "Clearances"),
    41:  ("interceptions",           "Interceptions"),
    46:  ("chances_created",         "Chances Created"),
    51:  ("tackles_won",             "Successful Tackles"),
    52:  ("crosses_completed",       "Completed Crosses"),
    53:  ("long_passes_completed",   "Completed Long Passes"),
    54:  ("dribbles_completed",      "Completed Dribbles"),
    55:  ("ground_duels_won",        "Ground Duels Won"),
    56:  ("aerial_duels_won",        "Aerial Duels Won"),
    60:  ("dribbled_past",           "Dribbled Past"),
    66:  ("errors_leading_to_goal",  "Errors Leading to Goal"),
    73:  ("possession_lost",         "Possession Lost"),
    76:  ("xg",                      "Expected Goals (xG)"),
    77:  ("xg_conceded",             "Expected Goals Conceded"),
    78:  ("xg_created",              "Expected Goals Created"),
    79:  ("xg_on_target",            "Expected Goals on Target (xGOT)"),
    80:  ("passes_final_third",      "Passes into Final Third"),
    81:  ("back_passes",             "Back Passes"),
    84:  ("final_third_possession",  "Possession in Final Third"),
    146: ("shots_inside_box",        "Shots Inside Box"),
    147: ("shots_outside_box",       "Shots Outside Box"),
    148: ("passes_own_half",         "Passes in Own Half"),
    149: ("passes_opposition_half",  "Passes in Opposition Half"),
    150: ("duels_won",               "Duels Won"),
    153: ("penalized_corners",       "Penalized Corners"),
}

# ── Deadline (so a scheduled CI run can stop gracefully) ──────────────────────

_DEADLINE: Optional[float] = None
_RUN_NOTES: list[dict] = []          # one row per league, shown on the GitHub Actions summary page


def _out_of_time() -> bool:
    return _DEADLINE is not None and time.monotonic() >= _DEADLINE


# ── Season by date ────────────────────────────────────────────────────────────

def current_season_start_year(today: Optional[datetime] = None) -> int:
    """Aug 2026 .. Jul 2027 -> 2026 ; Aug 2027 .. Jul 2028 -> 2027 ..."""
    t = today or datetime.now(timezone.utc)
    return t.year if t.month >= SEASON_START_MONTH else t.year - 1


def season_label(start_year: int) -> str:
    return f"{start_year}-{start_year + 1}"


# ── HTTP ──────────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.365scores.com",
    "Referer": "https://www.365scores.com/",
}
_TIMEOUT = 30
_last_req = 0.0


def _proxy() -> Optional[str]:
    return os.environ.get("STATS365_PROXY") or os.environ.get("WEBSHARE_PROXY_URL")


def _one_request(url: str, params: dict | None, proxy: Optional[str], accept: Optional[str] = None):
    """Browser-impersonating request (curl_cffi) with plain requests as fallback."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    _HEADERS_ = {**_HEADERS, "Accept": accept} if accept else _HEADERS
    try:
        from curl_cffi import requests as cffi
    except ImportError:
        logger.warning("curl_cffi not installed - using plain requests (pip install curl_cffi)")
        return requests.get(url, params=params, headers=_HEADERS_,
                            timeout=_TIMEOUT, proxies=proxies)
    return cffi.get(url, params=params, headers=_HEADERS_, impersonate="chrome120",
                    timeout=_TIMEOUT, proxies=proxies)


def _get_json(url: str, params: dict | None = None, retries: int = 3) -> dict | None:
    """GET json. First attempt is direct; retries go through the proxy if one is set."""
    global _last_req
    proxy = _proxy()
    for attempt in range(1, retries + 1):
        if _out_of_time():
            return None
        wait = MIN_INTERVAL - (time.monotonic() - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.monotonic()
        try:
            r = _one_request(url, params, proxy if attempt > 1 else None)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(30 * attempt)
                continue
            logger.warning("HTTP %d for %s (body: %.120s)", r.status_code, url, r.text)
            if r.status_code < 500 and r.status_code not in (403,):
                return None
        except Exception as exc:                      # timeouts, TLS, JSON errors
            logger.warning("Request error (%d/%d) %s: %s", attempt, retries, url, exc)
        time.sleep(2 ** attempt)
    return None


_last_img = 0.0


def _get_bytes(url: str, retries: int = 3) -> tuple[Optional[bytes], int]:
    """Download a binary file (image).  Returns (bytes, 200) or (None, last http status)."""
    global _last_img
    proxy = _proxy()
    status = 0
    for attempt in range(1, retries + 1):
        if _out_of_time():
            return None, 0
        wait = IMG_INTERVAL - (time.monotonic() - _last_img)
        if wait > 0:
            time.sleep(wait)
        _last_img = time.monotonic()
        try:
            r = _one_request(url, None, proxy if attempt > 1 else None, accept="image/png,image/*,*/*;q=0.8")
            status = r.status_code
            if status == 200:
                return r.content, 200
            if status == 429:
                time.sleep(30 * attempt)
                continue
            if status < 500 and status != 403:
                return None, status
        except Exception as exc:
            logger.warning("Image request error (%d/%d) %s: %s", attempt, retries, url, exc)
        time.sleep(2 ** attempt)
    return None, status


# ── Local file helpers ────────────────────────────────────────────────────────

def _load_data(path: str, default):
    """Read the 'data' part of a safe_write() envelope."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("data", default)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _read_json(path: str) -> dict:
    """Read a plain (non-envelope) json file; {} if missing or invalid."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _paths(code: str, season_str: Optional[str]) -> dict[str, str]:
    p = get_data_paths(code, season=season_str)
    root = p["root"]
    return {
        **p,
        "map":       f"{root}/match_map.json",
        "stats":     f"{root}/stats.json",
        "overrides": f"{root}/match_overrides.json",
    }


# ── Name matching ─────────────────────────────────────────────────────────────

_NOISE = re.compile(r"\b(fc|cf|ac|as|ssc|us|afc|calcio|club|de|1913|1899|1909)\b")


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _NOISE.sub(" ", s)
    return re.sub(r"[^a-z0-9]", "", s)


ALIAS = {            # normalised -> normalised; extend as UNMATCHED rows show up
    "inter": "internazionale",
    "manunited": "manchesterunited",
    "manutd": "manchesterunited",
    "spurs": "tottenhamhotspur",
}


def _names_fd(team: dict) -> set[str]:
    out = {_norm(team.get(k)) for k in ("name", "shortName", "tla")}
    out |= {ALIAS[n] for n in list(out) if n in ALIAS}
    return {n for n in out if n}


def _names_365(comp: dict) -> set[str]:
    keys = ("name", "shortName", "nameForURL", "symbolicName")
    out = {_norm(comp.get(k)) for k in keys}
    out |= {ALIAS[n] for n in list(out) if n in ALIAS}
    return {n for n in out if n}


def _same_team(fd_team: dict, comp365: dict, team_map: dict) -> bool:
    fid, cid = fd_team.get("id"), comp365.get("id")
    if fid is not None and cid is not None and team_map.get(str(fid)) == cid:
        return True
    a, b = _names_fd(fd_team), _names_365(comp365)
    if a & b:
        return True
    for x in a:
        for y in b:
            if len(x) >= 4 and len(y) >= 4 and (x in y or y in x):
                return True
            if SequenceMatcher(None, x, y).ratio() >= 0.88:
                return True
    return False


# ── 365scores fixtures ────────────────────────────────────────────────────────

def fetch_365_games(d_from: datetime, d_to: datetime,
                    comp_id: Optional[int]) -> Optional[list[dict]]:
    """Fixtures for a date window. None = request failed, [] = genuinely empty."""
    params = {
        "appTypeId": 5, "langId": 1, "timezoneName": "UTC",
        "startDate": d_from.strftime("%d/%m/%Y"),
        "endDate":   d_to.strftime("%d/%m/%Y"),
        "sports": 1,
    }
    if comp_id is not None:
        params["competitions"] = comp_id
    data = _get_json(S365_ALLSCORES, params=params)
    if data is None:
        return None
    return data.get("games") or []


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _entry(g, fh, fa, fd_home_365, fd_away_365, how) -> dict:
    return {
        "s365_id": g["id"],
        "fd_home_s365_id": fd_home_365,
        "fd_away_s365_id": fd_away_365,
        "how": how,
        "fd_home_id": fh.get("id"),
        "fd_away_id": fa.get("id"),
    }


def match_one(fd: dict, games: list[dict], team_map: dict) -> dict | None:
    """Return a map entry for one football-data match, or None."""
    fd_dt = _dt(fd.get("utcDate"))
    fh, fa = fd.get("homeTeam") or {}, fd.get("awayTeam") or {}
    if not fd_dt or not fh.get("id") or not fa.get("id"):
        return None

    cands = []
    for g in games:
        g_dt = _dt(g.get("startTime"))
        if g_dt and abs(g_dt - fd_dt) <= timedelta(minutes=MATCH_WINDOW_MIN):
            cands.append((abs(g_dt - fd_dt), g))
    cands.sort(key=lambda x: x[0])

    for _, g in cands:
        h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
        if _same_team(fh, h, team_map) and _same_team(fa, a, team_map):
            return _entry(g, fh, fa, h["id"], a["id"], "teams")
        if _same_team(fh, a, team_map) and _same_team(fa, h, team_map):
            return _entry(g, fh, fa, a["id"], h["id"], "teams_swapped")

    # Loose: kickoff differs (rescheduled / placeholder time) but BOTH teams match.
    # In a league the same pair never meets twice within 36h.
    for g in games:
        g_dt = _dt(g.get("startTime"))
        if not g_dt or abs(g_dt - fd_dt) > timedelta(hours=LOOSE_WINDOW_H):
            continue
        h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
        if _same_team(fh, h, team_map) and _same_team(fa, a, team_map):
            return _entry(g, fh, fa, h["id"], a["id"], "teams_loose")
        if _same_team(fh, a, team_map) and _same_team(fa, h, team_map):
            return _entry(g, fh, fa, a["id"], h["id"], "teams_loose_swapped")

    # Weak: single candidate nearly at kickoff and ONE team recognised
    close = [g for dt, g in cands if dt <= timedelta(minutes=WEAK_WINDOW_MIN)]
    if len(close) == 1:
        g = close[0]
        h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
        if _same_team(fh, h, team_map) or _same_team(fa, a, team_map):
            return _entry(g, fh, fa, h["id"], a["id"], "weak")
        if _same_team(fh, a, team_map) or _same_team(fa, h, team_map):
            return _entry(g, fh, fa, a["id"], h["id"], "weak")
    return None


LEARN_HOW = ("teams", "teams_swapped", "teams_loose", "teams_loose_swapped")


def _match_pass(todo: list[dict], games: list[dict], mapping: dict, team_map: dict) -> list[dict]:
    """Try to place every match in `todo`; returns the ones still unplaced."""
    left = []
    for m in todo:
        if str(m["id"]) in mapping:
            continue
        hit = match_one(m, games, team_map)
        if not hit:
            left.append(m)
            continue
        mapping[str(m["id"])] = hit
        if hit["how"] in LEARN_HOW:                    # learn team ids (never from weak matches)
            team_map[str(hit["fd_home_id"])] = hit["fd_home_s365_id"]
            team_map[str(hit["fd_away_id"])] = hit["fd_away_s365_id"]
    return left


# ── Step 1: build the match map ───────────────────────────────────────────────

def _mappable(m: dict, now: datetime, horizon_days: Optional[int] = None) -> bool:
    """Every match whose two teams are known (POSTPONED/CANCELLED retried when they change)."""
    if m.get("status") not in MAPPABLE_STATUSES:
        return False
    if not (m.get("homeTeam") or {}).get("id") or not (m.get("awayTeam") or {}).get("id"):
        return False                                   # knockout slot with teams still TBD
    d = _dt(m.get("utcDate"))
    if not d:
        return False
    if horizon_days is not None and d > now + timedelta(days=horizon_days):
        return False
    return True


def _resolve_override(fd: dict, s365_id: int, team_map: dict) -> dict:
    """Manual 365 id -> full entry; works out home/away orientation from koora /game/."""
    fh, fa = fd.get("homeTeam") or {}, fd.get("awayTeam") or {}
    ent = {"s365_id": s365_id, "how": "manual",
           "fd_home_id": fh.get("id"), "fd_away_id": fa.get("id")}
    game = ((_get_json(f"{KOORA_API}/game/{s365_id}") or {}).get("game")) or {}
    h, a = game.get("homeCompetitor") or {}, game.get("awayCompetitor") or {}
    if not (h.get("id") and a.get("id")):
        logger.warning("override %s: could not read /game/ - assuming same home/away", s365_id)
        ent["how"] = "manual_assumed"
        ent["fd_home_s365_id"], ent["fd_away_s365_id"] = None, None
        return ent
    swapped = _same_team(fh, a, team_map) and not _same_team(fh, h, team_map)
    ent["fd_home_s365_id"], ent["fd_away_s365_id"] = (a["id"], h["id"]) if swapped else (h["id"], a["id"])
    return ent


def discover_comp365(todo: list[dict], fetch_day) -> Optional[int]:
    """
    Work out a league's 365scores competitionId: on up to 6 sample days, find games
    whose BOTH teams and kickoff match ours, and take the most common competitionId.
    """
    votes: Counter = Counter()
    dates = sorted({_dt(m["utcDate"]).date() for m in todo})
    step = max(1, len(dates) // 6)
    for dd in dates[::step][:6]:
        games = fetch_day(dd)
        if not games:
            continue
        for m in todo:
            d = _dt(m["utcDate"])
            if d.date() != dd:
                continue
            fh, fa = m.get("homeTeam") or {}, m.get("awayTeam") or {}
            for g in games:
                g_dt = _dt(g.get("startTime"))
                if not g_dt or abs(g_dt - d) > timedelta(minutes=MATCH_WINDOW_MIN):
                    continue
                h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
                if ((_same_team(fh, h, {}) and _same_team(fa, a, {})) or
                        (_same_team(fh, a, {}) and _same_team(fa, h, {}))):
                    votes[g.get("competitionId")] += 1
                    break
    votes.pop(None, None)
    if not votes:
        return None
    cid, n = votes.most_common(1)[0]
    if n / sum(votes.values()) < 0.7:
        logger.warning("competition id vote is split: %s", dict(votes))
        return None
    logger.info("discovered 365scores competitionId=%s (%d votes of %d)", cid, n, sum(votes.values()))
    return cid


def build_map(code: str, season_str: Optional[str], comp_id: Optional[int],
              horizon_days: Optional[int] = None) -> None:
    p = _paths(code, season_str)
    matches = _load_data(p["matches"], [])
    if not matches:
        logger.warning("%s: no matches.json at %s - run fetch_matches first", code, p["matches"])
        return

    mp: dict = _load_data(p["map"], {})
    mapping: dict = mp.setdefault("matches", {})
    team_map: dict = mp.setdefault("teams", {})
    team_map.update({str(k): v for k, v in _read_json(TEAM_OVERRIDES_PATH).items()})
    comp_id = comp_id or mp.get("comp365")
    overrides: dict = _read_json(p["overrides"])

    now = datetime.now(timezone.utc)
    by_id = {str(m["id"]): m for m in matches if m.get("id")}

    for fid, ov in overrides.items():                  # manual overrides: any status
        m = by_id.get(str(fid))
        if m and mapping.get(str(fid), {}).get("s365_id") != ov:
            mapping[str(fid)] = _resolve_override(m, ov, team_map)
            logger.info("%s: manual override fd=%s -> 365=%s", code, fid, ov)

    todo = [m for m in matches
            if str(m.get("id")) not in mapping and _mappable(m, now, horizon_days)]
    if not todo:
        logger.info("%s: nothing new to map (%d already mapped)", code, len(mapping))
        mp["unmatched"] = []
        safe_write(p["map"], mp)
        return

    # ONE request per day that has an unmapped match (long ranges get truncated).
    # Kickoffs within 20 min of midnight also pull in the neighbouring day.
    dates = set()
    for m in todo:
        d = _dt(m["utcDate"])
        dates.add((d - timedelta(minutes=20)).date())
        dates.add((d + timedelta(minutes=20)).date())
    dates = sorted(dates)

    day_cache: dict = {}

    def fetch_day(dd):
        if dd not in day_cache:
            day = datetime(dd.year, dd.month, dd.day, tzinfo=timezone.utc)
            got = fetch_365_games(day, day, comp_id)
            if got is None:
                return None
            day_cache[dd] = got
        return day_cache[dd]

    if comp_id is None:
        comp_id = discover_comp365(todo, fetch_day)
        if comp_id is None:
            logger.error("%s: could not discover the 365scores competition id. Find it with "
                         "--debug <fd_match_id> and pass --comp365 <id>.", code)
            return
        day_cache.clear()                              # refetch with the competition filter
    mp["comp365"] = comp_id

    logger.info("%s: %d matches to map across %d days (comp365=%s)",
                code, len(todo), len(dates), comp_id)

    games: list[dict] = []
    seen_ids: set = set()
    fetched: set = set()
    pending = list(todo)

    def in_fetched(m) -> bool:
        return _dt(m["utcDate"]).date() in fetched

    for i, dd in enumerate(dates, 1):
        if _out_of_time():
            logger.warning("%s: time budget reached - progress saved, rerun to continue", code)
            break
        got = fetch_day(dd)
        if got is None:
            logger.warning("%s: day %s failed - those matches are retried next run", code, dd)
            continue
        fetched.add(dd)
        mine = [g for g in got if g.get("competitionId") == comp_id]
        logger.info("%s: day %s (%d/%d) -> %d games, %d in competition",
                    code, dd, i, len(dates), len(got), len(mine))
        for g in mine:
            if g.get("id") not in seen_ids:
                seen_ids.add(g.get("id"))
                games.append(g)

        if i % CHECKPOINT_DAYS == 0:                   # survive Ctrl+C / CI timeouts
            ready = [m for m in pending if in_fetched(m)]
            ready_ids = {m["id"] for m in ready}
            pending = _match_pass(ready, games, mapping, team_map) + \
                      [m for m in pending if m["id"] not in ready_ids]
            mp["unmatched"] = []
            safe_write(p["map"], mp)
            logger.info("%s: checkpoint saved (%d mapped so far)", code, len(mapping))

    ready = [m for m in pending if in_fetched(m)]
    unplaced = _match_pass(ready, games, mapping, team_map)
    unmatched = [{"fd_id": m.get("id"), "utcDate": m.get("utcDate"), "status": m.get("status"),
                  "home": (m.get("homeTeam") or {}).get("name"),
                  "away": (m.get("awayTeam") or {}).get("name"),
                  "home_id": (m.get("homeTeam") or {}).get("id"),
                  "away_id": (m.get("awayTeam") or {}).get("id")} for m in unplaced]

    weak = sum(1 for v in mapping.values() if v.get("how") == "weak")
    loose = sum(1 for v in mapping.values() if str(v.get("how", "")).startswith("teams_loose"))
    logger.info("%s: mapped=%d unmatched=%d weak(review)=%d time-differs(loose)=%d",
                code, len(mapping), len(unmatched), weak, loose)
    for u in unmatched:
        past = _dt(u["utcDate"]) < now
        (logger.warning if past else logger.info)(
            "UNMATCHED%s fd=%s %s  %s(%s) vs %s(%s)", "" if past else " (future, will retry)",
            u["fd_id"], u["utcDate"], u["home"], u["home_id"], u["away"], u["away_id"])
    mp["unmatched"] = unmatched
    safe_write(p["map"], mp)


# ── Step 2: fetch stats + game details, write the OLD stats.json format ──────

INCLUDE_EXTRAS = True           # s365_id / recorded / all_stats (see --no-extras)

# stats.json key -> koora stat id.  Order = key order in last season's file.
# None = derived (goals from the score, yellow2rcards from events).
OLD_STAT_IDS: list[tuple[str, Optional[int]]] = [
    ("yellow_cards", 1), ("yellow2rcards", None), ("red_cards", 2), ("corners", 8),
    ("fouls", 12), ("offsides", 9), ("possession", 10), ("shots", 3),
    ("shots_on_target", 4), ("goals", None), ("shots_off_goal", 5), ("blocked_shots", 6),
    ("shots_insidebox", 146), ("shots_outsidebox", 147), ("goalkeeper_saves", 23),
    ("total_passes", 21), ("passes_accurate", 19), ("expected_goals", 76),
]

FINAL_STATUS = "full time"      # same wording as the old file; == "recorded"
STATUS_TEXT = {"FINISHED": FINAL_STATUS, "IN_PLAY": "live", "PAUSED": "half time"}

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _num(v):
    """'26%' -> 26.0, '0.19' -> 0.19, '12 (50%)' -> 12.0, otherwise unchanged."""
    if isinstance(v, (int, float)):
        return v
    m = _NUM.search(str(v or ""))
    return float(m.group()) if m else v


def index_stats(raw: dict, home_365: int, away_365: int) -> dict[int, dict]:
    """koora /stats response -> {stat id: {name_ar, home, away}} oriented to football-data home/away."""
    idx: dict[int, dict] = {}
    for s in raw.get("statistics") or []:
        cid, sid = s.get("competitorId"), s.get("id")
        if cid not in (home_365, away_365):
            continue
        row = idx.setdefault(sid, {"name_ar": s.get("name"), "home": None, "away": None})
        row["home" if cid == home_365 else "away"] = _num(s.get("value"))
    return idx


_LOGGED_UNKNOWN: set = set()


def translate_stats(idx: dict[int, dict]) -> dict:
    """All stats with English names (kept next to the Arabic)."""
    rows: dict[str, dict] = {}
    for sid, r in idx.items():
        key, en = STATS.get(sid, (f"stat_{sid}", None))
        rows[key] = {"id": sid, "name": en or r["name_ar"], "name_ar": r["name_ar"],
                     "home": r["home"], "away": r["away"]}
        if sid not in STATS and sid not in _LOGGED_UNKNOWN:      # once per id, not once per match
            _LOGGED_UNKNOWN.add(sid)
            logger.info("untranslated stat id %s (Arabic name: %s) - add it to STATS", sid, r["name_ar"])
    return dict(sorted(rows.items(), key=lambda kv: kv[1]["id"]))


def _fmt_stat(key: str, v):
    if key == "possession":
        return f"{int(round(float(v)))}%"
    if key == "expected_goals":
        return round(float(v), 2)
    return int(round(float(v)))


def old_stats(idx: dict[int, dict], score: dict, extra: dict) -> dict:
    """Old-format stats dict. Like the old file: a stat is omitted when both teams have 0/none."""
    out: dict = {}
    for key, sid in OLD_STAT_IDS:
        if key == "goals":
            h, a = score.get("home"), score.get("away")
        elif key == "yellow2rcards":
            h, a = extra.get("yellow2_home"), extra.get("yellow2_away")
        else:
            row = idx.get(sid)
            h, a = (row["home"], row["away"]) if row else (None, None)
        if h is None and a is None:
            continue
        h, a = h or 0, a or 0
        if h == 0 and a == 0:
            continue
        out[key] = {"home": _fmt_stat(key, h), "away": _fmt_stat(key, a)}
    return out


# ── 365scores game details (koora-l /game/) -> events / lineups ──────────────
# koora returns Arabic names, but every player/team also has an English URL slug
# ("maduka-okoye", "l.-miller", "udinese").  Accents are lost ("nicolo-zaniolo").

def fetch_game(s365_id: int) -> Optional[dict]:
    """Game details (events, lineups, members, stages) from koora-l /game/{id}."""
    return (_get_json(f"{KOORA_API}/game/{s365_id}") or {}).get("game")


_TEAM_UPPER = {"ac", "as", "fc", "ssc", "us", "afc", "cf", "psv", "psg", "rb", "fk", "sk", "rc",
               "ud", "cd", "vfb", "vfl", "tsg", "fsv", "rcd", "ca", "sc"}
_NAME_LOWER = {"da", "de", "di", "del", "della", "van", "von", "dos", "das", "do", "la", "le",
               "el", "al", "bin", "ibn", "ter", "ten", "der", "den", "y"}
_SLUG_OK = re.compile(r"[a-z0-9.'\-]+")


def _slug_name(slug: str, team: bool = False) -> str:
    out = []
    for i, p in enumerate(x for x in slug.split("-") if x):
        if team and p in _TEAM_UPPER:
            out.append(p.upper())
        elif i > 0 and p in _NAME_LOWER:
            out.append(p)
        else:
            out.append(p[:1].upper() + p[1:])
    return " ".join(out)


def _pname(m: Optional[dict]) -> Optional[str]:
    """English name from the URL slug; Arabic name if there is no usable slug."""
    if not m:
        return None
    slug = _fold(str(m.get("nameForURL") or ""))
    if slug and _SLUG_OK.fullmatch(slug):
        return _slug_name(slug)
    return (m.get("name") or m.get("shortName") or "").strip() or None


def _is_coach(m: dict) -> bool:
    if m.get("status") == 4:                                     # real data: 4 = "Management"
        return True
    txt = " ".join(str(x).lower() for x in [
        (m.get("position") or {}).get("name"), (m.get("position") or {}).get("shortName"),
        (m.get("formation") or {}).get("name"), (m.get("formation") or {}).get("shortName")] if x)
    return any(w in txt for w in ("coach", "manager", "مدرب", "الإدارة"))


def _pos_letter(m: dict) -> Optional[str]:
    pos, frm = (m.get("position") or {}), (m.get("formation") or {})
    short = str(pos.get("shortName") or frm.get("shortName") or "").lower()
    table = {"goalkeeper": "g", "defender": "d", "midfielder": "m", "attacker": "f", "forward": "f",
             "g": "g", "gk": "g", "d": "d", "df": "d", "def": "d", "m": "m", "mf": "m", "mid": "m",
             "f": "f", "fw": "f", "fwd": "f", "a": "f", "att": "f"}
    if short in table:
        return table[short]
    txt = " ".join(str(x).lower() for x in (pos.get("name"), frm.get("name")) if x)
    for pat, letter in ((r"goalkeep|حارس", "g"), (r"midfield|وسط", "m"),
                        (r"forward|striker|attack|wing|هجوم|مهاجم|جناح", "f"),
                        (r"defen|back|دفاع|مدافع", "d")):
        if re.search(pat, txt):
            return letter
    return None


def _base_minute(e: dict) -> int:
    m = re.match(r"\s*(\d+)", str(e.get("gameTimeDisplay") or ""))
    if m:
        return int(m.group(1))                 # "45+2'" -> 45, "90+3'" -> 90 (old file does the same)
    try:
        return int(float(e.get("gameTime")))
    except (TypeError, ValueError):
        return 0


_SEEN_SUBTYPES: set = set()


def convert_events(game: dict, home_id: int, away_id: int, ids: Optional[dict] = None) -> tuple[list[dict], dict]:
    """365 events -> old-format events (goal / yellow_card / red_card / substitution / penalty_missed)."""
    members = {m.get("id"): m for m in game.get("members") or [] if m}
    out: list[dict] = []
    extra = {"yellow2_home": 0, "yellow2_away": 0}

    def tag(ev: dict, e: dict, who: Optional[int], other: Optional[int] = None, other_key: str = "") -> dict:
        # other_key = "assistant" | "player_out"  ->  <key>_id (football-data) and <key>_s365_id
        """extras only: football-data / 365 ids of the people in the event + stoppage time"""
        if ids is None:
            return ev
        ident = ids.get(who) or {}
        if ident.get("fd"):
            ev["player_id"] = ident["fd"]
        if ident.get("athlete"):
            ev["player_s365_id"] = ident["athlete"]
        if other is not None and other_key:
            o = ids.get(other) or {}
            if o.get("fd"):
                ev[f"{other_key}_id"] = o["fd"]
            if o.get("athlete"):
                ev[f"{other_key}_s365_id"] = o["athlete"]
        try:
            add = int(float(e.get("addedTime") or 0))
        except (TypeError, ValueError):
            add = 0
        if add > 0:
            ev["added_time"] = add
        return ev

    for e in game.get("events") or []:
        et = e.get("eventType") or {}
        etype, sub_id = et.get("id"), et.get("subTypeId")
        text = f"{et.get('subTypeName') or ''} {et.get('name') or ''} " \
               f"{(e.get('eventSubType') or {}).get('name') or ''}".lower()
        if re.search(r"ملغ|cancel|disallow", text):
            continue
        disp = str(e.get("gameTimeDisplay") or "").strip()
        try:
            gt = float(e.get("gameTime"))
        except (TypeError, ValueError):
            gt = None
        if e.get("stageId") == 11 or (gt is not None and gt > 120 and re.fullmatch(r"\d{1,2}", disp)):
            continue                                             # penalty-shootout kicks

        cid = e.get("competitorId")
        team = "home" if cid == home_id else "away" if cid == away_id else None
        if team is None:
            continue
        minute = _base_minute(e)
        player = _pname(members.get(e.get("playerId")))
        extras = [x for x in (e.get("extraPlayers") or []) if x is not None]

        pen = bool(re.search(r"pen|جزاء", text))
        if pen and re.search(r"miss|ضائع|أضاع|اهدر|أهدر|مهدر|saved|تصدي", text):
            out.append(tag({"minute": minute, "type": "penalty_missed", "team": team, "player": player},
                           e, e.get("playerId")))
        elif etype == 1:                                         # goal
            own = bool(re.search(r"\bown\b|ذاتي|عكسي|مرماه", text))
            key = (etype, sub_id, et.get("subTypeName"))
            if key not in _SEEN_SUBTYPES:                        # help verify the guesses on real data
                _SEEN_SUBTYPES.add(key)
                logger.info("goal event subtype seen: id=%s name=%r", sub_id, et.get("subTypeName"))
            ev = {"minute": minute, "type": "goal", "team": team, "player": player,
                  "goal_type": "own_goal" if own else ("penalty" if pen else "normal")}
            if extras and not own:
                assist = _pname(members.get(extras[0]))
                if assist:
                    ev["assistant"] = assist
            out.append(tag(ev, e, e.get("playerId"), extras[0] if extras and not own else None, "assistant"))
        elif etype == 2:
            out.append(tag({"minute": minute, "type": "yellow_card", "team": team, "player": player},
                           e, e.get("playerId")))
        elif etype == 3:
            ev = {"minute": minute, "type": "red_card", "team": team, "player": player}
            if re.search(r"second|2nd|yellow|ثاني|ثانية|صفراء", text):
                ev["_second"] = True
            out.append(tag(ev, e, e.get("playerId")))
        elif etype in (1000, 4):                                 # substitution: playerId = IN, extra[0] = OUT
            out.append(tag({"minute": minute, "type": "substitution", "team": team, "player": player,
                            "player_out": _pname(members.get(extras[0])) if extras else None},
                           e, e.get("playerId"), extras[0] if extras else None, "player_out"))

    out.sort(key=lambda x: x["minute"])                          # stable: keeps source order within a minute
    out = _normalise_cards(out, extra)
    return out, extra


def _normalise_cards(events: list[dict], extra: dict) -> list[dict]:
    """
    Old file: a second yellow appears ONLY as a red_card event (the player's first yellow stays),
    and the stat yellow2rcards counts it.  365 may list a player's second yellow as another
    yellow event; fold it into a red_card so the format matches.
    """
    yellows: dict = {}
    counted: set = set()
    result: list[dict] = []
    for ev in events:
        k = (ev["team"], ev.get("player"))
        if ev["type"] == "yellow_card" and ev.get("player"):
            yellows[k] = yellows.get(k, 0) + 1
            if yellows[k] >= 2:                                  # second yellow -> red_card
                if k not in counted:
                    counted.add(k)
                    extra[f"yellow2_{ev['team']}"] += 1
                result.append({**ev, "type": "red_card"})             # keeps player_id etc.
                continue
        elif ev["type"] == "red_card":
            second = ev.pop("_second", False)
            if ev.get("player") and k not in counted:
                dup = any(r["type"] == "red_card" and (r["team"], r.get("player")) == k
                          and abs(r["minute"] - ev["minute"]) <= 1 for r in result)
                if dup:
                    continue                                     # already folded in from a second yellow
                if second:                                       # explicit 'second yellow' text from 365
                    counted.add(k)
                    extra[f"yellow2_{ev['team']}"] += 1
        ev.pop("_second", None)
        result.append(ev)
    return result


def _fix_own_goals(events: list[dict], score: dict) -> None:
    """Own goals credit the side that benefits.  If goal counts do not match the score and own
    goals exist, try the other convention (flip their team) before giving up."""
    def counts():
        g = [e for e in events if e["type"] == "goal"]
        return (sum(1 for e in g if e["team"] == "home"), sum(1 for e in g if e["team"] == "away"))
    sh, sa = score.get("home"), score.get("away")
    if sh is None or sa is None or counts() == (sh, sa):
        return
    own = [e for e in events if e["type"] == "goal" and e.get("goal_type") == "own_goal"]
    if not own:
        return
    for e in own:
        e["team"] = "away" if e["team"] == "home" else "home"
    if counts() != (sh, sa):                                     # flipping did not help: undo
        for e in own:
            e["team"] = "away" if e["team"] == "home" else "home"


def build_lineup(comp: dict, game: dict, ids: Optional[dict] = None) -> dict:
    members = {m.get("id"): m for m in game.get("members") or [] if m}
    lu = comp.get("lineups") or {}
    starters, bench, coach = [], [], None
    coach_mid: Optional[int] = None
    missing: list[dict] = []
    by_mid: dict = {}
    subs_info: list[tuple] = []                                  # (member id replaced, minute)

    for m in lu.get("members") or []:
        info = members.get(m.get("id"), {})
        merged = {**info, **{k: v for k, v in m.items() if v not in (None, {}, [], "")}}   # empty values must not hide real ones
        if _is_coach(merged) or _is_coach(m):
            if coach is None:
                coach_mid = m.get("id")
            coach = coach or _pname(info) or _pname(m)
            continue
        if m.get("status") == 3 and ids is not None:             # 3 = injured / suspended
            missing.append(_missing_player(m, info, ids.get(m.get("id")) or {}))
            continue
        if m.get("status") not in (1, 2):                        # 3 = injured / suspended: not in the squad
            continue
        try:
            r = float(m.get("ranking") if m.get("ranking") is not None else info.get("ranking"))
        except (TypeError, ValueError):
            r = 0.0                                              # -1.0 means "no rating"
        jersey = info.get("jerseyNumber", m.get("jerseyNumber"))
        player = {
            "name": _pname(info) or _pname(m),
            "number": "" if jersey is None or jersey < 0 else str(jersey),
            "position": _pos_letter(merged),
            "rating": f"{round(r, 1):g}" if r > 0 else None,
        }
        yard = m.get("yardFormation") or {}
        if ids is not None:                                      # ---- extras (appended after the old keys)
            ident = ids.get(m.get("id")) or {}
            player["id"] = ident.get("fd")
            player["s365_id"] = ident.get("athlete")
            det = (merged.get("formation") or {}).get("shortName") or (merged.get("position") or {}).get("shortName")
            if det:
                player["position_detail"] = det
            if m.get("status") == 1 and yard.get("fieldLine") is not None:
                player["pitch"] = {"line": yard.get("line"), "depth": yard.get("fieldLine"),
                                   "side": yard.get("fieldSide")}
            if m.get("hasStats") and m.get("stats"):
                st = _player_stats(m.get("stats"))
                if "minutes" in st:
                    player["minutes"] = st["minutes"]
                player["stats"] = {k: v for k, v in st.items() if k != "minutes"}
                hm = _heatmap(m)
                if hm:
                    player["heatmap"] = hm
                if m.get("hasShotChart"):
                    player["has_shot_chart"] = True
            sub = m.get("substitution") or {}
            if m.get("status") == 2 and sub.get("time") is not None:
                player["sub_on"] = int(round(float(sub["time"])))
                subs_info.append((sub.get("playerId"), player["sub_on"]))
            by_mid[m.get("id")] = player
        sort_key = (0 if player["position"] == "g" else 1,
                    yard.get("line") if yard.get("line") is not None else 99,
                    yard.get("fieldSide") if yard.get("fieldSide") is not None else 50)
        if m.get("status") == 1:
            starters.append((sort_key, player))
        else:
            bench.append(player)

    if coach is None:                                            # coach listed only in game.members
        for m in members.values():
            if m.get("competitorId") == comp.get("id") and _is_coach(m):
                coach = _pname(m)
                break

    starters.sort(key=lambda x: x[0])
    formation = lu.get("formation")
    out = {"formation": formation if isinstance(formation, str) and formation else None,
           "starting": [p for _, p in starters], "subs": bench, "coach": coach}
    if ids is not None:
        for out_mid, minute in subs_info:                        # who left the pitch, and when
            if out_mid in by_mid:
                by_mid[out_mid]["sub_off"] = minute
        ident = ids.get(coach_mid) or {}
        out["coach_id"] = ident.get("fd")
        out["coach_s365_id"] = ident.get("athlete")
        out["confirmed"] = lu.get("status") == "مؤكد"
        out["missing"] = missing
    return out


def _derived_score(events: list[dict]) -> dict:
    g = [e for e in events if e["type"] == "goal"]
    return {"home": sum(1 for e in g if e["team"] == "home"),
            "away": sum(1 for e in g if e["team"] == "away"),
            "ht_home": sum(1 for e in g if e["team"] == "home" and e["minute"] <= 45),
            "ht_away": sum(1 for e in g if e["team"] == "away" and e["minute"] <= 45)}


def _int(v) -> Optional[int]:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _game_score(game: dict, fd_home_365: int) -> dict:
    """Score from the koora game: full-time from the competitors, half-time from stage id 7."""
    hc, ac = game.get("homeCompetitor") or {}, game.get("awayCompetitor") or {}
    swapped = hc.get("id") != fd_home_365                        # fd home is koora's away side
    ft_h, ft_a = _int(hc.get("score")), _int(ac.get("score"))
    ht_h = ht_a = None
    for st in game.get("stages") or []:
        if st.get("id") == 7:                                    # first half
            ht_h, ht_a = _int(st.get("homeCompetitorScore")), _int(st.get("awayCompetitorScore"))
    if ft_h is not None and ft_h < 0:
        ft_h = None
    if ft_a is not None and ft_a < 0:
        ft_a = None
    if swapped:
        ft_h, ft_a, ht_h, ht_a = ft_a, ft_h, ht_a, ht_h
    return {"home": ft_h, "away": ft_a, "ht_home": ht_h, "ht_away": ht_a}


def build_entry(fid: str, code: str, fd: dict, ent: dict, raw_stats: dict, game: dict,
                resolver: Optional["PlayerResolver"] = None) -> dict:
    """One entry in the old stats.json format."""
    fd_home_365, fd_away_365 = ent["fd_home_s365_id"], ent["fd_away_s365_id"]
    comps = {c.get("id"): c for c in (game.get("homeCompetitor"), game.get("awayCompetitor")) if c}
    home_c, away_c = comps.get(fd_home_365, {}), comps.get(fd_away_365, {})

    ids: Optional[dict] = None
    if INCLUDE_EXTRAS and resolver is not None:                  # football-data player ids for everybody
        ids = resolver.resolve_game(game, {fd_home_365: ent.get("fd_home_id"), fd_away_365: ent.get("fd_away_id")})
    events, extra = convert_events(game, fd_home_365, fd_away_365, ids)
    derived = _derived_score(events)
    sc = fd.get("score") or {}
    ft, ht = sc.get("fullTime") or {}, sc.get("halfTime") or {}
    gs = _game_score(game, fd_home_365)                          # fallback: koora score + half-time stage
    pick = lambda a, b, c: a if a is not None else (b if b is not None else c)
    score = {"home": pick(ft.get("home"), gs["home"], derived["home"]),
             "away": pick(ft.get("away"), gs["away"], derived["away"]),
             "ht_home": pick(ht.get("home"), gs["ht_home"], derived["ht_home"]),
             "ht_away": pick(ht.get("away"), gs["ht_away"], derived["ht_away"])}
    _fix_own_goals(events, score)
    if fd.get("status") == "FINISHED":
        d = _derived_score(events)
        if (d["home"], d["away"]) != (score["home"], score["away"]):
            logger.warning("fd=%s: goal events (%d-%d) do not match the score (%s-%s) - check events",
                           fid, d["home"], d["away"], score["home"], score["away"])

    def team_name(c: dict, fd_team: dict) -> Optional[str]:
        slug = str(c.get("nameForURL") or "")
        if slug and _SLUG_OK.fullmatch(slug):                    # "manchester-city" -> "Manchester City"
            return _slug_name(slug, team=True)
        return fd_team.get("shortName") or fd_team.get("name")

    idx = index_stats(raw_stats, fd_home_365, fd_away_365)
    status = STATUS_TEXT.get(fd.get("status"), "live")
    entry = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source_url": f"{KOORA_API}/game/{ent['s365_id']}",
        "status": status,
        "home_team": team_name(home_c, fd.get("homeTeam") or {}),
        "away_team": team_name(away_c, fd.get("awayTeam") or {}),
        "score": score,
        "stats": old_stats(idx, score, extra),
        "events": events,
        "lineups": {"home": build_lineup(home_c, game, ids), "away": build_lineup(away_c, game, ids)},
        "match_id": int(fid),
        "fd_competition_code": code,
    }
    if INCLUDE_EXTRAS:
        entry["s365_id"] = ent["s365_id"]
        entry["recorded"] = status == FINAL_STATUS
        entry["all_stats"] = translate_stats(idx)
        if ids is not None:
            swapped = (game.get("homeCompetitor") or {}).get("id") != fd_home_365
            entry.update(build_game_extras(game, ids, swapped, entry["lineups"]))
        else:
            entry["schema"] = SCHEMA_VERSION
    return entry


# ══ Player identity: 365scores athleteId <-> football-data player id ═════════════════════════════
# football-data gives no lineups / goals on the free tier, so ids are matched by NAME inside the
# player's own squad (teams.json).  Position and shirt number are used only as tie-breakers.
# Every decision is stored in data/player_map_365.json, so a player is matched once, then reused.

_CHAR_FIX = str.maketrans({"ß": "ss", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ł": "l", "Ł": "l",
                           "æ": "ae", "Æ": "ae", "œ": "oe", "ı": "i", "þ": "th", "ð": "d"})


def _fold(s: str | None) -> str:
    """'Jørgen Strand Larsen' -> 'jorgen strand larsen' (lower-case, accents removed)."""
    s = unicodedata.normalize("NFKD", (s or "").translate(_CHAR_FIX))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _ptokens(name: str | None) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _fold(name)) if t]


def _tok_sim(t: str, tokens: list[str]) -> float:
    """Best similarity of token t against a token list; a >=3 letter prefix counts (wil ~ wilson)."""
    best = 0.0
    for u in tokens:
        if t == u:
            return 1.0
        if min(len(t), len(u)) >= 3 and (u.startswith(t) or t.startswith(u)):
            best = max(best, 0.9)
        else:
            best = max(best, SequenceMatcher(None, t, u).ratio())
    return best


def _name_score(a: list[str], b: list[str]) -> float:
    """0..1 similarity of two tokenised person names (order-insensitive, initials aware)."""
    if not a or not b:
        return 0.0
    if sorted(a) == sorted(b):
        return 1.0
    ini = [t for t in a if len(t) == 1]                       # "l. miller"
    core = [t for t in a if len(t) > 1]
    if ini and core:
        if all(_tok_sim(t, b) >= 0.88 for t in core):
            rest = [u for u in b if u not in core]
            if any(u[0] == i for i in ini for u in rest):
                return 0.9
            # initial differs ("o. adarabioyo" vs "tosin adarabioyo": 365 uses the legal first name)
            return 0.84 if sum(len(t) for t in core if t in b) >= 6 else 0.5
        a = core
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    if all(t in set(large) for t in small):                   # "gabriel magalhaes" in "gabriel dos santos magalhaes"
        if len(small) >= 2:
            return 0.92
        return 0.84 if len(small[0]) >= 4 else 0.7            # single-token names: kepa, jair, endrick
    shared = [t for t in a if t in b and len(t) >= 6]         # distinctive shared word (usually the surname)
    if shared:
        ra, rb = [t for t in a if t not in shared], [t for t in b if t not in shared]
        if ra and rb and any((len(x) >= 3 and (x in y or y in x)) or SequenceMatcher(None, x, y).ratio() >= 0.6
                             for x in ra for y in rb):
            return 0.84                                       # "valentino livramento" ~ "tino livramento"
    sims = [_tok_sim(t, large) for t in small]
    if min(sims) >= 0.82:
        return min(0.95, 0.55 + 0.4 * sum(sims) / len(sims))
    j = SequenceMatcher(None, "".join(sorted(a)), "".join(sorted(b))).ratio()
    return j * 0.93 if j >= 0.9 else j * 0.6


_FD_POS = {"goalkeeper": "g", "defence": "d", "defender": "d", "midfield": "m", "midfielder": "m",
           "offence": "f", "forward": "f", "attacker": "f"}


def _assign(cands: list[dict], pool: list[dict], used: set, threshold: float) -> dict:
    """
    cands: [{"key", "tokens", "pos", "num"}]   365 side
    pool : [{"id", "tokens", "pos", "num"}]    football-data side
    -> {key: (fd_id, score)}.  One-to-one, best pairs first, ambiguous candidates are skipped.
    """
    per: dict = defaultdict(list)
    for c in cands:
        for f in pool:
            if f["id"] in used:
                continue
            s = _name_score(c["tokens"], f["tokens"])
            if s <= 0:
                continue
            if c.get("pos") and f.get("pos") and c["pos"] != f["pos"]:
                s -= 0.25 if "g" in (c["pos"], f["pos"]) else 0.04     # a keeper is never an outfielder
            if c.get("num") is not None and c["num"] >= 0 and c["num"] == f.get("num"):
                s = min(1.0, s + 0.05)
            if s >= threshold:
                per[c["key"]].append((s, f["id"]))
    out: dict = {}
    taken: set = set()
    for lst in per.values():
        lst.sort(reverse=True)
    for key, lst in sorted(per.items(), key=lambda kv: -kv[1][0][0]):
        lst = [x for x in lst if x[1] not in taken]
        if not lst:
            continue
        if len(lst) > 1 and lst[0][0] - lst[1][0] < AMBIGUITY_GAP:
            continue                                            # ambiguous -> leave for a human
        out[key] = (lst[0][1], round(lst[0][0], 3))
        taken.add(lst[0][1])
    return out


class FdPool:
    """Football-data people of ONE competition: squads (teams.json), coaches, top scorers, lineups."""

    def __init__(self) -> None:
        self.by_team: dict[int, list[dict]] = {}
        self.coach: dict[int, dict] = {}
        self.names: dict[int, str] = {}
        self.exact: dict[tuple, list[dict]] = defaultdict(list)   # sorted name tokens -> people (all leagues/seasons)

    def _add(self, team_id, pid, name, pos=None, num=None) -> None:
        if not pid or not name or not team_id:
            return
        lst = self.by_team.setdefault(team_id, [])
        for e in lst:
            if e["id"] == pid:
                if num is not None:
                    e["num"] = num
                return
        lst.append({"id": pid, "name": name, "tokens": _ptokens(name),
                    "pos": _FD_POS.get(str(pos or "").lower()), "num": num})
        self.names[pid] = name

    @classmethod
    def load(cls, p: dict, matches: list[dict]) -> "FdPool":
        pool = cls()
        for t in _load_data(p["teams"], []) or []:
            tid = t.get("id")
            for pl in t.get("squad") or []:
                pool._add(tid, pl.get("id"), pl.get("name"), pl.get("position"), pl.get("shirtNumber"))
            c = t.get("coach") or {}
            if c.get("id") and c.get("name"):
                pool.coach[tid] = {"id": c["id"], "name": c["name"], "tokens": _ptokens(c["name"])}
        sc = _load_data(p.get("scorers", ""), {}) if p.get("scorers") else {}
        for row in (sc.get("scorers") or []) if isinstance(sc, dict) else []:
            pl, tm = row.get("player") or {}, row.get("team") or {}
            pool._add(tm.get("id"), pl.get("id"), pl.get("name"), pl.get("position"), pl.get("shirtNumber"))
        for m in matches:                                       # lineups, when the fd plan provides them
            for side in ("homeTeam", "awayTeam"):
                t = m.get(side) or {}
                for pl in (t.get("lineup") or []) + (t.get("bench") or []):
                    if pl:
                        pool._add(t.get("id"), pl.get("id"), pl.get("name"), pl.get("position"),
                                  pl.get("shirtNumber"))
                c = t.get("coach") or {}
                if c.get("id") and c.get("name") and t.get("id") not in pool.coach:
                    pool.coach[t["id"]] = {"id": c["id"], "name": c["name"], "tokens": _ptokens(c["name"])}
        pool._index_everyone()
        return pool

    def _index_everyone(self) -> None:
        """Index EVERY squad in data/**/teams.json (all leagues, all seasons) by exact name tokens.
        Used for people who are not in their own club's squad snapshot (transfers, loans, promoted clubs)."""
        seen: set = set()

        def add(pid, name, pos, tid) -> None:
            if not pid or not name or pid in seen:
                return
            seen.add(pid)
            self.names.setdefault(pid, name)
            self.exact[tuple(sorted(_ptokens(name)))].append(
                {"id": pid, "name": name, "tokens": _ptokens(name), "pos": _FD_POS.get(str(pos or "").lower()),
                 "num": None, "team": tid})

        for tid, lst in self.by_team.items():
            for e in lst:
                add(e["id"], e["name"], None, tid)
                self.exact[tuple(sorted(e["tokens"]))][-1]["pos"] = e["pos"]
        try:
            files = sorted(Path("data").rglob("teams.json"), reverse=True)     # newest season first
        except OSError:
            files = []
        for f in files:
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                for t in (raw.get("data", raw) if isinstance(raw, dict) else raw) or []:
                    for pl in t.get("squad") or []:
                        add(pl.get("id"), pl.get("name"), pl.get("position"), t.get("id"))
            except Exception as exc:                                           # one bad file must not stop the run
                logger.debug("pool: skip %s (%s)", f, exc)

    def others_for(self, cands: list[dict], team_id: int) -> list[dict]:
        """People from ANY other squad / league / season whose full name equals a candidate's name."""
        out, seen = [], set()
        for c in cands:
            for e in self.exact.get(tuple(sorted(c["tokens"])), []):
                if e["team"] != team_id and e["id"] not in seen:
                    seen.add(e["id"])
                    out.append(e)
        return out


class PlayerResolver:
    """Resolves every 365 member of a /game payload to a football-data player / coach id."""

    def __init__(self, pool: FdPool, store: dict, overrides: dict) -> None:
        self.pool, self.store, self.overrides = pool, store, overrides
        self.players: dict = store.setdefault("players", {})
        self.coaches: dict = store.setdefault("coaches", {})
        self.unmatched: dict = store.setdefault("unmatched", {})
        self.used: set = {v["fd_id"] for v in self.players.values()}
        self.used_coach: set = {v["fd_id"] for v in self.coaches.values()}
        self.dirty = False
        self.last: list[tuple] = []                  # (fd_id, athleteId, imageVersion) matched in the last game
        self.stats = Counter()

    # -- public ---------------------------------------------------------------------------------
    def resolve_game(self, game: dict, comp_to_fd_team: dict[int, int]) -> dict[int, dict]:
        """-> {member id: {"fd": fd id|None, "athlete": athleteId|None, "how": str, "name": english name}}"""
        members = [m for m in game.get("members") or [] if m and m.get("id") is not None]
        coach_mids = {m.get("id") for comp in (game.get("homeCompetitor"), game.get("awayCompetitor")) if comp
                      for m in (comp.get("lineups") or {}).get("members") or [] if m.get("status") == 4}
        coach_mids |= {m["id"] for m in members if _is_coach(m)}
        result: dict[int, dict] = {}
        self.last = []
        for comp_id, fd_team in comp_to_fd_team.items():
            mine = [m for m in members if m.get("competitorId") == comp_id]
            self._players([m for m in mine if m["id"] not in coach_mids], fd_team, result)
            self._coaches([m for m in mine if m["id"] in coach_mids], fd_team, result)
        for mid, r in result.items():
            if r["fd"] and r["athlete"]:
                self.last.append((r["fd"], r["athlete"], r.get("version")))
        return result

    def unmatched_summary(self, limit: int = 15) -> list[str]:
        rows = sorted(self.unmatched.items(), key=lambda kv: -kv[1].get("seen", 0))
        return [f"{k}: {v.get('name')} ({v.get('team')})" for k, v in rows[:limit]]

    # -- internals ------------------------------------------------------------------------------
    @staticmethod
    def _athlete(m: dict) -> Optional[int]:
        a = m.get("athleteId")
        return a if isinstance(a, int) and a > 0 else None

    def _record(self, table: dict, key: str, fd_id: int, m: dict, how: str, score: float, fd_team: int) -> None:
        table[key] = {"fd_id": fd_id, "name": _pname(m), "name_fd": self.pool.names.get(fd_id),
                      "how": how, "score": score, "team_fd_id": fd_team}
        self.unmatched.pop(key, None)
        self.dirty = True
        self.stats[how] += 1

    def _note_unmatched(self, key: str, m: dict, fd_team: int, coach: bool = False) -> None:
        row = self.unmatched.setdefault(key, {"seen": 0})
        row.update({"name": _pname(m), "name_ar": m.get("name"), "team_fd_id": fd_team,
                    "team": (self.pool.names.get(fd_team) or fd_team), "jersey": m.get("jerseyNumber"),
                    "pos": _pos_letter(m) if not coach else None, "coach": coach})
        row["seen"] += 1
        self.dirty = True

    def _players(self, members: list[dict], fd_team: int, result: dict) -> None:
        todo: list[dict] = []
        for m in members:
            ath, mid = self._athlete(m), m["id"]
            base = {"fd": None, "athlete": ath, "how": None, "name": _pname(m), "version": m.get("imageVersion")}
            result[mid] = base
            if ath is None:
                continue
            key = str(ath)
            if key in self.overrides:                           # manual decision wins
                fd = self.overrides[key]
                if fd:
                    base.update(fd=int(fd), how="manual")
                    if self.players.get(key, {}).get("fd_id") != int(fd):
                        self._record(self.players, key, int(fd), m, "manual", 1.0, fd_team)
                        self.used.add(int(fd))
                continue
            rec = self.players.get(key)
            if rec:
                base.update(fd=rec["fd_id"], how=rec.get("how"))
                continue
            todo.append(m)
        if not todo:
            return
        cands = [{"key": str(m["athleteId"]), "tokens": _ptokens(_pname(m)), "pos": _pos_letter(m),
                  "num": m.get("jerseyNumber")} for m in todo]
        found = _assign(cands, self.pool.by_team.get(fd_team, []), self.used, TEAM_MATCH_MIN)
        for k, (fd, sc) in found.items():
            self.used.add(fd)
        rest = [c for c in cands if c["key"] not in found]
        if rest and self.pool.exact:                            # transferred / loaned / other league or season
            far = _assign(rest, self.pool.others_for(rest, fd_team), self.used, LEAGUE_MATCH_MIN)
            for k, (fd, sc) in far.items():
                self.used.add(fd)
                found[k] = (fd, sc, "league")
        by_key = {str(m["athleteId"]): m for m in todo}
        for key, m in by_key.items():
            hit = found.get(key)
            if hit:
                how = hit[2] if len(hit) > 2 else "team"
                self._record(self.players, key, hit[0], m, how, hit[1], fd_team)
                result[m["id"]].update(fd=hit[0], how=how)
            else:
                self._note_unmatched(key, m, fd_team)
                self.stats["unmatched"] += 1

    def _coaches(self, members: list[dict], fd_team: int, result: dict) -> None:
        fd_coach = self.pool.coach.get(fd_team)
        for m in members:
            ath = self._athlete(m)
            base = {"fd": None, "athlete": ath, "how": None, "name": _pname(m), "version": m.get("imageVersion")}
            result[m["id"]] = base
            if ath is None:
                continue
            key = str(ath)
            if key in self.overrides and self.overrides[key]:
                base.update(fd=int(self.overrides[key]), how="manual")
                continue
            rec = self.coaches.get(key)
            if rec:
                base.update(fd=rec["fd_id"], how=rec.get("how"))
                continue
            if fd_coach and fd_coach["id"] not in self.used_coach and \
                    _name_score(_ptokens(_pname(m)), fd_coach["tokens"]) >= 0.8:
                self._record(self.coaches, key, fd_coach["id"], m, "coach",
                             round(_name_score(_ptokens(_pname(m)), fd_coach["tokens"]), 3), fd_team)
                self.used_coach.add(fd_coach["id"])
                base.update(fd=fd_coach["id"], how="coach")
            else:
                self._note_unmatched(key, m, fd_team, coach=True)

    def lookup(self, athlete_id, coach: bool = False) -> Optional[int]:
        """football-data id for a 365 athleteId (manual override > map), or None."""
        key = str(athlete_id)
        if self.overrides.get(key):
            return int(self.overrides[key])
        rec = (self.coaches if coach else self.players).get(key)
        return rec["fd_id"] if rec else None

    def rematch_unmatched(self) -> int:
        """Re-run matching for people stored as 'unmatched' (after you updated teams.json / overrides)."""
        n = 0
        by_team: dict = defaultdict(list)
        for key, row in list(self.unmatched.items()):
            if self.overrides.get(key):
                self._record(self.coaches if row.get("coach") else self.players, key, int(self.overrides[key]),
                             {"name": row.get("name")}, "manual", 1.0, row.get("team_fd_id"))
                n += 1
            else:
                by_team[row.get("team_fd_id")].append((key, row))
        for team, rows in by_team.items():
            people = [(k, r) for k, r in rows if not r.get("coach")]
            cands = [{"key": k, "tokens": _ptokens(r.get("name")), "pos": r.get("pos"), "num": r.get("jersey")}
                     for k, r in people]
            found = _assign(cands, self.pool.by_team.get(team, []), self.used, TEAM_MATCH_MIN)
            for k in found:
                self.used.add(found[k][0])
            rest = [c for c in cands if c["key"] not in found]
            if rest and self.pool.exact:
                for k, (fd, sc) in _assign(rest, self.pool.others_for(rest, team), self.used, LEAGUE_MATCH_MIN).items():
                    self.used.add(fd)
                    found[k] = (fd, sc, "league")
            for k, hit in found.items():
                row = self.unmatched[k]
                self._record(self.players, k, hit[0], {"name": row.get("name")},
                             hit[2] if len(hit) > 2 else "team", hit[1], team)
                n += 1
            fd_coach = self.pool.coach.get(team)
            for k, r in rows:
                if r.get("coach") and fd_coach and fd_coach["id"] not in self.used_coach and \
                        _name_score(_ptokens(r.get("name")), fd_coach["tokens"]) >= 0.8:
                    self._record(self.coaches, k, fd_coach["id"], {"name": r.get("name")}, "coach", 1.0, team)
                    self.used_coach.add(fd_coach["id"])
                    n += 1
        return n

    def save(self) -> None:
        if self.dirty:
            safe_write(PLAYER_MAP_PATH, self.store)
            self.dirty = False


def load_resolver(p: dict, matches: list[dict]) -> PlayerResolver:
    store = _load_data(PLAYER_MAP_PATH, {})
    overrides = {str(k): v for k, v in _read_json(PLAYER_OVERRIDES_PATH).items()
                 if not str(k).startswith("_")}
    return PlayerResolver(FdPool.load(p, matches), store, overrides)


# ══ Headshots: public/headshot/{football-data id}.png ═══════════════════════════════════════════

class Headshots:
    """Downloads each 365 athlete photo once and saves it under the football-data id."""

    def __init__(self, directory: str = HEADSHOT_DIR, manifest: str = HEADSHOT_MANIFEST) -> None:
        self.dir, self.manifest_path = Path(directory), manifest
        self.data: dict = _load_data(manifest, {})
        self.files: dict = self.data.setdefault("files", {})          # fd id -> {athlete_id, version, sha1}
        self.no_photo: dict = self.data.setdefault("no_photo", {})    # athleteId -> {ts, version}
        self._default: Optional[str] = None
        self.dirty = False
        self.counts = Counter()

    def _url(self, athlete_id) -> str:
        return HEADSHOT_URL.replace("{id}", str(athlete_id))

    def _default_hash(self) -> str:
        """sha1 of the 365 'default silhouette' (what an unknown athlete id returns)."""
        if self._default is None:
            data, status = _get_bytes(self._url(0), retries=2)
            self._default = hashlib.sha1(data).hexdigest() if data else ""
            logger.info("headshots: default-silhouette fingerprint %s (http %s)",
                        self._default[:10] or "unavailable", status)
        return self._default

    def ensure(self, fd_id: int, athlete_id: int, version: Optional[int] = None) -> str:
        """-> exists | downloaded | no_photo | error"""
        if not fd_id or not athlete_id:
            return "error"
        dest = self.dir / f"{fd_id}.png"
        rec = self.files.get(str(fd_id))
        if dest.exists():
            if rec is None:                                    # a file you already had: adopt it
                self.files[str(fd_id)] = {"athlete_id": athlete_id, "version": version, "adopted": True}
                self.dirty = True
                self.counts["exists"] += 1
                return "exists"
            if rec.get("athlete_id") == athlete_id and (
                    version is None or rec.get("version") is None or version <= rec["version"]):
                self.counts["exists"] += 1
                return "exists"
        np_rec = self.no_photo.get(str(athlete_id))
        if np_rec:
            age = datetime.now(timezone.utc) - (_dt(np_rec.get("ts")) or datetime.now(timezone.utc))
            newer = version is not None and np_rec.get("version") is not None and version > np_rec["version"]
            if age < timedelta(days=NO_PHOTO_RETRY_DAYS) and not newer:
                return "no_photo"
        data, status = _get_bytes(self._url(athlete_id))
        if data is None:
            if status in (404, 410):
                self.no_photo[str(athlete_id)] = {"ts": datetime.now(timezone.utc).isoformat(), "version": version}
                self.dirty = True
                self.counts["no_photo"] += 1
                return "no_photo"
            self.counts["error"] += 1
            return "error"                                      # network problem: retry next run
        if not data.startswith(b"\x89PNG"):
            logger.warning("headshot %s: not a PNG (%d bytes) - skipped", athlete_id, len(data))
            self.counts["error"] += 1
            return "error"
        sha = hashlib.sha1(data).hexdigest()
        if sha == self._default_hash():
            self.no_photo[str(athlete_id)] = {"ts": datetime.now(timezone.utc).isoformat(), "version": version}
            self.dirty = True
            self.counts["no_photo"] += 1
            return "no_photo"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".png.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        self.files[str(fd_id)] = {"athlete_id": athlete_id, "version": version, "sha1": sha,
                                  "bytes": len(data), "downloaded_at": datetime.now(timezone.utc).isoformat()}
        self.no_photo.pop(str(athlete_id), None)
        self.dirty = True
        self.counts["downloaded"] += 1
        if self.counts["downloaded"] % 25 == 0:
            self.flush()
        return "downloaded"

    def ensure_many(self, rows: list[tuple]) -> None:
        for fd_id, athlete_id, version in rows:
            if _out_of_time():
                return
            self.ensure(fd_id, athlete_id, version)

    def flush(self) -> None:
        if self.dirty:
            safe_write(self.manifest_path, self.data)
            self.dirty = False

    def summary(self) -> str:
        c = self.counts
        return (f"headshots: downloaded={c['downloaded']} already={c['exists']} "
                f"no-photo={c['no_photo']} errors={c['error']}")


def backfill_images(heads: Headshots) -> None:
    """--mode images: every player / coach already matched in player_map_365.json gets a headshot."""
    store = _load_data(PLAYER_MAP_PATH, {})
    rows = [(rec["fd_id"], int(k), None) for table in ("players", "coaches")
            for k, rec in (store.get(table) or {}).items()]
    missing = [r for r in rows if not (heads.dir / f"{r[0]}.png").exists()]
    logger.info("images: %d matched people, %d headshots missing", len(rows), len(missing))
    heads.ensure_many(missing)
    heads.flush()
    logger.info(heads.summary())


# ══ Per-player stats (365 stat type id -> English key) ═════════════════════════════════════════
# Ids/Arabic names were read from a real La Liga game payload (Real Madrid - Sevilla).  An id that
# is not listed here is kept as "stat_<id>" with its Arabic name and logged once.
# (59 and 85 are the least certain translations - check them against 365scores' own labels.)

PLAYER_STATS: dict[int, tuple[str, str]] = {
    30: ("minutes",                  "Minutes Played"),
    27: ("goals",                    "Goals"),
    26: ("assists",                  "Assists"),
    76: ("xg",                       "Expected Goals (xG)"),
    79: ("xgot",                     "Expected Goals on Target (xGOT)"),
    78: ("xa",                       "Expected Assists (xA)"),
    3:  ("shots",                    "Total Shots"),
    4:  ("shots_on_target",          "Shots on Target"),
    5:  ("shots_off_target",         "Shots off Target"),
    6:  ("shots_blocked",            "Shots Blocked"),
    9:  ("offsides",                 "Offsides"),
    47: ("penalties_won",            "Penalties Won"),
    36: ("big_chances_missed",       "Big Chances Missed"),
    87: ("big_chances_scored",       "Big Chances Scored"),
    24: ("dangerous_chances",        "Dangerous Chances"),
    46: ("chances_created",          "Chances Created"),
    19: ("passes",                   "Accurate Passes"),
    80: ("passes_final_third",       "Passes into Final Third"),
    81: ("back_passes",              "Back Passes"),
    53: ("long_passes",              "Completed Long Passes"),
    52: ("crosses",                  "Completed Crosses"),
    54: ("dribbles",                 "Completed Dribbles"),
    45: ("touches",                  "Touches"),
    84: ("final_third_possession",   "Final Third Possession Won"),
    73: ("possession_lost",          "Possession Lost"),
    37: ("fouls_won",                "Fouls Won"),
    42: ("fouls_committed",          "Fouls Committed"),
    60: ("dribbled_past",            "Dribbled Past"),
    39: ("tackles_won",              "Successful Tackles"),
    41: ("interceptions",            "Interceptions"),
    40: ("clearances",               "Clearances"),
    86: ("recoveries",               "Ball Recoveries"),
    85: ("defensive_actions",        "Defensive Actions"),
    59: ("blocks",                   "Blocks"),
    56: ("aerial_duels_won",         "Aerial Duels Won"),
    55: ("ground_duels_won",         "Ground Duels Won"),
    66: ("errors_leading_to_goal",   "Errors Leading to Goal"),
    65: ("errors_leading_to_shot",   "Errors Leading to Shot"),
    48: ("penalties_conceded",       "Penalties Conceded"),
    23: ("saves",                    "Goalkeeper Saves"),
    35: ("goals_conceded",           "Goals Conceded"),
    82: ("xgot_faced",               "xGOT Faced"),
    83: ("goals_prevented",          "Goals Prevented"),
    43: ("punches",                  "Punches"),
    44: ("penalties_saved",          "Penalties Saved"),
}
_KEEP_ZERO = {"minutes"}
_LOGGED_PLAYER_UNKNOWN: set = set()
_RATIO = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*(?:\(\s*(-?\d+(?:\.\d+)?)\s*%\s*\))?\s*$")
_PEN_GOALS = re.compile(r"^\s*(\d+)\s*\(\s*(\d+)")


def _pval(raw):
    """'36/38 (95%)' -> {made,total,pct}; '90'' -> 90; '1.8' -> 1.8; '2(1 pen)' -> (2, penalty goals 1)."""
    s = str(raw if raw is not None else "").strip().rstrip("'").strip()
    m = _RATIO.match(s)
    if m:
        made, total = int(m.group(1)), int(m.group(2))
        pct = float(m.group(3)) if m.group(3) is not None else (made / total * 100 if total else 0.0)
        return {"made": made, "total": total, "pct": int(pct) if float(pct).is_integer() else round(pct, 1)}
    m = _PEN_GOALS.match(s)
    if m:
        return int(m.group(1)), int(m.group(2))
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return s


def _player_stats(rows: list[dict] | None) -> dict:
    """365 per-player stat rows -> {english key: value}.  Zero values are omitted (like the old
    team stats); 'minutes' always stays.  Ratio stats become {made,total,pct}."""
    out: dict = {}
    for r in rows or []:
        sid = r.get("type")
        key, _en = PLAYER_STATS.get(sid, (f"stat_{sid}", None))
        if sid not in PLAYER_STATS and sid not in _LOGGED_PLAYER_UNKNOWN:
            _LOGGED_PLAYER_UNKNOWN.add(sid)
            logger.info("untranslated PLAYER stat id %s (Arabic name: %s) - add it to PLAYER_STATS",
                        sid, r.get("name"))
        val = _pval(r.get("value"))
        pens = 0
        if isinstance(val, tuple):                                # goals "2(1 pen)"
            val, pens = val
        if isinstance(val, dict):
            if val["total"] == 0:
                continue
        elif val == 0 and key not in _KEEP_ZERO:
            continue
        out[key] = val
        if pens:
            out["penalty_goals"] = pens
    return out


# ══ Other pieces of the /game payload ══════════════════════════════════════════════════════════

_INJURY_RETURN = {"عاد إلى تدريبات": "Back in training"}
_SUSPENSION = {1: "Red card"}
_BODY_PART = {"القدم اليمنى": "right_foot", "القدم اليسرى": "left_foot", "رأسية": "head"}
_SHOT_SITUATION = {"هجمة مرتدة": "counter_attack", "من الركنية": "corner", "لعب مفتوح": "open_play",
                   "صناعة": "assisted", "ركلة جزاء": "penalty"}
_SHOT_OUTCOME = {0: "goal", 1: "off_target", 2: "saved", 4: "blocked"}
_TOP_CATEGORY = {"الهجوم": "attack", "الوسط": "midfield", "الدفاع": "defence"}
_PAREN = re.compile(r"\((\d+)(?:/(\d+))?\)")
_MMSS = re.compile(r"(\d+):(\d{2})")


def _heatmap(m: dict) -> Optional[str]:
    h = m.get("heatMap")
    return h if isinstance(h, str) and h.startswith("http") else None


def _missing_player(m: dict, info: dict, ident: dict) -> dict:
    """Injured / suspended squad member (lineup status 3)."""
    row: dict = {"id": ident.get("fd"), "s365_id": ident.get("athlete"),
                 "name": _pname(info) or _pname(m), "position": _pos_letter({**info, **m})}
    inj, sus = m.get("injury"), m.get("suspension")
    if inj:
        code = str(inj.get("imageId") or "")
        row["reason"] = "injury"
        row["detail"] = code.replace("_", " ").title() if code else inj.get("reason")
        ret = inj.get("expectedReturn")
        if ret:
            row["expected_return"] = _INJURY_RETURN.get(ret, ret)
    elif sus:
        row["reason"] = "suspension"
        row["detail"] = _SUSPENSION.get(sus.get("id"), sus.get("name"))
    else:
        row["reason"] = "unavailable"
    season: dict = {}
    for st in m.get("seasonStats") or []:
        mm = _PAREN.search(str(st.get("text") or ""))
        if not mm:
            continue
        if st.get("type") == 5:
            season["appearances"] = int(mm.group(1))
            if mm.group(2):
                season["matches"] = int(mm.group(2))
        elif st.get("type") == 1:
            season["goals"] = int(mm.group(1))
        elif st.get("type") == 2:
            season["assists"] = int(mm.group(1))
    if season:
        row["season"] = season
    return row


def _shot_minute(t: str | None) -> tuple[int, int]:
    mm = re.match(r"\s*(\d+)\s*(?:\+\s*(\d+))?", str(t or ""))
    return (int(mm.group(1)), int(mm.group(2) or 0)) if mm else (0, 0)


def build_shots(game: dict, ids: dict, swapped: bool) -> list[dict]:
    """Shot map (chartEvents) with xG / xGOT / coordinates, oriented to football-data home/away.
    x = distance towards the goal being attacked (0-100, a penalty is ~88), y = lateral (0-100)."""
    chart = game.get("chartEvents") or {}
    sub_names = {s.get("value"): _SHOT_SITUATION.get(s.get("name"), f"subtype_{s.get('value')}")
                 for s in chart.get("eventSubTypes") or []}
    members = {m.get("id"): m for m in game.get("members") or [] if m}
    out: list[dict] = []
    for e in chart.get("events") or []:
        num = e.get("competitorNum")
        if num not in (1, 2):
            continue
        minute, added = _shot_minute(e.get("time"))
        mem = members.get(e.get("playerId")) or {}
        ident = ids.get(e.get("playerId")) or {}
        oc = e.get("outcome") or {}
        sh: dict = {"minute": minute}
        if added:
            sh["added_time"] = added
        sh["team"] = ("away" if num == 1 else "home") if swapped else ("home" if num == 1 else "away")
        sh["player"] = _pname(mem)
        if ident.get("fd"):
            sh["player_id"] = ident["fd"]
        if ident.get("athlete"):
            sh["player_s365_id"] = ident["athlete"]
        for src, dst in (("xg", "xg"), ("xgot", "xgot")):
            try:
                sh[dst] = round(float(e.get(src)), 2)
            except (TypeError, ValueError):
                pass
        sh["outcome"] = _SHOT_OUTCOME.get(oc.get("id"), f"outcome_{oc.get('id')}")
        bp = e.get("bodyPart")
        if bp and bp != "-":
            sh["body_part"] = _BODY_PART.get(bp, bp)
        if e.get("subType") is not None:
            sh["situation"] = sub_names.get(e["subType"], f"subtype_{e['subType']}")
        if e.get("side") is not None and e.get("line") is not None:
            sh["x"], sh["y"] = e["side"], e["line"]
        end = {k: oc[k] for k in ("x", "y", "z") if oc.get(k) is not None}
        if end:
            sh["end"] = end                                      # goal-frame (y,z) or block spot (x,y)
        out.append(sh)
    return out


def _potm(lineups: dict) -> Optional[dict]:
    best, row = 0.0, None
    for side in ("home", "away"):
        for p in (lineups.get(side) or {}).get("starting", []) + (lineups.get(side) or {}).get("subs", []):
            try:
                r = float(p.get("rating"))
            except (TypeError, ValueError):
                continue
            if r > best:
                best, row = r, {"team": side, "id": p.get("id"), "s365_id": p.get("s365_id"),
                                "name": p.get("name"), "rating": p.get("rating")}
    return row


def build_game_extras(game: dict, ids: dict, swapped: bool, lineups: dict) -> dict:
    """Everything else worth keeping from /game (all keys optional)."""
    ex: dict = {"schema": SCHEMA_VERSION}
    v = game.get("venue") or {}
    if v:
        slug = _fold(str(v.get("shortName") or ""))
        ex["venue"] = {"name": _slug_name(slug) if slug and _SLUG_OK.fullmatch(slug) else v.get("name"),
                       "capacity": v.get("capacity"), "attendance": v.get("attendance")}
        ex["venue"] = {k: x for k, x in ex["venue"].items() if x}
    off = [o for o in game.get("officials") or [] if o]
    if off:
        nm = str(off[0].get("name") or "")
        ex["referee"] = nm if nm.isascii() and nm else _pname(off[0])
    potm = _potm(lineups)
    if potm:
        ex["player_of_the_match"] = potm
    apt = game.get("actualPlayTime") or {}
    a = _MMSS.search(str((apt.get("actualTime") or {}).get("name") or ""))
    t = _MMSS.search(str((apt.get("totalTime") or {}).get("name") or ""))
    if a and t:
        ea, tt = int(a.group(1)) * 60 + int(a.group(2)), int(t.group(1)) * 60 + int(t.group(2))
        ex["play_time"] = {"effective": f"{a.group(1)}:{a.group(2)}", "total": f"{t.group(1)}:{t.group(2)}",
                           "effective_pct": round(ea / tt * 100, 1) if tt else None}
    pred: dict = {}
    for pr in (game.get("promotedPredictions") or {}).get("predictions") or []:
        opts = {o.get("num"): (o.get("vote") or {}).get("percentage") for o in pr.get("options") or []}
        if pr.get("type") == 1:                                  # who wins (1 = koora home, 2 = draw, 3 = away)
            h_, d_, a_ = opts.get(1), opts.get(2), opts.get(3)
            if swapped:
                h_, a_ = a_, h_
            pred["result"] = {"home": h_, "draw": d_, "away": a_, "votes": pr.get("totalVotes")}
        elif pr.get("type") == 3:                                # total goals over / under
            mm = re.search(r"\(([\d.]+)\)", str(pr.get("title") or ""))
            pred["total_goals"] = {"line": float(mm.group(1)) if mm else None,
                                   "under": opts.get(1), "over": opts.get(2), "votes": pr.get("totalVotes")}
    if pred:
        ex["predictions"] = pred
    tops: dict = {}
    for i, cat in enumerate((game.get("topPerformers") or {}).get("categories") or []):
        row: dict = {}
        for side_key, side in (("homePlayer", "home"), ("awayPlayer", "away")):
            pl = cat.get(side_key)
            if not pl:
                continue
            ident = ids.get(pl.get("id")) or {}
            out_side = ({"home": "away", "away": "home"}[side]) if swapped else side
            row[out_side] = {"id": ident.get("fd"), "s365_id": pl.get("athleteId") or ident.get("athlete"),
                             "name": _pname(pl),
                             "stats": _player_stats(pl.get("stats"))}
        if row:
            tops[_TOP_CATEGORY.get(cat.get("name"), f"category_{i + 1}")] = row
    if tops:
        ex["top_performers"] = tops
    vid = (game.get("video") or {}).get("url")
    if vid:
        ex["video_url"] = vid
    shots = build_shots(game, ids, swapped)
    if shots:
        ex["shots"] = shots
    return ex


# ══ Local clean-up tools (no network) ══════════════════════════════════════════════════════════

_ID_PAIRS = (("id", "s365_id"), ("player_id", "player_s365_id"), ("assistant_id", "assistant_s365_id"),
             ("player_out_id", "player_out_s365_id"), ("coach_id", "coach_s365_id"))


def remap_entry(entry: dict, resolver: "PlayerResolver") -> int:
    """Fill every missing football-data id in an entry from player_map_365.json.  Returns #ids added."""
    added = 0

    def walk(o) -> None:
        nonlocal added
        if isinstance(o, dict):
            for id_key, s_key in _ID_PAIRS:
                ath = o.get(s_key)
                if ath and not o.get(id_key):
                    fd = resolver.lookup(ath, coach=(id_key == "coach_id"))
                    if fd:
                        o[id_key] = fd
                        added += 1
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(entry)
    return added


def remap_ids(code: str, season_str: Optional[str], heads: Optional["Headshots"]) -> None:
    """--mode remap: re-match unmatched people, then push the ids into the existing stats.json."""
    p = _paths(code, season_str)
    matches = _load_data(p["matches"], [])
    store: dict = _load_data(p["stats"], {})
    if not store:
        logger.warning("%s: no stats.json to remap (%s)", code, p["stats"])
        return
    resolver = load_resolver(p, matches)
    newly = resolver.rematch_unmatched()
    added = sum(remap_entry(e, resolver) for e in store.values())
    resolver.save()
    if added:
        _write_stats(p["stats"], code, season_str, store)
    logger.info("%s: remap - %d people newly matched, %d ids filled into %d entries; %d people still unmatched",
                code, newly, added, len(store), len(resolver.unmatched))
    if heads is not None and newly:
        backfill_images(heads)


def _split_details(entry: dict, fid: str) -> dict:
    """--split-details: move the heavy parts (per-player stats + heat maps, shot map) out of the entry.
    Detail file players are keyed by the 365 athleteId (= lineup player's s365_id) - it never changes."""
    players: dict = {}
    for side in ("home", "away"):
        lu = entry["lineups"][side]
        for pl in lu["starting"] + lu["subs"]:
            blob = {k: pl.pop(k) for k in ("stats", "heatmap") if k in pl}
            if blob and pl.get("s365_id"):
                players[str(pl["s365_id"])] = {"side": side, **blob}
    detail: dict = {"match_id": int(fid), "players": players}
    if "shots" in entry:
        detail["shots"] = entry.pop("shots")
    entry["details"] = f"match_details/{fid}.json"
    return detail


def _write_details(root: str, fid: str, detail: dict) -> None:
    path = Path(root) / "match_details" / f"{fid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"_meta": {"last_synced": datetime.now(timezone.utc).isoformat()}, "data": detail},
                  fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


# ── stats.json read/write (same _meta as the old file) ───────────────────────

def _write_stats(path: str, code: str, season_str: Optional[str], store: dict) -> None:
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {"last_synced": datetime.now(timezone.utc).isoformat(),
                  "competition": code, "season": season_str, "total_entries": len(store)},
        "data": store,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    logger.info("Wrote %s (%d entries)", path, len(store))


def _is_recorded(entry: Optional[dict]) -> bool:
    return bool(entry) and entry.get("status") == FINAL_STATUS


def fetch_stats(code: str, season_str: Optional[str], retry_unavailable: bool = False,
                rebuild: bool = False, heads: Optional[Headshots] = None, upgrade: bool = True,
                split_details: bool = False) -> None:
    p = _paths(code, season_str)
    matches = {str(m["id"]): m for m in _load_data(p["matches"], []) if m.get("id")}
    mp: dict = _load_data(p["map"], {})
    mapping: dict = mp.get("matches", {})
    store: dict = _load_data(p["stats"], {})
    now = datetime.now(timezone.utc)
    resolver = load_resolver(p, list(matches.values())) if INCLUDE_EXTRAS else None

    if retry_unavailable:
        for ent in mapping.values():
            ent.pop("stats_status", None)
            ent.pop("stats_tries", None)

    def need(fid: str) -> bool:
        ent, m = mapping[fid], matches[fid]
        if ent.get("stats_status") == "unavailable":
            return False
        st = m.get("status")
        if st in LIVE_STATUSES:
            return True                                    # live: refresh every run
        if st == "FINISHED":
            if rebuild or not _is_recorded(store.get(fid)):
                return True
            # recorded: never again - except entries from an older script version (no player ids yet)
            return bool(upgrade and INCLUDE_EXTRAS and store[fid].get("schema", 1) < SCHEMA_VERSION)
        d = _dt(m.get("utcDate"))                          # fd status can lag: try just-started games
        return bool(st in SCHEDULED_STATUSES and d and timedelta(0) <= now - d <= timedelta(hours=3)
                    and not _is_recorded(store.get(fid)))

    todo = [fid for fid, ent in mapping.items()
            if fid in matches and ent.get("fd_home_s365_id") and need(fid)]
    n_up = sum(1 for fid in todo if _is_recorded(store.get(fid)) and not rebuild)
    logger.info("%s: %d matches need stats%s", code, len(todo),
                f" ({n_up} recorded ones are upgraded to schema {SCHEMA_VERSION} once)" if n_up else "")

    new = since_save = 0
    for fid in todo:
        if _out_of_time():
            logger.warning("%s: time budget reached - progress saved, rerun to continue", code)
            break
        ent, m = mapping[fid], matches[fid]
        raw = _get_json(f"{KOORA_API}/stats/{ent['s365_id']}")
        game = fetch_game(ent["s365_id"]) if raw and raw.get("statistics") else None
        if not game:                                       # no stats yet, or game details unavailable
            if m.get("status") == "FINISHED":              # count failures; give up on old matches
                ent["stats_tries"] = ent.get("stats_tries", 0) + 1
                age = now - (_dt(m.get("utcDate")) or now)
                if ent["stats_tries"] >= MAX_STATS_TRIES and age > timedelta(days=GIVE_UP_AFTER_DAYS):
                    ent["stats_status"] = "unavailable"
                    logger.warning("%s: no stats for fd=%s after %d tries - marked unavailable",
                                   code, fid, ent["stats_tries"])
            else:
                logger.info("%s: no stats yet for fd=%s (365=%s)", code, fid, ent["s365_id"])
            continue

        entry = build_entry(fid, code, m, ent, raw, game, resolver)
        if not entry["stats"]:
            logger.warning("%s: fd=%s: stats had no rows for the mapped team ids (check mapping)", code, fid)
            continue
        if split_details and INCLUDE_EXTRAS and resolver is not None:
            _write_details(p["root"], fid, _split_details(entry, fid))
        store[fid] = entry
        ent.pop("stats_tries", None)
        if resolver is not None and heads is not None:     # headshots of everybody in this game
            heads.ensure_many(resolver.last)
        new += 1
        since_save += 1
        if since_save >= CHECKPOINT_MATCHES:               # checkpoint on long runs
            _write_stats(p["stats"], code, season_str, store)
            safe_write(p["map"], mp)
            if resolver is not None:
                resolver.save()
            if heads is not None:
                heads.flush()
            since_save = 0

    if new:
        _write_stats(p["stats"], code, season_str, store)
    if resolver is not None:
        resolver.save()
        c = resolver.stats
        logger.info("%s: player ids - new matches by squad=%d, other squad=%d, manual=%d, coaches=%d, "
                    "UNMATCHED=%d", code, c["team"], c["league"], c["manual"], c["coach"], c["unmatched"])
        if resolver.unmatched:
            logger.warning("%s: %d people still have no football-data id (see 'unmatched' in %s; fix with %s). "
                           "Most seen: %s", code, len(resolver.unmatched), PLAYER_MAP_PATH,
                           PLAYER_OVERRIDES_PATH, "; ".join(resolver.unmatched_summary(8)))
    if heads is not None:
        heads.flush()
    if new or any("stats_tries" in e or "stats_status" in e for e in mapping.values()):
        safe_write(p["map"], mp)
    rec = sum(1 for e in store.values() if _is_recorded(e))
    logger.info("%s: stored %d new/updated (full time=%d, live=%d, total=%d)",
                code, new, rec, len(store) - rec, len(store))
    _RUN_NOTES.append({"code": code, "new": new, "full_time": rec, "live": len(store) - rec,
                       "unmatched": len(resolver.unmatched) if resolver is not None else 0})


# ── Debug: why did one match not map? ─────────────────────────────────────────

def debug_match(code: str, season_str: Optional[str], comp_id: Optional[int], fd_id: int) -> None:
    p = _paths(code, season_str)
    fd = next((m for m in _load_data(p["matches"], []) if m.get("id") == fd_id), None)
    if not fd:
        print(f"fd match {fd_id} not found in {p['matches']}")
        return
    d = _dt(fd["utcDate"])
    h, a = fd.get("homeTeam") or {}, fd.get("awayTeam") or {}
    print(f"\nfootball-data: {fd_id}  {fd['utcDate']}  {h.get('name')} ({h.get('shortName')}) vs "
          f"{a.get('name')} ({a.get('shortName')})")
    print("normalised fd names:", _names_fd(h), "|", _names_fd(a))

    games = fetch_365_games(d - timedelta(days=1), d + timedelta(days=1), comp_id) or []
    print(f"\n365scores returned {len(games)} games for {d.date()} +/- 1 day")
    print("competitionId counts (top 8):", Counter(g.get("competitionId") for g in games).most_common(8))

    keys = {n[:4] for n in _names_fd(h) | _names_fd(a) if len(n) >= 4}
    print("\nGames within +/-24h whose team names resemble either team:")
    found = 0
    for g in games:
        hc, ac = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
        blob = " ".join(_names_365(hc) | _names_365(ac))
        if any(k in blob for k in keys):
            found += 1
            print(f"  id={g.get('id')} start={g.get('startTime')} comp={g.get('competitionId')} "
                  f"| {hc.get('name')} ({hc.get('nameForURL')}) vs {ac.get('name')} ({ac.get('nameForURL')})")
    if not found:
        print("  (none) -> the game is not in the response at all (filter/pagination/date issue)")


# ── Runner / CLI ──────────────────────────────────────────────────────────────

def _write_step_summary(heads: Optional["Headshots"], failures: list[str]) -> None:
    """GitHub Actions: a small table on the run's summary page (no-op elsewhere)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not _RUN_NOTES:
        return
    lines = ["### stats365", "", "| league | new / updated | full time | live | unmatched people |",
             "|---|---:|---:|---:|---:|"]
    for r in _RUN_NOTES:
        lines.append(f"| {r['code']} | {r['new']} | {r['full_time']} | {r['live']} | {r['unmatched']} |")
    if heads is not None and sum(heads.counts.values()):
        lines += ["", heads.summary()]
    if _out_of_time():
        lines += ["", "> time budget reached - the next run continues where this one stopped"]
    if failures:
        lines += ["", f"> leagues with errors: {', '.join(failures)}"]
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def run(competition: Optional[str] = None, season: Optional[int] = None, mode: str = "all",
        comp365: Optional[int] = None, debug: Optional[int] = None,
        horizon_days: Optional[int] = None, max_minutes: Optional[float] = None,
        retry_unavailable: bool = False, strict: bool = False,
        include_extras: bool = True, rebuild: bool = False, images: bool = True,
        upgrade: bool = True, split_details: bool = False) -> int:
    """Returns the number of leagues that raised an error."""
    global _DEADLINE, INCLUDE_EXTRAS
    INCLUDE_EXTRAS = include_extras
    _DEADLINE = time.monotonic() + max_minutes * 60 if max_minutes else None

    start_year = season if season else current_season_start_year()
    sstr_league = season_label(start_year)
    logger.info("Season %s (today %s UTC, season starts month %d)",
                sstr_league, datetime.now(timezone.utc).date(), SEASON_START_MONTH)

    codes = [competition.upper()] if competition else \
        [c for c in LEAGUE_COMPETITIONS if not is_tournament(c)]

    heads: Optional[Headshots] = Headshots() if (include_extras and images) else None
    if mode == "images":
        if heads is None:
            heads = Headshots()
        backfill_images(heads)
        return 0

    if mode == "remap":
        for code in codes:
            try:
                remap_ids(code, None if is_tournament(code) else sstr_league, heads)
            except Exception:
                logger.exception("%s: remap failed", code)
        if heads is not None:
            heads.flush()
        return 0

    failures: list[str] = []
    for code in codes:
        if _out_of_time():
            logger.warning("time budget reached - skipping remaining leagues from %s", code)
            break
        sstr = None if is_tournament(code) else sstr_league
        cid = (comp365 if competition else None) or S365_COMPETITION.get(code)
        try:
            if debug:
                debug_match(code, sstr, cid, debug)
                return 0
            if mode in ("all", "map"):
                build_map(code, sstr, cid, horizon_days)
            if mode in ("all", "stats"):
                fetch_stats(code, sstr, retry_unavailable, rebuild, heads, upgrade, split_details)
        except Exception:
            logger.exception("%s: failed - continuing with the next league", code)
            failures.append(code)

    if heads is not None:
        heads.flush()
        if sum(heads.counts.values()):
            logger.info(heads.summary())
    _write_step_summary(heads, failures)
    if failures:
        logger.error("leagues with errors: %s", ", ".join(failures))
        if strict:
            sys.exit(1)
    return len(failures)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Map match ids to 365scores and store translated stats.")
    ap.add_argument("--competition", metavar="CODE", help="one code, e.g. SA (default: all leagues)")
    ap.add_argument("--season", type=int, help="season start year (default: from today's date)")
    ap.add_argument("--mode", choices=["all", "map", "stats", "images", "remap"], default="all")
    ap.add_argument("--comp365", type=int, help="365scores competitionId (with --competition)")
    ap.add_argument("--debug", type=int, metavar="FD_ID", help="explain why one match id is not mapping")
    ap.add_argument("--horizon-days", type=int, default=None,
                    help="only map scheduled matches within N days (default: whole season)")
    ap.add_argument("--max-minutes", type=float, default=None,
                    help="stop gracefully after N minutes (saves progress) - use in CI")
    ap.add_argument("--retry-unavailable", action="store_true",
                    help="clear the 'unavailable' marker and retry those matches")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-fetch matches already stored as 'full time' (use once after upgrading)")
    ap.add_argument("--strict", action="store_true", help="exit 1 if any league raised an error")
    ap.add_argument("--no-extras", action="store_true",
                    help="write ONLY the old stats.json keys (no ids, player stats, heat maps, shots, images)")
    ap.add_argument("--no-images", action="store_true", help="do not download player headshots")
    ap.add_argument("--split-details", action="store_true",
                    help="keep stats.json small: per-player stats, heat maps and the shot map go to "
                         "data/{season}/{CODE}/match_details/{fd_match_id}.json")
    ap.add_argument("--no-upgrade", action="store_true",
                    help="do not re-fetch recorded entries written by an older version of this script")
    a = ap.parse_args()
    run(a.competition, a.season, a.mode, a.comp365, a.debug, a.horizon_days,
        a.max_minutes, a.retry_unavailable, a.strict, not a.no_extras, a.rebuild,
        not a.no_images, not a.no_upgrade, a.split_details)