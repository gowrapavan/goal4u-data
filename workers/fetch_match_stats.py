#!/usr/bin/env python3
"""
workers/fetch_match_stats.py
──────────────────────────────────────────────────────────────────────────────
Scrapes deep match statistics (possession, shots, lineups, goals, cards,
substitutions, player ratings, xG) from yallashoot.soccer — bypassing the
football-data.org free-tier paywall that blocks these stats for WC matches.

URL syntax (derived from the live site):
    https://yallashoot.soccer/live/{home-slug}-{away-slug}-{YYYY-MM-DD}/

Output layout (mirrors existing pipeline conventions):
    data/{season}/stats/{match_slug}.json

JSON envelope format (consistent with utils.safe_write):
    {
      "_meta": {
        "last_synced": "<ISO-8601 UTC>",
        "source": "yallashoot.soccer",
        "url":    "<scraped URL>"
      },
      "data": { <match stats payload — see schema below> }
    }

Data schema (data key):
    match_id        str   — internal site ID (data-id attribute on wrapper div)
    match_slug      str   — URL slug, used as file name
    url             str   — canonical URL scraped
    competition     str   — "FIFA World Cup 2026"
    matchday        str   — "Matchweek 1" (or stage name)
    date            str   — "14/06/2026"
    time            str   — "8:00 PM"
    status          str   — "Full Time" | "Live" | "Half Time" | "Scheduled"
    home            TeamBlock
    away            TeamBlock
    score           {home: int, away: int, ht_home: int, ht_away: int}
    statistics      {home: StatsSide, away: StatsSide}
    timeline        [TimelineEvent, ...]

Sync schedule:
    Every 5 minutes during live matches (via GitHub Actions).
    On-demand for finished matches (manual trigger or cron at match end).

Usage:
    # Scrape all matches for a competition dynamically using the link database
    python -m workers.fetch_match_stats --competition PL
    python -m workers.fetch_match_stats --competition WC --today

    # Scrape a single match
    python -m workers.fetch_match_stats --url https://yallashoot.soccer/live/netherlands-japan-2026-06-14/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

# Unified Cloudflare bypass fetcher (Playwright → curl_cffi → free proxy)
from workers.cf_fetcher import fetch_html as _cf_fetch_html

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL  = "https://yallashoot.soccer/live/"
BASE_DIR  = Path("data")          # repo root; override via DATA_DIR env var
SEASON    = "2025-2026"           # override via SEASON env var



# CSS class → our snake_case stat key
_STAT_CLASS_MAP: dict[str, str] = {
    "club-stats__yellowCards":      "yellow_cards",
    "club-stats__corners":          "corners",
    "club-stats__fouls":            "fouls",
    "club-stats__offsides":         "offsides",
    "club-stats__possession":       "possession",
    "club-stats__shots":            "shots",
    "club-stats__shotsOnGoals":     "shots_on_target",
    "club-stats__goals":            "goals",
    "club-stats__shots_off_goal":   "shots_off_goal",
    "club-stats__blocked_shots":    "blocked_shots",
    "club-stats__shots_insidebox":  "shots_insidebox",
    "club-stats__shots_outsidebox": "shots_outsidebox",
    "club-stats__goalkeeper_saves": "goalkeeper_saves",
    "club-stats__total_passes":     "total_passes",
    "club-stats__passes_accurate":  "passes_accurate",
    "club-stats__xg":               "xg",
}

# SVG icon href → player event type
_ICON_MAP: dict[str, str] = {
    "icon-ball":        "goal",
    "icon-card_y":      "card_y",
    "icon-card_r":      "card_r",
    "icon-arrow-o-up":  "subs_in",
    "icon-arrow-o-down":"subs_out",
}

# yallashoot.soccer `status` strings that mean the match is over and the
# stats we've already scraped for it are final — safe to skip forever.
# Anything else ("Scheduled", "Live", "Half Time", missing/unreadable, ...)
# means the existing file is just a stale snapshot and should be re-scraped.
_FINAL_STATS_STATUSES: set[str] = {
    "full time", "ft", "match finished", "finished", "ended",
    "after extra time", "aet", "penalties", "pen.",
}

# Empty stats side template
_EMPTY_STATS: dict[str, Any] = {
    "yellow_cards":     None,
    "corners":          None,
    "fouls":            None,
    "offsides":         None,
    "possession":       None,
    "shots":            None,
    "shots_on_target":  None,
    "goals":            None,
    "shots_off_goal":   None,
    "blocked_shots":    None,
    "shots_insidebox":  None,
    "shots_outsidebox": None,
    "goalkeeper_saves": None,
    "total_passes":     None,
    "passes_accurate":  None,
    "xg":               None,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("fetch_match_stats")


# ── Path helpers ──────────────────────────────────────────────────────────────

def _get_stats_dir() -> Path:
    base   = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
    season = os.environ.get("SEASON", SEASON)
    path   = base / season / "stats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_matches_dir() -> Path:
    """
    Directory containing the per-competition match files written by
    fetch_matches.py, e.g. data/{season}/matches/PL.json.
    """
    base   = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
    season = os.environ.get("SEASON", SEASON)
    return base / season / "matches"


def _get_stats_match_dir(league: str) -> Path:
    """
    Output directory for match-ID-keyed stats files:
        data/{season}/stats/stats-match/{league}/
    """
    base   = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
    season = os.environ.get("SEASON", SEASON)
    path   = base / season / "stats" / "stats-match" / league
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug_from_url(url: str) -> str:
    """
    Extract the match slug from a yallashoot URL.
    """
    url = url.rstrip("/")
    return url.split("/")[-1]


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _fetch_html(url: str) -> str | None:
    """
    Fetch the HTML of a match page using the CF bypass cascade.

    Tries (in order):
      1. Playwright + stealth  — solves the JS challenge CF issues on datacenter IPs
      2. curl_cffi             — fast TLS impersonation (works locally)
      3. Free rotating proxies — last-resort fallback

    Returns the raw HTML string on success, None on failure.
    """
    logger.info("Fetching: %s", url)
    html = _cf_fetch_html(url, retries=2)

    if html is None:
        logger.error("All fetch methods exhausted for %s", url)

    return html


# ── Parse helpers ─────────────────────────────────────────────────────────────

def _txt(el: Tag | None, default: str | None = None) -> str | None:
    """Safe .get_text(strip=True) — returns default when el is None."""
    if el is None:
        return default
    return el.get_text(strip=True) or default


def _coerce_num(val: str | None) -> int | float | None:
    """Convert '59', '0.70', '59%' → numeric. Returns None for non-numeric."""
    if val is None:
        return None
    v = val.strip().rstrip("%")
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return None


# ── Section parsers ───────────────────────────────────────────────────────────

def _parse_match_header(soup: BeautifulSoup) -> dict:
    """Extract the scoreboard header block."""
    wrapper = soup.select_one("[data-id]")
    match_id = wrapper.get("data-id") if wrapper else None

    comp_el  = soup.select_one(".match-scoreboard__header-line a")
    competition = _txt(comp_el)

    matchday = None
    for span in soup.select(".match-scoreboard__header-line span.anwp-text-nowrap"):
        t = _txt(span)
        if t and t not in ("|",):
            matchday = t
            break

    date_el = soup.select_one(".match__date-formatted")
    time_el = soup.select_one(".match__time-formatted")
    match_date = _txt(date_el)
    match_time = _txt(time_el)

    status_el = soup.select_one(".match-scoreboard__text-result span")
    status = _txt(status_el, "Unknown")

    club_wrappers = soup.select(".match-scoreboard__club-wrapper")
    home_name = away_name = None
    home_crest = away_crest = None

    if len(club_wrappers) >= 2:
        home_wrap = club_wrappers[0]
        away_wrap = club_wrappers[-1]
        home_name  = _txt(home_wrap.select_one(".match-scoreboard__club-title"))
        away_name  = _txt(away_wrap.select_one(".match-scoreboard__club-title"))
        home_crest = (home_wrap.select_one("img.match-scoreboard__club-logo") or {}).get("src")
        away_crest = (away_wrap.select_one("img.match-scoreboard__club-logo") or {}).get("src")

    score_nums = soup.select(".match-scoreboard__score-number")

    def _parse_score(el) -> int:
        val = _txt(el, "0").strip()
        try:
            return int(val)
        except ValueError:
            return 0  # Fallback to 0 if the site shows a dash "-" for unplayed matches

    home_score = _parse_score(score_nums[0]) if len(score_nums) > 0 else 0
    away_score = _parse_score(score_nums[1]) if len(score_nums) > 1 else 0

    ht_home = ht_away = None
    footer_el = soup.select_one(".match-scoreboard__footer-line")
    if footer_el:
        ht_match = re.search(r"Half Time:\s*(\d+)-(\d+)", footer_el.get_text())
        if ht_match:
            ht_home, ht_away = int(ht_match.group(1)), int(ht_match.group(2))

    referee = None
    if footer_el:
        ref_match = re.search(r"Referee:\s*(.+?)(?:\s*\|)", footer_el.get_text() + "|")
        if ref_match:
            referee = ref_match.group(1).strip()

    form_items = soup.select(".club-form__item-pro")
    raw_form   = [_txt(f, "").upper() for f in form_items if _txt(f, "").upper() in ("W", "D", "L")]
    home_form = raw_form[:5]
    away_form = raw_form[5:10]

    xg_els  = soup.select(".fl-game-xg--scoreboard .fl-game-xg__val")
    home_xg = _txt(xg_els[0]) if xg_els else None
    away_xg = _txt(xg_els[1]) if len(xg_els) > 1 else None

    return {
        "match_id":    match_id,
        "competition": competition,
        "matchday":    matchday,
        "date":        match_date,
        "time":        match_time,
        "status":      status,
        "referee":     referee,
        "score": {
            "home":    home_score,
            "away":    away_score,
            "ht_home": ht_home,
            "ht_away": ht_away,
        },
        "home": {
            "name":      home_name,
            "crest_url": home_crest,
            "form":      home_form,
            "xg":        _coerce_num(home_xg),
        },
        "away": {
            "name":      away_name,
            "crest_url": away_crest,
            "form":      away_form,
            "xg":        _coerce_num(away_xg),
        },
    }

def _parse_statistics(soup: BeautifulSoup) -> dict:
    home_stats: dict[str, Any] = dict(_EMPTY_STATS)
    away_stats: dict[str, Any] = dict(_EMPTY_STATS)

    wrapper = soup.select_one(".team-stats__modern-wrapper")
    if not wrapper:
        return {"home": home_stats, "away": away_stats}

    for row in wrapper.select(".team-stats"):
        row_classes = row.get("class", [])
        stat_key = next((_STAT_CLASS_MAP[cls] for cls in row_classes if cls in _STAT_CLASS_MAP), None)
        if not stat_key:
            continue

        val_spans = row.select(".team-stats__value")
        if len(val_spans) < 2:
            continue

        home_stats[stat_key] = _coerce_num(_txt(val_spans[0]))
        away_stats[stat_key] = _coerce_num(_txt(val_spans[-1]))

    return {"home": home_stats, "away": away_stats}

def _parse_player_events(player_el: Tag) -> list[dict]:
    events: list[dict] = []
    icons   = player_el.select(".icon--lineups use")
    minutes = player_el.select(".anwp-fl-lineups-event-minutes")

    for i, icon in enumerate(icons):
        href      = icon.get("xlink:href", "")
        icon_id   = href.split("#")[-1]
        event_type = _ICON_MAP.get(icon_id)
        if not event_type:
            continue
        minute = _txt(minutes[i]) if i < len(minutes) else None
        events.append({"type": event_type, "minute": minute})

    return events

def _parse_lineup_section(soup: BeautifulSoup, side: str) -> dict:
    prefix  = f".match-lineups__{side}"
    coach_el  = soup.select_one(f"{prefix}-coach .match__player-name")
    coach_name = _txt(coach_el)

    def _parse_players(selector: str) -> list[dict]:
        players = []
        for pw in soup.select(selector):
            name = _txt(pw.select_one(".match__player-name"))
            if not name:
                continue
            players.append({
                "number":   _txt(pw.select_one(".match__player-number")),
                "name":     name,
                "position": _txt(pw.select_one(".match__player-position")),
                "rating":   _txt(pw.select_one(".match__player-rating")),
                "events":   _parse_player_events(pw),
            })
        return players

    return {
        "coach":       coach_name,
        "starting_xi": _parse_players(f"{prefix}-starting .match__player-wrapper"),
        "substitutes": _parse_players(f"{prefix}-subs .match__player-wrapper"),
    }

def _parse_timeline(soup: BeautifulSoup) -> list[dict]:
    events: list[dict] = []

    for row in soup.select(".match-commentary__row"):
        row_classes = row.get("class", [])

        event_type = None
        if "match-commentary__event--goal" in row_classes: event_type = "goal"
        elif "match-commentary__event--card" in row_classes: event_type = "card"
        elif "match-commentary__event--substitute" in row_classes: event_type = "substitute"
        
        if not event_type:
            continue

        block = row.select_one(".match-commentary__block")
        if not block: continue
        
        block_cls = block.get("class", [])
        side = "home" if "match-commentary__block--home" in block_cls else "away" if "match-commentary__block--away" in block_cls else "unknown"

        minute = _txt(row.select_one(".match-commentary__minute"))
        score_at_event = _txt(row.select_one(".match-commentary__scores"))
        detail_raw  = _txt(row.select_one(".match-commentary__block-sub-header"), "")

        player = assist = player_in = player_out = None

        if event_type == "goal":
            parts = re.split(r"\s*Assistant:\s*", detail_raw, maxsplit=1)
            player = parts[0].strip() or None
            assist = parts[1].strip() if len(parts) > 1 else None

        elif event_type == "card":
            player = detail_raw.strip() or None

        elif event_type == "substitute":
            in_match  = re.search(r"In:\s*(.+?)(?:Out:|$)", detail_raw)
            out_match = re.search(r"Out:\s*(.+?)$", detail_raw)
            player_in  = in_match.group(1).strip()  if in_match  else None
            player_out = out_match.group(1).strip() if out_match else None
            player = player_in

        events.append({
            "minute":     minute,
            "side":       side,
            "type":       event_type,
            "score":      score_at_event,
            "detail":     detail_raw,
            "player":     player,
            "assist":     assist,
            "player_in":  player_in,
            "player_out": player_out,
        })

    return events


# ── Top-level scraper ─────────────────────────────────────────────────────────

def scrape_match(url: str) -> dict | None:
    logger.info("Scraping: %s", url)
    html = _fetch_html(url)
    if not html:
        return None

    soup     = BeautifulSoup(html, "lxml")
    slug     = _slug_from_url(url)

    header     = _parse_match_header(soup)
    statistics = _parse_statistics(soup)
    timeline   = _parse_timeline(soup)

    home_lineup = _parse_lineup_section(soup, "home")
    away_lineup = _parse_lineup_section(soup, "away")

    if statistics["home"].get("xg") is not None:
        header["home"]["xg"] = statistics["home"]["xg"]
        header["away"]["xg"] = statistics["away"]["xg"]
    statistics["home"].pop("xg", None)
    statistics["away"].pop("xg", None)

    home_block = {**header.pop("home"), **home_lineup}
    away_block = {**header.pop("away"), **away_lineup}

    return {
        "match_id":   header.pop("match_id"),
        "match_slug": slug,
        "url":        url,
        **header,
        "home":       home_block,
        "away":       away_block,
        "statistics": statistics,
        "timeline":   timeline,
    }


# ── Skip/refresh decision for already-scraped files ──────────────────────────

def _existing_stats_status(path: Path) -> str | None:
    """
    Peek at an already-written stats JSON file and return its yallashoot
    `status` string (e.g. "Full Time", "Scheduled", "Live"), or None if the
    file is missing/corrupt/has no status.

    Used by run_competition() to decide whether an existing file is truly
    "done" (skip it forever) or just a stale pre-match / in-play snapshot
    that needs to be re-scraped to pick up the real result.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            envelope = json.load(fh)
        data = envelope.get("data") if isinstance(envelope, dict) else None
        return (data or {}).get("status")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read existing stats file %s (%s) — treating as incomplete.", path, exc)
        return None


# ── File writer ───────────────────────────────────────────────────────────────

def safe_write_stats(path: Path, data: dict, source_url: str) -> bool:
    if data is None:
        logger.error("safe_write_stats: data is None — not writing %s", path)
        return False

    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "_meta": {
            "last_synced": datetime.now(timezone.utc).isoformat(),
            "source":      "yallashoot.soccer",
            "url":         source_url,
        },
        "data": data,
    }

    tmp = Path(str(path) + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        logger.info("Wrote %s (%d bytes)", path, path.stat().st_size)
        return True
    except OSError as exc:
        logger.error("Failed to write %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _load_match_links(code: str) -> dict[int, str]:
    """
    Load the pre-computed match URLs from data/{season}/match-links/{code}.json.
    Returns a dictionary mapping match_id -> canonical YallaShoot URL.
    """
    base   = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
    season = os.environ.get("SEASON", SEASON)
    path   = base / season / "match-links" / f"{code}.json"

    if not path.exists():
        logger.warning("Match links file not found: %s — run fetch_match_links.py first", path)
        return {}

    try:
        with open(path, encoding="utf-8") as fh:
            envelope = json.load(fh)
        
        # Support both {"_meta": {...}, "data": [...]} and [...]
        records = envelope.get("data", []) if isinstance(envelope, dict) else envelope
        
        # match_id can be None in the JSON if it was unmatched, so we filter those out
        return {
            rec["match_id"]: rec["url"] 
            for rec in records 
            if rec.get("match_id") and rec.get("url")
        }
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read %s: %s", path, exc)
        return {}


def _load_competition_matches(code: str) -> list[dict]:
    matches_dir = _get_matches_dir()
    path = matches_dir / f"{code}.json"

    if not path.exists():
        logger.warning("Match file not found: %s — run fetch_matches.py first", path)
        return []

    try:
        with open(path, encoding="utf-8") as fh:
            envelope = json.load(fh)
        return envelope.get("data", [])
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read %s: %s", path, exc)
        return []


def _utc_date(match: dict) -> datetime | None:
    raw = match.get("utcDate")
    if not raw: return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _filter_matches_by_period(matches: list[dict], period: str | None) -> list[dict]:
    if period is None: return matches
    now = datetime.now(timezone.utc)

    if period == "today":
        target_date = now.date()
        return [m for m in matches if (dt := _utc_date(m)) and dt.date() == target_date]
    if period == "week":
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = week_start + timedelta(days=7)
        return [m for m in matches if (dt := _utc_date(m)) and week_start <= dt < week_end]
    if period == "month":
        return [m for m in matches if (dt := _utc_date(m)) and dt.year == now.year and dt.month == now.month]
    if period == "year":
        return [m for m in matches if (dt := _utc_date(m)) and dt.year == now.year]

    return matches


def _discover_competition_codes() -> list[str]:
    matches_dir = _get_matches_dir()
    if not matches_dir.exists():
        logger.error("Matches directory not found: %s", matches_dir)
        return []
    codes = sorted(p.stem for p in matches_dir.glob("*.json"))
    logger.info("Discovered %d competition(s): %s", len(codes), ", ".join(codes))
    return codes


def run_competition(
    code: str,
    period: str | None = None,
    force: bool = False,
    delay: float = 3.0,
    statuses: tuple[str, ...] = ("FINISHED", "IN_PLAY", "PAUSED", "LIVE", "TIMED", "SCHEDULED"),
) -> tuple[int, int, int]:
    
    matches = _load_competition_matches(code)
    if not matches: return 0, 0, 0

    eligible = [m for m in matches if m.get("status") in statuses]
    if period: eligible = _filter_matches_by_period(eligible, period)

    if not eligible:
        logger.info("%s: nothing to scrape — returning", code)
        return 0, 0, 0

    # ── Load Exact URLs from our Database ──
    match_links = _load_match_links(code)
    if not match_links:
        logger.warning("%s: match-links JSON missing or empty. Skipping.", code)
        return 0, 0, len(eligible)

    stats_match_dir = _get_stats_match_dir(code)
    ok = failed = skipped = 0

    for i, match in enumerate(eligible):
        match_id   = match.get("id")
        home_name  = (match.get("homeTeam") or {}).get("name", "?")
        away_name  = (match.get("awayTeam") or {}).get("name", "?")
        match_date = match.get("utcDate", "")[:10]

        if not match_id:
            skipped += 1
            continue

        out_path = stats_match_dir / f"{match_id}.json"

        if out_path.exists() and not force:
            logger.info("[%d/%d] Skip (exists): %s vs %s (%s)", i + 1, len(eligible), home_name, away_name, match_id)
            skipped += 1
            continue

        # ── EXACT URL LOOKUP ──
        url = match_links.get(match_id)
        if not url:
            logger.warning("[%d/%d] Skip (no URL mapping): %s vs %s (%s)", i + 1, len(eligible), home_name, away_name, match_id)
            skipped += 1
            continue

        logger.info("[%d/%d] %s | %s vs %s  (%s)", i + 1, len(eligible), code, home_name, away_name, match_date)

        if i > 0 and (ok + failed) > 0: time.sleep(delay)

        data = scrape_match(url)

        # Skip on 404/Missing Data so GitHub Actions stay green
        if data is None:
            logger.warning("  Stats not available yet (404) or scrape failed for match %s. Skipping.", match_id)
            skipped += 1
            continue

        data["fd_match_id"]          = match_id
        data["fd_competition_code"]  = code

        if safe_write_stats(out_path, data, url):
            ok += 1
        else:
            failed += 1

    logger.info("%s complete: %d OK / %d failed / %d skipped (of %d eligible)", code, ok, failed, skipped, len(eligible))
    return ok, failed, skipped


def run_all_competitions(period: str | None = None, force: bool = False, delay: float = 3.0) -> None:
    codes = _discover_competition_codes()
    if not codes: sys.exit(1)

    total_ok = total_failed = total_skipped = 0
    for code in codes:
        ok, failed, skipped = run_competition(code, period=period, force=force, delay=delay)
        total_ok += ok; total_failed += failed; total_skipped += skipped

    logger.info("=== run_all_competitions complete: %d OK / %d failed / %d skipped ===", total_ok, total_failed, total_skipped)


# ── Legacy URL builder (preserved for --match CLI usage) ──────────────────────

_TEAM_SLUG_OVERRIDES: dict[str, str] = {
    "manchester united fc": "manchester-united", "manchester city fc": "manchester-city",
    "arsenal fc": "arsenal", "chelsea fc": "chelsea", "liverpool fc": "liverpool",
    "tottenham hotspur fc": "tottenham-hotspur", "newcastle united fc": "newcastle-united",
    "aston villa fc": "aston-villa", "brighton & hove albion fc": "brighton-hove-albion",
    "west ham united fc": "west-ham-united", "wolverhampton wanderers fc": "wolverhampton-wanderers",
    "nottingham forest fc": "nottingham-forest", "brentford fc": "brentford", "fulham fc": "fulham",
    "crystal palace fc": "crystal-palace", "everton fc": "everton", "afc bournemouth": "bournemouth",
    "leicester city fc": "leicester-city", "leeds united fc": "leeds-united",
    "ipswich town fc": "ipswich-town", "southampton fc": "southampton",
    "real madrid cf": "real-madrid", "fc barcelona": "barcelona", "atletico madrid": "atletico-madrid",
    "ssc napoli": "napoli", "ac milan": "ac-milan", "juventus fc": "juventus",
    "fc bayern münchen": "bayern-munich", "borussia dortmund": "borussia-dortmund",
    "paris saint-germain fc": "paris-saint-germain", "olympique de marseille": "marseille",
}

def _slugify_team(name: str) -> str:
    key = name.strip().lower()
    if key in _TEAM_SLUG_OVERRIDES: return _TEAM_SLUG_OVERRIDES[key]
    for pat in [r"\bafc\b", r"\bfc\b", r"\bsc\b", r"\bcf\b", r"\bbc\b", r"\bssc\b", r"\bas\b", r"\bss\b"]:
        key = re.sub(pat, " ", key)
    for src, dst in [("ü", "ue"), ("ö", "oe"), ("ä", "ae"), ("ß", "ss"), ("é", "e"), ("ñ", "n"), ("&", ""), ("'", "")]:
        key = key.replace(src, dst)
    return re.sub(r"[\s\-_]+", "-", key).strip("-")

def build_url(home: str, away: str, date: str) -> str:
    return f"{BASE_URL}{_slugify_team(home)}-{_slugify_team(away)}-{date}/"

def run_single(url: str) -> bool:
    slug = _slug_from_url(url)
    out_path = _get_stats_dir() / f"{slug}.json"
    data = scrape_match(url)
    return False if data is None else safe_write_stats(out_path, data, url)

def run_batch(urls: list[str], delay: float = 3.0) -> tuple[int, int]:
    ok = fail = 0
    for i, url in enumerate(urls):
        if i > 0: time.sleep(delay)
        if run_single(url): ok += 1
        else: fail += 1
    return ok, fail


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(description="Scrape match stats from yallashoot.soccer.")
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", "-u", help="Single match URL to scrape.")
    group.add_argument("--file", "-f", help="Path to a text file containing one match URL per line.")
    group.add_argument("--match", "-m", nargs=3, metavar=("HOME", "AWAY", "DATE"), help="Build URL from team names and date.")
    group.add_argument("--competition", "-c", metavar="CODE", help="Scrape all eligible matches for one competition.")
    group.add_argument("--all", "-a", action="store_true", dest="all_competitions", help="Scrape all discovered competitions.")

    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between requests.")
    parser.add_argument("--force", action="store_true", help="Re-scrape even when the output file already exists.")

    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument("--today", action="store_true", help="Only process matches scheduled for today.")
    date_group.add_argument("--week", action="store_true", help="Only process matches in the current week.")
    date_group.add_argument("--month", action="store_true", help="Only process matches in the current month.")
    date_group.add_argument("--year", action="store_true", help="Only process matches in the current year.")

    args = parser.parse_args()

    period = "today" if args.today else "week" if args.week else "month" if args.month else "year" if args.year else None

    if args.url:
        sys.exit(0 if run_single(args.url) else 1)
    elif args.file:
        urls = [line.strip() for line in Path(args.file).read_text().splitlines() if line.strip() and not line.startswith("#")]
        ok, fail = run_batch(urls, delay=args.delay)
        sys.exit(0 if fail == 0 else 1)
    elif args.match:
        sys.exit(0 if run_single(build_url(*args.match)) else 1)
    elif args.competition:
        ok, failed, skipped = run_competition(args.competition, period=period, force=args.force, delay=args.delay)
        sys.exit(0 if failed == 0 else 1)
    elif args.all_competitions:
        run_all_competitions(period=period, force=args.force, delay=args.delay)
        sys.exit(0)

if __name__ == "__main__":
    _main()