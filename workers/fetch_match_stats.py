#!/usr/bin/env python3
"""
workers/fetch_match_stats.py  (FAST CONCURRENT VERSION — HTML-AWARE PARSER)
──────────────────────────────────────────────────────────────────────────────
Scrapes per-match statistics from yallashoot.soccer and stores them in a
single stats.json file per competition, indexed by str(match_id).

Parser strategy
───────────────
The page is rendered by a WordPress + AnWP Football Leagues plugin.
All stats live in real structured HTML — NOT a text blob. We use
BeautifulSoup with specific CSS selectors confirmed against the live HTML:

  • Scoreboard:  .match-scoreboard__club-title  (home=first, away=second)
                 .match-scoreboard__score-number (home=first, away=second)
                 .match-scoreboard__text-result span  (status)
                 .match-scoreboard__footer-line  (HT score)

  • Stats table: .team-stats  (one div per stat)
                 Inside each: span.team-stats__value (home=first, away=second)
                              span.anwp-flex-none    (label)
                 The outer div class e.g. club-stats__corners gives the key.

  • Events:      .game-timeline__item[data-tippy-content]
                 Formats observed:
                   "20' Goooal!: B. Mbeumo (assistant: C. Nørgaard)"
                   "72' Goal (from penalty): E. Haaland"
                   "88' Goal (own goal): J. Smith"
                   "35' Red Card: "  ← second yellow (name empty in tooltip)
                 Side:   class contains 'item-home' or 'item-away'
                 Fallback: .match-commentary__row player names used when
                   tooltip player is null/empty (penalty goals, 2nd-yellow reds)

  • Lineups:     .match-lineups__{side}-starting / -subs / -coach
                 .match__player-name, .match__player-number,
                 .match__player-position, .match__player-rating

Concurrency
───────────
ThreadPoolExecutor (default 8 workers) — cf_fetcher uses sync libs so we
can't use asyncio. Each thread scrapes independently; a write_lock guards
the stats_store dict and checkpoint saves.

Speed:  ~5–8 min for 380 matches with 8 workers  (vs ~63 min sequential)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

sys.path.insert(0, ".")

from workers.cf_fetcher import fetch_html as _cf_fetch_html
from workers.tournament_paths import get_data_paths
from config import TRACKED_COMPETITIONS, get_season_paths as _get_season_paths

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_match_stats")

# ── Season ────────────────────────────────────────────────────────────────────
SEASON = os.environ.get("SEASON", _get_season_paths()["season"])

# ── Concurrency defaults ──────────────────────────────────────────────────────
DEFAULT_WORKERS       = 8
DEFAULT_CHECKPOINT_N  = 25
DEFAULT_RETRY_FAILED  = 2

# ── Statuses that mean a match is over and stats are final ────────────────────
_FINAL_STATUSES: frozenset[str] = frozenset({
    "full time", "ft", "match finished", "finished", "ended",
    "after extra time", "aet", "after penalties", "penalties", "pen.",
    "FINISHED",
})

# ── Stats CSS class → canonical key mapping ────────────────────────────────────
# Maps the outer div class suffix (club-stats__X) to a clean snake_case key.
_STAT_CLASS_MAP: dict[str, str] = {
    "yellowCards":    "yellow_cards",
    "yellowcards":    "yellow_cards",
    "red_cards":      "red_cards",
    "redCards":       "red_cards",
    "corners":        "corners",
    "fouls":          "fouls",
    "offsides":       "offsides",
    "possession":     "possession",
    "shots":          "shots",
    "shotsOnGoals":   "shots_on_target",
    "shotsongoals":   "shots_on_target",
    "shots_off_goal": "shots_off_goal",
    "blocked_shots":  "blocked_shots",
    "shots_insidebox":"shots_insidebox",
    "shots_outsidebox":"shots_outsidebox",
    "goalkeeper_saves":"goalkeeper_saves",
    "total_passes":   "total_passes",
    "passes_accurate":"passes_accurate",
    "goals":          "goals",
    "xg":             "expected_goals",
}


# ═══════════════════════════════════════════════════════════════════════════════
# PARSERS  (thread-safe: read-only, no shared state)
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_num(val: str) -> int | float | str | None:
    """Coerce a string to int, float or keep as string (for % values)."""
    v = val.strip()
    if not v:
        return None
    if v.endswith("%"):
        return v  # keep possession as "49%"
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v or None


def parse_header(soup: BeautifulSoup) -> dict:
    """
    Extract home/away teams, score, HT score, and match status.
    Uses .match-scoreboard__* selectors confirmed on live HTML.
    """
    result: dict[str, Any] = {
        "home_team": None,
        "away_team": None,
        "status":    None,
        "score":     {"home": None, "away": None, "ht_home": None, "ht_away": None},
    }

    # Team names — first and second .match-scoreboard__club-title
    team_els = soup.select(".match-scoreboard__club-title")
    if len(team_els) >= 1:
        result["home_team"] = team_els[0].get_text(strip=True) or None
    if len(team_els) >= 2:
        result["away_team"] = team_els[1].get_text(strip=True) or None

    # Score — first and second .match-scoreboard__score-number
    score_els = soup.select(".match-scoreboard__score-number")
    if len(score_els) >= 2:
        try:
            result["score"]["home"] = int(score_els[0].get_text(strip=True))
            result["score"]["away"] = int(score_els[1].get_text(strip=True))
        except (ValueError, TypeError):
            pass

    # Status — inside .match-scoreboard__text-result
    status_el = soup.select_one(".match-scoreboard__text-result span")
    if status_el:
        result["status"] = status_el.get_text(strip=True).lower()

    # HT score — in .match-scoreboard__footer-line text
    for footer in soup.select(".match-scoreboard__footer-line"):
        txt = footer.get_text(separator=" ", strip=True)
        m = re.search(r"[Hh]alf\s*[Tt]ime[:\s]+(\d+)[-:\s]+(\d+)", txt)
        if m:
            result["score"]["ht_home"] = int(m.group(1))
            result["score"]["ht_away"] = int(m.group(2))
            break

    return result


def parse_stats(soup: BeautifulSoup) -> dict:
    """
    Extract the match stats table.

    Each stat row is a div.team-stats with a class like club-stats__corners.
    Inside: two span.team-stats__value (home, away) and one label span.

    Returns dict like:
        {
            "corners":       {"home": 8,  "away": 2},
            "possession":    {"home": "49%", "away": "51%"},
            "shots":         {"home": 18, "away": 12},
            "expected_goals":{"home": 1.12, "away": 1.34},
            ...
        }
    """
    stats: dict[str, dict] = {}

    for row in soup.select(".team-stats"):
        # Identify stat key from outer class  e.g. club-stats__yellowCards
        outer_classes = row.get("class", [])
        raw_key = None
        for cls in outer_classes:
            if cls.startswith("club-stats__"):
                raw_key = cls.replace("club-stats__", "")
                break

        if raw_key is None:
            # Fall back to label text
            label_el = row.select_one("span.anwp-flex-none, span.anwp-text-sm")
            if label_el:
                raw_key = label_el.get_text(strip=True).lower()
                raw_key = re.sub(r"[^a-z0-9]+", "_", raw_key).strip("_")
            if not raw_key:
                continue

        # Map to canonical key
        canonical = _STAT_CLASS_MAP.get(raw_key) or re.sub(r"[^a-z0-9]+", "_", raw_key.lower()).strip("_")

        # Values — first and last span.team-stats__value
        val_spans = row.select("span.team-stats__value")
        if len(val_spans) < 2:
            continue

        home_raw = val_spans[0].get_text(strip=True)
        away_raw = val_spans[-1].get_text(strip=True)

        # For possession, append % so UI knows it's a percentage
        label_el = row.select_one("span.anwp-flex-none, span.anwp-text-sm")
        label_text = label_el.get_text(strip=True).lower() if label_el else ""
        if "possession" in label_text or "possession" in canonical:
            if home_raw and not home_raw.endswith("%"):
                home_raw += "%"
            if away_raw and not away_raw.endswith("%"):
                away_raw += "%"

        stats[canonical] = {
            "home": _safe_num(home_raw),
            "away": _safe_num(away_raw),
        }

    return stats


def _build_commentary_player_map(soup: BeautifulSoup) -> dict:
    """
    Build a fallback map from the match commentary section.
    Commentary renders full player names and event types even when tooltips
    have null/empty player names (e.g. second-yellow red cards, penalty goals).

    Returns a dict keyed by (minute: int, event_type: str, side: str | None)
    mapping to player name string.

    The commentary block structure:
      .match-commentary__row[data-event-id]
        |- .match-commentary__block--home or --away   → determines side
        |   |- .match-commentary__block-header
        |   |   |- .match-commentary__minute          → "72'"
        |   |   |- .match-commentary__event-name      → "Goal (from penalty)", "Substitute", etc.
        |   |- .match-commentary__block-sub-header    → player name(s)
    """
    fallback: dict[tuple, str] = {}

    for row in soup.select(".match-commentary__row"):
        block = row.select_one(
            ".match-commentary__block--home, .match-commentary__block--away"
        )
        if not block:
            continue

        # Determine side
        block_classes = " ".join(block.get("class", []))
        if "block--home" in block_classes:
            side = "home"
        elif "block--away" in block_classes:
            side = "away"
        else:
            side = None

        # Minute
        min_el = block.select_one(".match-commentary__minute")
        if not min_el:
            continue
        min_text = min_el.get_text(strip=True).rstrip("'").strip()
        # Handle "90+2'" style
        min_text = re.sub(r"\+.*", "", min_text).strip()
        try:
            minute = int(min_text)
        except ValueError:
            continue

        # Event type label
        ename_el = block.select_one(".match-commentary__event-name")
        event_label = ename_el.get_text(strip=True).lower() if ename_el else ""

        # Classify to match our event types
        if "goal" in event_label:
            etype = "goal"
        elif "yellow card" in event_label:
            etype = "yellow_card"
        elif "red card" in event_label:
            etype = "red_card"
        elif "substitute" in event_label:
            etype = "substitution"
        elif "penalty" in event_label:
            etype = "penalty"
        else:
            etype = "other"

        # Player name from sub-header
        sub_el = block.select_one(".match-commentary__block-sub-header")
        if not sub_el:
            continue

        # For goals/cards/penalties: sub-header is just the player name
        # For substitutions: "In: X  Out: Y"  — we want the first (player in)
        if etype == "substitution":
            # Pick the "In:" span
            in_div = sub_el.select_one(".anwp-text-nowrap:first-child")
            player_text = in_div.get_text(strip=True) if in_div else sub_el.get_text(strip=True)
            # Strip "In:" label
            player_text = re.sub(r"^(?:in|out)\s*:\s*", "", player_text, flags=re.I).strip()
        else:
            player_text = sub_el.get_text(separator=" ", strip=True).strip()

        if player_text:
            fallback[(minute, etype, side)] = player_text

    return fallback


def parse_events(soup: BeautifulSoup) -> list[dict]:
    """
    Extract timeline events from data-tippy-content attributes on
    .game-timeline__item elements, with commentary fallback for null players.

    Tooltip format variants observed on live HTML:
      "20' Goooal!: B. Mbeumo (assistant: C. Nørgaard)"   ← standard goal
      "72' Goal (from penalty): E. Haaland"               ← penalty goal
      "88' Goal (own goal): J. Smith"                     ← own goal
      "35' Yellow Card: João Gomes"
      "35' Red Card: "                                     ← second yellow → red (name missing)
      "65' Substitute: R. Gomes > R. Aït Nouri"
      "90'+1' Substitute: T. Lloyd King > José Sá"

    Root causes of null players fixed here:
      1. "Goal (from penalty):" — old regex required "ooo+" and missed this variant.
         Fixed by a unified goal regex that matches all label forms before ":".
      2. "Red Card: " with empty player — site omits name for second-yellow reds.
         Fixed by commentary fallback map.
      3. goal_type field added: "normal" | "penalty" | "own_goal"
    """
    # Build commentary fallback before processing tooltips
    commentary_map = _build_commentary_player_map(soup)

    events: list[dict] = []
    seen: set[str] = set()

    for el in soup.select(".game-timeline__item[data-tippy-content]"):
        tip = el.get("data-tippy-content", "").strip()
        if not tip:
            continue

        # Deduplicate (same tooltip can appear in both 1st and 2nd half strips)
        if tip in seen:
            continue
        seen.add(tip)

        # Parse minute — supports "90'+1'" and "90' +2'" styles
        m_min = re.match(r"(\d+)(?:'\s*\+?\s*\d+)?'[\s\u00a0]*(.*)", tip, re.S)
        if not m_min:
            continue

        minute  = int(m_min.group(1))
        content = m_min.group(2).strip()

        # Determine side from element CSS class
        el_classes = " ".join(el.get("class", []))
        if "item-home" in el_classes:
            side = "home"
        elif "item-away" in el_classes:
            side = "away"
        else:
            side = None

        cl = content.lower()

        # ── GOAL (all variants) ────────────────────────────────────────────
        # Handles: "Goooal!: Name", "Goal: Name", "Goal (from penalty): Name",
        #          "Goal (own goal): Name", "Goal (Penalty): Name"
        if re.match(r"go+al", cl) or re.match(r"goal", cl):
            etype = "goal"

            # Determine goal sub-type from label
            if re.search(r"own.?goal|own goal", cl):
                goal_type = "own_goal"
            elif re.search(r"penalty|from penalty", cl):
                goal_type = "penalty"
            else:
                goal_type = "normal"

            # Extract player name: everything after the last ":" up to "("
            # Handles: "Goooal!: Name", "Goal (from penalty): Name"
            pm = re.search(r":\s*([^(]+)", content)
            if pm:
                player = pm.group(1).strip() or None
            else:
                player = None

            # Extract assistant
            assist_m = re.search(r"\(assistant:\s*([^)]+)\)", content, re.I)
            assistant = assist_m.group(1).strip() if assist_m else None

            # Fallback to commentary if player still null
            if not player:
                player = commentary_map.get((minute, "goal", side))

        # ── YELLOW CARD ────────────────────────────────────────────────────
        elif "yellow card" in cl:
            etype     = "yellow_card"
            goal_type = None
            pm        = re.match(r"[Yy]ellow [Cc]ard:?\s*(.+)", content)
            player    = pm.group(1).strip() if pm else None
            assistant = None
            if not player:
                player = commentary_map.get((minute, "yellow_card", side))

        # ── RED CARD ───────────────────────────────────────────────────────
        elif "red card" in cl:
            etype     = "red_card"
            goal_type = None
            pm        = re.match(r"[Rr]ed [Cc]ard:?\s*(.+)", content)
            player    = pm.group(1).strip() if pm else None
            assistant = None
            # Second-yellow red cards often have empty player in tooltip
            if not player:
                # Try commentary fallback; also check yellow_card key (site
                # sometimes logs the second yellow under yellow_card in commentary)
                player = (
                    commentary_map.get((minute, "red_card", side))
                    or commentary_map.get((minute, "yellow_card", side))
                )

        # ── SUBSTITUTION ───────────────────────────────────────────────────
        elif "substitute" in cl:
            etype     = "substitution"
            goal_type = None
            # "Substitute: Player In > Player Out"
            pm = re.match(r"[Ss]ubstitute:?\s*(.+?)\s*[>→]\s*(.+)", content)
            if pm:
                player    = pm.group(1).strip()   # player coming IN
                assistant = pm.group(2).strip()   # player going OUT
            else:
                player    = content
                assistant = None
            if not player:
                player = commentary_map.get((minute, "substitution", side))

        # ── STANDALONE PENALTY (missed / saved — not a goal) ──────────────
        elif "penalty" in cl or "pen." in cl:
            etype     = "penalty_missed"
            goal_type = None
            # "Penalty missed: Name" or "Penalty saved: Name"
            pm = re.search(r":\s*(.+)", content)
            player    = pm.group(1).strip() if pm else content.strip() or None
            assistant = None

        # ── FALLTHROUGH ────────────────────────────────────────────────────
        else:
            etype     = "other"
            goal_type = None
            player    = content or None
            assistant = None

        # Clean up any residual HTML entity artefacts in names (e.g. "O&apos;Reilly")
        if player:
            player = re.sub(r"&apos;", "'", player)
            player = re.sub(r"&amp;",  "&", player)
            player = player.strip() or None
        if assistant:
            assistant = re.sub(r"&apos;", "'", assistant)
            assistant = re.sub(r"&amp;",  "&", assistant)
            assistant = assistant.strip() or None

        event: dict[str, Any] = {
            "minute": minute,
            "type":   etype,
            "team":   side,
            "player": player,
        }
        if etype == "goal":
            event["goal_type"] = goal_type
        if etype == "goal" and assistant:
            event["assistant"] = assistant
        if etype == "substitution" and assistant:
            event["player_out"] = assistant

        events.append(event)

    events.sort(key=lambda e: e["minute"])
    return events


def _parse_player(wrapper) -> dict:
    name_el   = wrapper.select_one(".match__player-name")
    num_el    = wrapper.select_one(".match__player-number")
    pos_el    = wrapper.select_one(".match__player-position")
    rating_el = wrapper.select_one(".match__player-rating")
    return {
        "name":     name_el.get_text(strip=True)   if name_el   else None,
        "number":   num_el.get_text(strip=True)    if num_el    else None,
        "position": pos_el.get_text(strip=True)    if pos_el    else None,
        "rating":   rating_el.get_text(strip=True) if rating_el else None,
    }


def parse_lineups(soup: BeautifulSoup) -> dict:
    """
    Extract starting XI, substitutes, and coach for each side.

    Selectors:
      .match-lineups__home-starting .match__player-wrapper
      .match-lineups__home-subs     .match__player-wrapper
      .match-lineups__home-coach    .match__player-name
      (same pattern for 'away')
    """
    lineups: dict[str, dict] = {
        "home": {"formation": None, "starting": [], "subs": [], "coach": None},
        "away": {"formation": None, "starting": [], "subs": [], "coach": None},
    }

    for side in ("home", "away"):
        starting_wrappers = soup.select(
            f".match-lineups__{side}-starting .match__player-wrapper"
        )
        lineups[side]["starting"] = [_parse_player(w) for w in starting_wrappers]

        sub_wrappers = soup.select(
            f".match-lineups__{side}-subs .match__player-wrapper"
        )
        lineups[side]["subs"] = [_parse_player(w) for w in sub_wrappers]

        coach_el = soup.select_one(
            f".match-lineups__{side}-coach .match__player-name"
        )
        lineups[side]["coach"] = coach_el.get_text(strip=True) if coach_el else None

        # Formation from pitch diagram (if present)
        # The formation string appears as the class suffix in .fl-formation-home/away
        # but the actual text value isn't rendered — skip for now

    return lineups


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN SCRAPE ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_match(url: str) -> dict | None:
    """
    Fetch a yallashoot match page and return parsed stats dict.
    Returns None if the fetch fails or the match is not yet finished.
    Thread-safe: read-only, no shared state.
    """
    html = _cf_fetch_html(url, retries=2)
    if not html:
        logger.warning("[stats] CF fetch failed for %s", url)
        return None

    soup = BeautifulSoup(html, "lxml")

    header     = parse_header(soup)
    page_status = (header.get("status") or "").lower().strip()

    # Only store stats for finished matches
    is_final = any(fs in page_status for fs in _FINAL_STATUSES) if page_status else False
    if page_status and not is_final:
        logger.info("[stats] Not final (status=%r) — skipping %s", page_status, url)
        return None
    if not page_status:
        # Page loaded but scoreboard didn't render — check if we got ANY useful
        # data at all (team names are the most reliable signal). If not, the page
        # was a CF challenge response or an incomplete render; return None so the
        # caller retries rather than writing a hollow entry to stats.json.
        if not header.get("home_team") and not header.get("away_team"):
            logger.warning(
                "[stats] Empty page (no status, no team names) for %s — will retry", url
            )
            return None
        logger.warning("[stats] Could not determine status for %s — treating as final", url)

    return {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source_url": url,
        "status":     header["status"],
        "home_team":  header["home_team"],
        "away_team":  header["away_team"],
        "score":      header["score"],
        "stats":      parse_stats(soup),
        "events":     parse_events(soup),
        "lineups":    parse_lineups(soup),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STATS.JSON — SINGLE-FILE STORE
# ═══════════════════════════════════════════════════════════════════════════════

def _load_stats(stats_path: str) -> dict[str, dict]:
    path = Path(stats_path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("data", {})
    except Exception as exc:
        logger.warning("[stats] Could not load existing stats.json: %s", exc)
        return {}


def _save_stats(stats_path: str, data: dict[str, dict], competition: str, season: str) -> None:
    """Atomically write stats.json. Must be called with the write lock held."""
    path = Path(stats_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "last_synced":   datetime.now(timezone.utc).isoformat(),
            "competition":   competition,
            "season":        season,
            "total_entries": len(data),
        },
        "data": data,
    }
    tmp = Path(str(stats_path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, stats_path)
    logger.info("[stats] Checkpoint → %s  (%d entries)", stats_path, len(data))


def _load_match_links(links_path: str) -> dict[int, str]:
    path = Path(links_path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            records = json.load(fh).get("data", [])
        return {
            rec["match_id"]: rec["url"]
            for rec in records
            if rec.get("match_id") and rec.get("url")
        }
    except Exception as exc:
        logger.warning("[stats] Could not load match_stats_links.json: %s", exc)
        return {}


def _load_competition_matches(matches_path: str) -> list[dict]:
    path = Path(matches_path)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("data", [])
    except Exception as exc:
        logger.warning("[stats] Could not load matches.json: %s", exc)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# WORKER TASK  (runs in a thread)
# ═══════════════════════════════════════════════════════════════════════════════

def _scrape_task(match: dict, url: str, code: str) -> tuple[dict, dict | None]:
    mid = match["id"]
    max_attempts = DEFAULT_RETRY_FAILED + 1
    for attempt in range(1, max_attempts + 1):
        try:
            data = scrape_match(url)
            if data is not None:
                data["match_id"]            = mid
                data["fd_competition_code"] = code
                return match, data
            # scrape_match returned None — page empty or not final.
            # Retry unless this is the last attempt.
            if attempt < max_attempts:
                wait = 3 * attempt
                logger.warning(
                    "[stats] match %s attempt %d/%d returned no data — retrying in %ds",
                    mid, attempt, max_attempts, wait,
                )
                time.sleep(wait)
            else:
                logger.error("[stats] match %s all %d attempts returned no data", mid, max_attempts)
            continue
        except Exception as exc:
            if attempt <= DEFAULT_RETRY_FAILED:
                wait = 3 * attempt
                logger.warning(
                    "[stats] match %s attempt %d/%d failed (%s) — retrying in %ds",
                    mid, attempt, DEFAULT_RETRY_FAILED + 1, exc, wait,
                )
                time.sleep(wait)
            else:
                logger.error("[stats] match %s all retries exhausted: %s", mid, exc)
    return match, None


# ═══════════════════════════════════════════════════════════════════════════════
# CONCURRENT AUDIT RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def _is_entry_empty(entry: dict) -> bool:
    """
    Return True if a stats_store entry was saved but contains no useful data.

    This happens when the scraper fetched the page successfully but the match
    page hadn't rendered its data yet (e.g. the match just finished, or the
    yallashoot page was still loading). We detect this by checking whether ALL
    of the key fields are None/empty — if so, we should re-fetch on the next run.

    An entry is considered empty when ALL of these are true:
      • home_team is None
      • away_team is None
      • score.home is None
      • stats dict is empty  {}
      • events list is empty []
    """
    if not entry:
        return True
    score = entry.get("score") or {}
    return (
        entry.get("home_team") is None
        and entry.get("away_team") is None
        and score.get("home") is None
        and not entry.get("stats")       # {} is falsy
        and not entry.get("events")      # [] is falsy
    )


def run_competition_audit(
    code: str,
    season_str: str | None = None,
    force: bool = False,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
    checkpoint_n: int = DEFAULT_CHECKPOINT_N,
) -> tuple[int, int, int, int]:
    """
    Concurrent audit for one competition.
    Returns (ok, failed, skipped, pending).
    """
    s = season_str or SEASON
    paths       = get_data_paths(code, season=s)
    matches     = _load_competition_matches(paths["matches"])
    match_links = _load_match_links(paths["match_stats_links"])
    stats_store = _load_stats(paths["stats"])

    write_lock  = threading.Lock()
    dirty_count = 0

    eligible = [m for m in matches if m.get("status") == "FINISHED"]
    if not eligible:
        logger.info("[stats] %s: no FINISHED matches found", code)
        return 0, 0, 0, 0

    needs_fetch: list[tuple[dict, str]] = []
    skipped = 0

    for match in eligible:
        mid = match.get("id")
        if not mid:
            skipped += 1
            continue
        existing = stats_store.get(str(mid))
        if existing is not None and not force and not _is_entry_empty(existing):
            skipped += 1
            continue
        if mid not in match_links:
            logger.debug("[stats] %s: no link for match %s — skipping", code, mid)
            skipped += 1
            continue
        needs_fetch.append((match, match_links[mid]))

    total_to_fetch = len(needs_fetch)
    if limit is not None and total_to_fetch > limit:
        pending_before = total_to_fetch - limit
        needs_fetch    = needs_fetch[:limit]
    else:
        pending_before = 0

    logger.info(
        "[stats] %s: %d to fetch  |  %d skipped  |  %d workers  |  checkpoint every %d",
        code, len(needs_fetch), skipped, workers, checkpoint_n,
    )

    if not needs_fetch:
        return 0, 0, skipped, pending_before

    ok = failed = 0
    t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scraper") as pool:
        futures = {
            pool.submit(_scrape_task, match, url, code): match
            for match, url in needs_fetch
        }

        completed_count = 0
        for future in as_completed(futures):
            completed_count += 1
            match, data = future.result()
            mid = match["id"]

            with write_lock:
                if data is not None:
                    stats_store[str(mid)] = data
                    ok          += 1
                    dirty_count += 1

                    elapsed   = time.monotonic() - t0
                    rate      = ok / elapsed if elapsed > 0 else 0
                    remaining = len(needs_fetch) - completed_count
                    eta_s     = int(remaining / rate) if rate > 0 else 0
                    logger.info(
                        "[stats] %s  ✓ %d/%d  |  %.1f/s  |  ETA ~%dm%02ds  (match %s)",
                        code, ok, len(needs_fetch),
                        rate, eta_s // 60, eta_s % 60, mid,
                    )

                    if dirty_count >= checkpoint_n:
                        _save_stats(paths["stats"], stats_store, code, s)
                        dirty_count = 0
                else:
                    failed += 1
                    logger.warning("[stats] %s  ✗ %d/%d  (match %s failed)",
                                   code, ok + failed, len(needs_fetch), mid)

    with write_lock:
        if dirty_count > 0:
            _save_stats(paths["stats"], stats_store, code, s)

    elapsed = time.monotonic() - t0
    logger.info(
        "[stats] %s done in %.0fs — ok=%d  failed=%d  skipped=%d  pending=%d  "
        "(avg %.2fs/match with %d workers)",
        code, elapsed, ok, failed, skipped, pending_before,
        elapsed / ok if ok else 0, workers,
    )
    return ok, failed, skipped, pending_before


def run_all_competitions_audit(
    force: bool = False,
    limit: int | None = None,
    season_str: str | None = None,
    workers: int = 8,
    checkpoint_n: int = 25,
) -> None:
    """
    Run concurrent audit across all tracked competitions, respecting a
    total match budget. Each competition runs sequentially; parallelism
    is within a competition (across matches).
    """
    budget = limit
    for code in TRACKED_COMPETITIONS:
        # FIX: Check if budget is not None before doing the <= 0 check
        if budget is not None and budget <= 0:
            logger.info("[stats] Budget exhausted — stopping")
            break
            
        ok, failed, skipped, pending = run_competition_audit(
            code,
            season_str=season_str,
            force=force,
            limit=budget,
            workers=workers,
            checkpoint_n=checkpoint_n,
        )
        
        # FIX: Only subtract from budget if it is not None
        if budget is not None:
            budget -= (ok + failed)

            

# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape match statistics from yallashoot concurrently and store in stats.json.\n"
            "Default: 8 parallel workers → ~6–8 min for 380 matches (vs ~60 min sequential)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python fetch_match_stats.py --competition PL\n"
            "  python fetch_match_stats.py --competition PL --workers 12\n"
            "  python fetch_match_stats.py --all --limit 200\n"
            "  python fetch_match_stats.py --competition WC --force\n"
        ),
    )

    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--competition", "-c", help="Competition code e.g. PL, WC")
    grp.add_argument(
        "--all", action="store_true", dest="all_competitions",
        help="Process all tracked competitions",
    )

    parser.add_argument("--season", "-s",
                        help="Season string e.g. 2025-2026 (overrides env SEASON)")
    parser.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS, metavar="N",
                        help=f"Parallel scrape threads (default: {DEFAULT_WORKERS})")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Max matches to scrape per run. Omit to scrape all pending.")
    parser.add_argument("--checkpoint", type=int, default=DEFAULT_CHECKPOINT_N, metavar="N",
                        help=f"Save every N successful scrapes (default: {DEFAULT_CHECKPOINT_N})")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even if stats already exist in stats.json.")

    args = parser.parse_args()
    season_override = args.season or os.environ.get("SEASON") or None

    if args.all_competitions:
        run_all_competitions_audit(
            force=args.force,
            limit=args.limit or 200,
            season_str=season_override,
            workers=args.workers,
            checkpoint_n=args.checkpoint,
        )
    else:
        run_competition_audit(
            args.competition,
            season_str=season_override,
            force=args.force,
            limit=args.limit,
            workers=args.workers,
            checkpoint_n=args.checkpoint,
        )


if __name__ == "__main__":
    main()