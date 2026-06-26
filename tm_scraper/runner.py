"""
tm_scraper/runner.py
────────────────────────────────────────────────────────────────────────────
Ultra-Fast, Multi-Level Concurrent Transfermarkt Scraper.

ID strategy
────────────
All scraped records use IDs from YOUR football-data.org data files so the
UI can navigate directly:

  player_id    – football-data.org player ID (from data/{season}/{league}/teams.json squad)
  team_id      – football-data.org team ID   (from same source)
  tm_player_id – Transfermarkt internal player ID (extracted from TM URL)
  tm_team_id   – Transfermarkt internal team ID   (extracted from TM URL)

The runner builds two global lookup indexes at start-up by scanning ALL
available data/{season}/{league}/teams.json files across every league and
season on disk, so even players who appear in multiple leagues or seasons
are resolved correctly.

Historical players (retired legends, pre-data-era) get player_id=null since
they are absent from the football-data squad lists; tm_player_id is always
populated when a TM URL is available.

Output layout:
  data/
  ├── league_info/
  │   └── {LEAGUE_CODE}/
  │       ├── league_metadata.json
  │       ├── top_scorers.json           ← all-time golden boot list (no slug)
  │       ├── successful_players.json
  │       ├── all_champions.json
  │       ├── championship_managers.json
  │       ├── market_values.json         ← always current; no season slug
  │       └── players_of_year.json
  ├── team_informations/
  │   └── {team_id}/
  │       ├── team_metadata.json
  │       └── {season}-squad.json
  └── player_information/
      └── {player_id}.json

Skip / force logic:
  Default run   — skips any JSON file that already exists AND has real data
                  (non-empty list / non-empty dict).  Empty files (caused by a
                  failed fetch that wrote []) are retried automatically.
                  Image assets are ALWAYS skipped if already on disk.
  --fullscrape  — re-fetches and overwrites every JSON data file regardless.
                  Image assets are still skipped (they never change).

Speed strategy:
  TEAM_WORKERS  concurrent teams
  PLAYER_WORKERS concurrent players per team
  Total max concurrency = TEAM_WORKERS × PLAYER_WORKERS
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from threading import Lock

from tm_scraper.config_tm import (
    BASE_DATA_DIR, PLAYER_IMAGES_DIR, TROPHY_IMAGES_DIR,
    LEAGUE_MAPPING, LEAGUE_INFO_DIR, TEAM_INFO_DIR, PLAYER_INFO_DIR,
    TM_BASE_URL, TEAM_ALIASES,
    league_url, league_metadata_url,
    league_top_scorers_url, league_successful_players_url,
    league_all_champions_url, league_championship_managers_url,
    league_market_values_url, league_players_of_year_url,
)
from tm_scraper.cf_fetcher import fetch_html
from tm_scraper.utils import safe_write
from tm_scraper.tm_parsers import (
    parse_league_metadata, parse_league_teams,
    parse_top_scorers, parse_successful_players,
    parse_all_champions, parse_championship_managers,
    parse_market_values, parse_players_of_year,
    parse_team_info, parse_squad_links, parse_trophies,
    extract_player_image, parse_player_full_info,
)

# ── Concurrency config ────────────────────────────────────────────────────────
TEAM_WORKERS   = 6    # concurrent teams
PLAYER_WORKERS = 12   # concurrent players per team

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("tm_runner")
logger.setLevel(logging.INFO)
for _noisy in ("cf_fetcher", "tm_scraper.utils", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_trophy_lock = Lock()


# ── Skip / force helpers ──────────────────────────────────────────────────────

def _file_has_data(path: Path) -> bool:
    """
    Return True if path exists AND contains real scraped data (not an empty
    list/dict that was written when a previous fetch failed or got CF-blocked).

    A file is considered empty / bad if:
      • it doesn't exist at all
      • the JSON "data" value is an empty list []
      • the JSON "data" value is an empty dict {}
      • the file is corrupt / unreadable
    """
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # safe_write wraps everything in {"_meta": …, "data": …}
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        if isinstance(data, list):
            return len(data) > 0
        if isinstance(data, dict):
            return len(data) > 0
        return data is not None
    except Exception:
        return False  # corrupt → re-fetch


# ── Global ID index builder ───────────────────────────────────────────────────

def build_id_indexes(season: str | None = None) -> tuple[dict[str, dict], dict[str, int]]:
    """
    Scan ALL data/{season}/{league}/teams.json files on disk and build:

      player_index: { lowercase_player_name -> {player_id, team_id, team_name} }
      team_index:   { lowercase_team_name   -> football_data_team_id }

    If season is given, that season's files are loaded first (highest priority),
    then all other seasons are scanned so historical squads can also be resolved.

    The team_index includes both full name and shortName variants so fuzzy
    matching in parsers is more reliable.
    """
    player_index: dict[str, dict] = {}
    team_index:   dict[str, int]  = {}

    def _load_file(path: Path) -> None:
        try:
            raw   = json.loads(path.read_text(encoding="utf-8"))
            teams = raw.get("data", raw) if isinstance(raw, dict) else raw
            if not isinstance(teams, list):
                return
            for team in teams:
                tid   = team.get("id")
                tname = team.get("name", "")
                tshort = team.get("shortName", "")
                tla   = team.get("tla", "")

                if tid and tname:
                    # Register every name variant
                    for variant in (tname, tshort, tla):
                        if variant:
                            team_index.setdefault(variant.lower().strip(), tid)
                    # Also strip "FC" / "AFC" suffix variants
                    import re
                    stripped = re.sub(r"\b(fc|afc|sc|cf)\b", "", tname.lower()).strip()
                    if stripped:
                        team_index.setdefault(stripped, tid)

                for p in team.get("squad", []):
                    pid  = p.get("id")
                    name = (p.get("name") or "").lower().strip()
                    if pid and name:
                        # Only set once — first occurrence wins (current season files load first)
                        player_index.setdefault(name, {
                            "player_id": pid,
                            "team_id":   tid,
                            "team_name": tname,
                        })
        except Exception as exc:
            logger.debug("build_id_indexes: skip %s (%s)", path, exc)

    # ── Priority 1: current season across all leagues ─────────────────────────
    if season:
        for league_code in LEAGUE_MAPPING:
            p = BASE_DATA_DIR / season / league_code / "teams.json"
            if p.exists():
                _load_file(p)

    # ── Priority 2: all other seasons / leagues ───────────────────────────────
    for teams_file in sorted(BASE_DATA_DIR.rglob("teams.json"), reverse=True):
        _load_file(teams_file)

    logger.info("[index] player_index: %d entries, team_index: %d entries",
                len(player_index), len(team_index))
    return player_index, team_index


# ── Binary / asset helpers ────────────────────────────────────────────────────

def _download_binary(url: str) -> bytes | None:
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.get(
            url, impersonate="chrome120", timeout=15,
            headers={"Referer": TM_BASE_URL + "/"},
        )
        if resp.status_code == 200:
            return resp.content
    except ImportError:
        import requests as _req
        try:
            resp = _req.get(
                url, timeout=15,
                headers={"User-Agent": "Mozilla/5.0", "Referer": TM_BASE_URL + "/"},
            )
            if resp.status_code == 200:
                return resp.content
        except Exception:
            pass
    except Exception as exc:
        logger.debug("Binary download failed for %s: %s", url, exc)
    return None


def download_asset(url: str, save_path: Path, use_lock: bool = False) -> str | None:
    if not url:
        return None
    if save_path.exists():
        return str(save_path.as_posix())
    if use_lock:
        with _trophy_lock:
            if save_path.exists():
                return str(save_path.as_posix())
            data = _download_binary(url)
            if data:
                save_path.write_bytes(data)
                return str(save_path.as_posix())
    else:
        data = _download_binary(url)
        if data:
            save_path.write_bytes(data)
            return str(save_path.as_posix())
    return None


# ── Name-matching helpers ─────────────────────────────────────────────────────

def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def match_name(target: str, options: dict, threshold: float = 0.55) -> tuple | None:
    """Fuzzy-match target against options dict keys. Returns (key, value) or None."""
    best_match, best_ratio = None, 0.0
    for key, val in options.items():
        ratio = _similar(target, key)
        if ratio > best_ratio and ratio >= threshold:
            best_ratio, best_match = ratio, (key, val)
    return best_match


# ── League info pipeline ──────────────────────────────────────────────────────

def _fetch_league_info(league_code: str,
                       season: str,
                       player_index: dict,
                       team_index:   dict,
                       force: bool = False) -> None:
    """
    Fetch and persist all league-level metadata pages in parallel.
    Saves to data/league_info/{league_code}/.
    Every output record is enriched with player_id / team_id from our data.

    Skip logic (per file):
      force=False  → skip if file exists AND has real data (non-empty list/dict).
                     Empty files from failed fetches are retried automatically.
      force=True   → always re-fetch and overwrite.
    """
    tm   = LEAGUE_MAPPING[league_code]
    name = tm["tm_name"]
    lid  = tm["tm_id"]

    out_dir = LEAGUE_INFO_DIR / league_code
    out_dir.mkdir(parents=True, exist_ok=True)

    def _task(filename: str, url: str, parser, parser_kwargs: dict | None = None) -> None:
        fpath = out_dir / filename

        if not force and _file_has_data(fpath):
            logger.info("  [skip-li] %s — already has data", filename)
            return

        if force and fpath.exists():
            logger.info("  [force] Re-fetching %s", filename)
        else:
            logger.info("  [league] Fetching %s …", filename)

        html = fetch_html(url)
        if not html:
            logger.warning("  [league] Could not fetch HTML for %s — skipping", url)
            return

        kwargs = parser_kwargs or {}
        result = parser(html, player_index=player_index, team_index=team_index, **kwargs)

        # Guard: don't write an empty result — it would mark the file as "done"
        # and the next normal run would skip it forever.
        if not result:
            logger.warning(
                "  [league] Parser returned empty result for %s — "
                "page may be CF-blocked or structure changed; not saving", filename
            )
            return

        safe_write(str(fpath), result)
        logger.info("  [league] ✓ Saved %s (%s records)",
                    filename, len(result) if isinstance(result, list) else "dict")

    tasks = [
        # (output filename,          URL,                                    parser fn)
        ("league_metadata.json",     league_metadata_url(name, lid),         parse_league_metadata),
        ("top_scorers.json",         league_top_scorers_url(name, lid),      parse_top_scorers),
        ("successful_players.json",  league_successful_players_url(name, lid), parse_successful_players),
        ("all_champions.json",       league_all_champions_url(name, lid),    parse_all_champions),
        ("championship_managers.json", league_championship_managers_url(name, lid), parse_championship_managers),
        ("market_values.json",       league_market_values_url(name, lid),    parse_market_values),
        ("players_of_year.json",     league_players_of_year_url(name, lid),  parse_players_of_year),
    ]

    with ThreadPoolExecutor(max_workers=7, thread_name_prefix="league_info") as pool:
        futures = [
            pool.submit(_task, fname, url, parser)
            for fname, url, parser in tasks
        ]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as exc:
                logger.error("  [league] Task error: %s", exc)

    logger.info("  [league] All league info pages done for %s", league_code)


# ── Per-player worker ─────────────────────────────────────────────────────────

def _scrape_player(p: dict, tm_players: dict, force: bool = False) -> dict | None:
    """Fetch, parse, and persist one player. Returns summary dict or None.

    force=False → skip if player JSON already exists and has real data.
    force=True  → re-fetch and overwrite the JSON; image assets always skipped.
    """
    p_id   = str(p.get("id", ""))
    p_name = p.get("name", "")
    if not p_id or not p_name:
        return None

    player_file = PLAYER_INFO_DIR / f"{p_id}.json"

    # Resume / skip: already processed and has real data
    if not force and _file_has_data(player_file):
        try:
            return json.loads(player_file.read_text(encoding="utf-8"))
        except Exception:
            pass  # corrupt → fall through and re-fetch

    # Match player name against TM squad
    p_match = match_name(p_name, tm_players)
    if not p_match:
        logger.debug("  [skip] No TM match for player: %s", p_name)
        return None

    _, p_uri = p_match
    p_url    = TM_BASE_URL + p_uri
    p_html   = fetch_html(p_url)
    if not p_html:
        return None

    player_details = parse_player_full_info(p_html)

    # Player image — always skip if already on disk (images don't change)
    img_local_path = None
    p_img_url = extract_player_image(p_html)
    if p_img_url:
        img_save = PLAYER_IMAGES_DIR / f"{p_id}.jpg"
        if download_asset(p_img_url, img_save):
            img_local_path = f"/assets/player_images/{p_id}.jpg"

    # Player trophies — trophy images also always skipped if on disk
    trophies_url = p_url.replace("/profil/", "/erfolge/")
    t_html       = fetch_html(trophies_url)
    p_trophies   = parse_trophies(t_html) if t_html else []
    for pt in p_trophies:
        if pt.get("source_url"):
            download_asset(
                pt["source_url"],
                TROPHY_IMAGES_DIR / f"{pt['safe_name']}.jpg",
                use_lock=True,
            )

    record = {
        "player_id":  p_id,
        "name":       p_name,
        "tm_url":     p_url,
        "image_path": img_local_path,
        "details":    player_details,
        "trophies":   p_trophies,
    }

    safe_write(str(player_file), record)
    logger.info("  [✓] Player: %s", p_name)
    return record


# ── Per-team orchestrator ─────────────────────────────────────────────────────

def _scrape_team(team: dict, tm_teams: dict, season: str, force: bool = False) -> None:
    """Fetch team metadata + all player profiles for one team.

    force=False → skip metadata/squad files that already have real data.
    force=True  → re-fetch and overwrite; trophy images always skipped.
    """
    team_id   = str(team.get("id", ""))
    team_name = team.get("shortName") or team.get("name", "Unknown")

    team_out_dir  = TEAM_INFO_DIR / team_id
    team_out_dir.mkdir(parents=True, exist_ok=True)
    squad_fname   = f"{season}-squad.json"
    metadata_file = team_out_dir / "team_metadata.json"
    squad_file    = team_out_dir / squad_fname

    metadata_done = _file_has_data(metadata_file)
    squad_done    = _file_has_data(squad_file)

    # Full skip: both files already have real data and we're not forcing
    if not force and metadata_done and squad_done:
        logger.info("[skip] %s — Team fully completed", team_name)
        return

    # Resolve team name via alias map + fuzzy matching
    search_name = TEAM_ALIASES.get(team_name, team_name)
    match = match_name(search_name, tm_teams)
    if not match:
        match = match_name(team_name, tm_teams)
    if not match:
        logger.warning("[-] Unmatched team: %s (Searched as: %s) — skipping",
                       team_name, search_name)
        return

    tm_team_name, tm_team_uri = match
    team_url  = TM_BASE_URL + tm_team_uri
    logger.info("[+] Starting Team: %-30s → %s", team_name, tm_team_name)

    # ── Team metadata ─────────────────────────────────────────────────────────
    if force or not metadata_done:
        team_html = fetch_html(team_url)
        if not team_html:
            logger.warning("  [-] Could not fetch team page for %s", team_name)
            return

        team_info = parse_team_info(team_html)

        trophies_url  = team_url.replace("/startseite/", "/erfolge/")
        t_html        = fetch_html(trophies_url)
        team_trophies = parse_trophies(t_html) if t_html else []
        # Trophy images: always skip if already on disk
        for t in team_trophies:
            if t.get("source_url"):
                download_asset(
                    t["source_url"],
                    TROPHY_IMAGES_DIR / f"{t['safe_name']}.jpg",
                )

        safe_write(
            str(metadata_file),
            {
                "team_id":  team_id,
                "tm_name":  tm_team_name,
                "tm_url":   team_url,
                "tm_stats": team_info,
                "trophies": team_trophies,
            },
        )
        tm_players = parse_squad_links(team_html)
    else:
        # Metadata exists — still need TM player links for squad scraping
        team_html  = fetch_html(team_url)
        tm_players = parse_squad_links(team_html) if team_html else {}

    # ── Squad ─────────────────────────────────────────────────────────────────
    if not force and squad_done:
        logger.info("[skip] %s — Squad already scraped", team_name)
        return

    squad = team.get("squad", [])
    if not squad:
        safe_write(str(squad_file), {"team_id": team_id, "season": season, "squad_roster": []})
        return

    # ── Concurrent player scraping ────────────────────────────────────────────
    player_summaries: list[dict] = []

    with ThreadPoolExecutor(
        max_workers=PLAYER_WORKERS, thread_name_prefix=f"plr_{team_id}"
    ) as pool:
        futures = {
            pool.submit(_scrape_player, p, tm_players, force): p
            for p in squad
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result and isinstance(result, dict) and "player_id" in result:
                    player_summaries.append({
                        "player_id": result["player_id"],
                        "name":      result["name"],
                        "position":  result.get("details", {}).get("position", "Unknown"),
                    })
            except Exception as exc:
                logger.error("  [CRITICAL] Player error: %s", exc, exc_info=True)

    player_summaries.sort(key=lambda x: int(x["player_id"]))

    safe_write(
        str(squad_file),
        {"team_id": team_id, "season": season, "squad_roster": player_summaries},
    )
    logger.info("[✓✓✓] COMPLETED: %s (%d players)", team_name, len(player_summaries))


# ── Main season pipeline ──────────────────────────────────────────────────────

def run_season(league_code: str, season: str, force: bool = False) -> None:
    logger.info("════════════════════════════════════════")
    logger.info(" Pipeline: %s  season: %s  force=%s", league_code, season, force)
    logger.info("════════════════════════════════════════")

    # ── 1. Build global ID indexes from all available data on disk ────────────
    logger.info("[index] Building player / team ID indexes …")
    player_index, team_index = build_id_indexes(season)

    # ── 2. League info pages (parallel, 7 requests) ───────────────────────────
    logger.info("[league] Fetching league info for %s …", league_code)
    _fetch_league_info(league_code, season, player_index, team_index, force=force)

    # ── 3. Load local team data ───────────────────────────────────────────────
    teams_json = BASE_DATA_DIR / season / league_code / "teams.json"
    if not teams_json.exists():
        logger.error("Local teams DB not found: %s", teams_json)
        return

    with open(teams_json, encoding="utf-8") as fh:
        raw         = json.load(fh)
        local_teams = raw.get("data", raw) if isinstance(raw, dict) else raw

    # ── 4. Fetch TM league page to build team → href mapping ─────────────────
    tm   = LEAGUE_MAPPING[league_code]
    year = season.split("-")[0]
    html = fetch_html(league_url(tm["tm_name"], tm["tm_id"], year))
    if not html:
        logger.error("Failed to fetch league teams page for %s", league_code)
        return

    tm_teams = parse_league_teams(html)
    logger.info("[league] TM teams found: %d", len(tm_teams))

    # ── 5. Concurrent team scraping ───────────────────────────────────────────
    with ThreadPoolExecutor(
        max_workers=TEAM_WORKERS, thread_name_prefix="team"
    ) as pool:
        futures = {
            pool.submit(_scrape_team, team, tm_teams, season, force): team
            for team in local_teams
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.error("[team] Unhandled error: %s", exc, exc_info=True)

    logger.info("════ Done: %s %s ════", league_code, season)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Transfermarkt scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Normal run — skip files that already have data, re-try any empties
  python -m tm_scraper.runner --season 2024-2025

  # Scrape specific leagues only
  python -m tm_scraper.runner --season 2024-2025 --leagues PL PD

  # Full re-scrape — overwrite all JSON data (images always skipped if on disk)
  python -m tm_scraper.runner --season 2024-2025 --fullscrape
        """,
    )
    parser.add_argument(
        "--season", default="2024-2025",
        help="Season string, e.g. 2024-2025",
    )
    parser.add_argument(
        "--leagues", nargs="+", default=list(LEAGUE_MAPPING.keys()),
        help="League codes to process, e.g. PD PL SA BL1 FL1",
    )
    parser.add_argument(
        "--fullscrape", action="store_true", default=False,
        help=(
            "Re-fetch and overwrite all JSON data files even if they already "
            "exist and have data. Image assets are always skipped if already "
            "on disk regardless of this flag."
        ),
    )
    args = parser.parse_args()

    if args.fullscrape:
        logger.info("⚠  --fullscrape mode: all JSON data files will be re-fetched and overwritten")
        logger.info("   Image assets on disk will still be skipped")

    for code in args.leagues:
        if code not in LEAGUE_MAPPING:
            logger.error("Unknown league code: %s  (valid: %s)", code, list(LEAGUE_MAPPING))
            continue
        run_season(code, args.season, force=args.fullscrape)