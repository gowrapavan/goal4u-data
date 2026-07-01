"""
tm_scraper/cup_runner.py
────────────────────────────────────────────────────────────────────────────
Orchestrator for the three cup competitions (Champions League, Euro, World
Cup) plus a national-team roster scraper. Sibling to runner.py, reusing its
helpers rather than duplicating them.

Scope, by design (per explicit request): competition info + team info +
player info only. NO match schedules, NO group-stage tables, NO bracket.

STORAGE — reuses the exact same folders runner.py already writes to
────────────────────────────────────────────────────────────────────
There are NO new top-level data folders. Everything lands in:

    data/league_info/{CODE}/{season_label}/market_values.json
    data/league_info/{CODE}/{season_label}/top_scorers.json
    data/league_info/{CODE}/top_scorers_all_time.json        (no season — spans every edition)
    data/team_informations/{team_id}/team_metadata.json       (shared/reused)
    data/team_informations/{team_id}/{competition_slug}-{season_label}.json   (squad)
    data/player_information/{player_id}.json                  (shared/reused)
    public/assets/player_images/{player_id}.jpg                (shared/reused)
    public/assets/trophies/{safe_name}.jpg                     (shared/reused)

CODE is "CL" / "EURO" / "WC" — sits next to "GB1" / "ES1" / etc. in
league_info/, same shape.

ID strategy — reuse first, synthesize only as a last resort
─────────────────────────────────────────────────────────────
1. Build player_index / team_index from ALL data/{season}/{league}/teams.json
   files on disk (runner.build_id_indexes) — the real football-data IDs.
2. For every cup team/player, try to match its name against that index
   first. A club that's ALSO in one of the 5 tracked leagues (e.g. Real
   Madrid in the Champions League) resolves to its EXISTING
   team_informations/{id}/ folder — no duplicate folder is ever created,
   and its existing team_metadata.json is reused as-is rather than
   re-fetched.
3. Only if no football-data match exists (national teams; clubs outside
   the 5 tracked leagues) does id_registry.py hand out a synthetic id —
   and it's permanent, persisted to data/id_registry.json, so the same
   entity always resolves to the same folder in every future run and every
   competition. This is what makes "Portugal" share ONE
   team_informations/{id}/ folder across both the World Cup and the Euro,
   with two different squad files inside it:
       team_informations/1000001/world-cup-2026.json
       team_informations/1000001/euro-2024.json

Usage:
    python -m tm_scraper.cup_runner --competition CL   --season 2025
    python -m tm_scraper.cup_runner --competition EURO --season 2023
    python -m tm_scraper.cup_runner --competition WC   --season 2021
    python -m tm_scraper.cup_runner --national-teams
"""

from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tm_scraper.config_cup import (
    CUP_MAPPING, CUP_SLUG, ID_REGISTRY_PATH, cup_season_label,
    cup_participants_url, cup_market_values_url,
    cup_top_scorers_url, cup_top_scorers_alltime_url,
    national_ranking_url,
)
from tm_scraper.config_tm import (
    TM_BASE_URL, LEAGUE_INFO_DIR, TEAM_INFO_DIR, PLAYER_INFO_DIR,
    PLAYER_IMAGES_DIR, TROPHY_IMAGES_DIR,
)
from tm_scraper.cf_fetcher import fetch_html
from tm_scraper.utils import safe_write
from tm_scraper.tm_parsers import (
    parse_market_values, parse_top_scorers,
    parse_team_info, parse_squad_links, parse_trophies,
    parse_player_full_info, extract_player_image,
    _resolve_team_id,
)
from tm_scraper.cup_parsers import (
    parse_cup_participants, parse_cup_alltime_top_scorers,
    parse_national_ranking_page, get_last_ranking_page,
)
from tm_scraper.runner import _file_has_data, download_asset, build_id_indexes
from tm_scraper.id_registry import IdRegistry

TEAM_WORKERS = 6
PLAYER_WORKERS = 10

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("cup_runner")
logger.setLevel(logging.INFO)


# ── ID resolution: football-data index first, registry only as fallback ──────

def _resolve_or_assign_team_id(name: str, team_index: dict, registry: IdRegistry) -> int:
    tid = _resolve_team_id(name, team_index)
    if tid is not None:
        return tid
    return registry.get_or_create_team_id(name)


def _resolve_or_assign_player_id(name: str, player_index: dict, registry: IdRegistry) -> int:
    pid = player_index.get(name.lower().strip(), {}).get("player_id")
    if pid is not None:
        return pid
    return registry.get_or_create_player_id(name)


# ── Competition-level info → data/league_info/{CODE}/... ─────────────────────

def run_cup_info(code: str, season_id: str | None,
                  player_index: dict, team_index: dict,
                  force: bool = False) -> None:
    cfg = CUP_MAPPING[code]
    name, tm_id = cfg["tm_name"], cfg["tm_id"]
    season_label = cup_season_label(code, season_id)

    out_dir = LEAGUE_INFO_DIR / code / season_label
    out_dir.mkdir(parents=True, exist_ok=True)
    alltime_dir = LEAGUE_INFO_DIR / code
    alltime_dir.mkdir(parents=True, exist_ok=True)

    tasks = [
        (out_dir / "market_values.json", cup_market_values_url(name, tm_id, season_id), parse_market_values),
        (out_dir / "top_scorers.json", cup_top_scorers_url(name, tm_id, season_id), parse_top_scorers),
        (alltime_dir / "top_scorers_all_time.json", cup_top_scorers_alltime_url(name, tm_id), parse_cup_alltime_top_scorers),
    ]

    for fpath, url, parser in tasks:
        if not force and _file_has_data(fpath):
            logger.info("[skip-info] %s already has data", fpath)
            continue

        html = fetch_html(url)
        if not html:
            logger.warning("[info] Could not fetch %s — skipping", url)
            continue

        if parser is parse_cup_alltime_top_scorers:
            result = parser(html)
        else:
            result = parser(html, player_index=player_index, team_index=team_index)

        if not result:
            logger.warning("[info] Parser returned empty for %s — page may be blocked or structure changed", fpath)
            continue

        safe_write(str(fpath), result)
        logger.info("[info] Saved %s (%d records)", fpath, len(result))


# ── Player scraping → data/player_information/{player_id}.json (shared) ──────

def _scrape_player(p_name: str, p_href: str,
                    player_index: dict, registry: IdRegistry,
                    force: bool = False) -> dict | None:
    p_id = _resolve_or_assign_player_id(p_name, player_index, registry)
    player_file = PLAYER_INFO_DIR / f"{p_id}.json"

    if not force and _file_has_data(player_file):
        try:
            raw = json.loads(player_file.read_text(encoding="utf-8"))
            # safe_write() wraps payloads as {"_meta": ..., "data": {...}} —
            # unwrap it, or a cache-hit returns the envelope instead of the
            # actual record and rec["player_id"] below raises KeyError.
            cached = raw.get("data", raw) if isinstance(raw, dict) else raw
            if isinstance(cached, dict) and "player_id" in cached:
                return cached
        except Exception:
            pass  # corrupt → fall through and re-fetch

    p_url = TM_BASE_URL + p_href
    p_html = fetch_html(p_url)
    if not p_html:
        return None

    details = parse_player_full_info(p_html)

    img_local = None
    img_url = extract_player_image(p_html)
    if img_url:
        img_save = PLAYER_IMAGES_DIR / f"{p_id}.jpg"
        if download_asset(img_url, img_save):
            img_local = f"/assets/player_images/{p_id}.jpg"

    trophies_url = p_url.replace("/profil/", "/erfolge/")
    t_html = fetch_html(trophies_url)
    trophies = parse_trophies(t_html) if t_html else []
    for t in trophies:
        if t.get("source_url"):
            download_asset(t["source_url"], TROPHY_IMAGES_DIR / f"{t['safe_name']}.jpg", use_lock=True)

    record = {
        "player_id": p_id,
        "name": p_name,
        "tm_url": p_url,
        "image_path": img_local,
        "details": details,
        "trophies": trophies,
    }
    safe_write(str(player_file), record)
    logger.info("  [✓] Player: %s", p_name)
    return record


# ── Team scraping → data/team_informations/{team_id}/... (shared) ────────────

def _scrape_team(team_name: str, team_href: str,
                  team_index: dict, player_index: dict, registry: IdRegistry,
                  code: str, season_label: str,
                  force: bool = False) -> None:
    team_id = _resolve_or_assign_team_id(team_name, team_index, registry)
    team_out_dir = TEAM_INFO_DIR / str(team_id)
    team_out_dir.mkdir(parents=True, exist_ok=True)

    metadata_file = team_out_dir / "team_metadata.json"
    squad_fname = f"{CUP_SLUG[code]}-{season_label}.json"
    squad_file = team_out_dir / squad_fname

    if not force and _file_has_data(metadata_file) and _file_has_data(squad_file):
        logger.info("[skip] %s — %s already complete", team_name, squad_fname)
        return

    team_url = TM_BASE_URL + team_href
    team_html = fetch_html(team_url)
    if not team_html:
        logger.warning("[-] Could not fetch team page for %s", team_name)
        return

    if force or not _file_has_data(metadata_file):
        team_info = parse_team_info(team_html)
        trophies_url = team_url.replace("/startseite/", "/erfolge/")
        t_html = fetch_html(trophies_url)
        trophies = parse_trophies(t_html) if t_html else []
        for t in trophies:
            if t.get("source_url"):
                download_asset(t["source_url"], TROPHY_IMAGES_DIR / f"{t['safe_name']}.jpg")
        safe_write(str(metadata_file), {
            "team_id": team_id,
            "name": team_name,
            "tm_url": team_url,
            "tm_stats": team_info,
            "trophies": trophies,
        })
    else:
        # Metadata already exists — very likely this club was already
        # scraped via the league pipeline (runner.py). Reused as-is.
        logger.info("[reuse] %s metadata already exists (team_id=%s) — not re-fetched", team_name, team_id)

    if not force and _file_has_data(squad_file):
        logger.info("[skip] %s — %s already scraped", team_name, squad_fname)
        return

    squad_links = parse_squad_links(team_html)  # { player_name: href }
    roster: list[dict] = []

    with ThreadPoolExecutor(max_workers=PLAYER_WORKERS, thread_name_prefix=f"cup_plr_{team_id}") as pool:
        futures = {
            pool.submit(_scrape_player, name, href, player_index, registry, force): name
            for name, href in squad_links.items()
        }
        for fut in as_completed(futures):
            try:
                rec = fut.result()
                if rec and isinstance(rec, dict) and "player_id" in rec:
                    roster.append({
                        "player_id": rec["player_id"],
                        "name": rec.get("name", ""),
                        "position": rec.get("details", {}).get("position", "Unknown"),
                    })
                elif rec is not None:
                    logger.warning("  [-] Skipping malformed cached record under %s", team_name)
            except Exception as exc:
                logger.error("  [CRITICAL] Player error under %s: %s", team_name, exc, exc_info=True)

    roster.sort(key=lambda x: int(x["player_id"]) if x.get("player_id") is not None else 0)
    safe_write(str(squad_file), {
        "team_id": team_id,
        "competition": code,
        "season": season_label,
        "squad_roster": roster,
    })
    logger.info("[✓✓✓] COMPLETED: %s → %s (%d players)", team_name, squad_fname, len(roster))


# ── Cup team/squad pipeline ───────────────────────────────────────────────────

def run_cup_teams(code: str, season_id: str | None,
                   player_index: dict, team_index: dict, registry: IdRegistry,
                   force: bool = False) -> None:
    cfg = CUP_MAPPING[code]
    name, tm_id = cfg["tm_name"], cfg["tm_id"]
    season_label = cup_season_label(code, season_id)

    html = fetch_html(cup_participants_url(name, tm_id, season_id))
    if not html:
        logger.error("Failed to fetch participants page for %s %s", code, season_label)
        return

    teams = parse_cup_participants(html)
    logger.info("[%s %s] Participating teams found: %d", code, season_label, len(teams))
    if not teams:
        logger.warning(
            "[%s %s] 0 teams parsed — check cup_parsers.parse_cup_participants "
            "against the live page structure before trusting this run.", code, season_label,
        )
        return

    with ThreadPoolExecutor(max_workers=TEAM_WORKERS, thread_name_prefix=f"cup_team_{code}") as pool:
        futures = {
            pool.submit(_scrape_team, tname, href, team_index, player_index, registry, code, season_label, force): tname
            for tname, href in teams.items()
        }
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                logger.error("[team] Unhandled error: %s", exc, exc_info=True)


def run_cup(code: str, season_id: str | None, force: bool = False) -> None:
    season_label = cup_season_label(code, season_id)
    logger.info("════════════════════════════════════════")
    logger.info(" Cup pipeline: %s  season: %s  force=%s", code, season_label, force)
    logger.info("════════════════════════════════════════")

    logger.info("[index] Building player/team ID indexes from existing football-data teams.json files …")
    player_index, team_index = build_id_indexes(season=None)
    registry = IdRegistry(ID_REGISTRY_PATH)

    run_cup_info(code, season_id, player_index, team_index, force=force)
    run_cup_teams(code, season_id, player_index, team_index, registry, force=force)

    logger.info("════ Done: %s %s ════", code, season_label)


# ── National teams (FIFA ranking → squads) ────────────────────────────────────
# Standalone utility, not tied to one tournament edition: pulls the FULL
# current national-team roster for every country TM tracks. Useful as a
# fallback source of national-team entities (e.g. before a World Cup's
# teilnehmer page exists for a not-yet-qualified field) or just to seed
# every country once. Squad content is saved as "national-team-current.json"
# inside the same team_informations/{team_id}/ folder that a World Cup or
# Euro run for that same country would also write into (via _resolve_or_
# assign_team_id, so the id always matches).

def run_national_teams(force: bool = False) -> None:
    logger.info("[index] Building player/team ID indexes …")
    player_index, team_index = build_id_indexes(season=None)
    registry = IdRegistry(ID_REGISTRY_PATH)

    first_html = fetch_html(national_ranking_url(1))
    if not first_html:
        logger.error("Failed to fetch weltrangliste page 1")
        return

    last_page = get_last_ranking_page(first_html)
    logger.info("[national] %d ranking pages to scan", last_page)

    all_teams: dict[str, dict] = {}
    for entry in parse_national_ranking_page(first_html):
        all_teams[entry["tm_url"]] = entry

    for page in range(2, last_page + 1):
        html = fetch_html(national_ranking_url(page))
        if not html:
            logger.warning("[national] Could not fetch ranking page %d — skipping", page)
            continue
        for entry in parse_national_ranking_page(html):
            all_teams[entry["tm_url"]] = entry

    logger.info("[national] Total national teams found: %d", len(all_teams))

    with ThreadPoolExecutor(max_workers=TEAM_WORKERS, thread_name_prefix="natl_team") as pool:
        futures = {
            pool.submit(_scrape_team, entry["name"], entry["tm_url"],
                        team_index, player_index, registry, "NT", "current", force): entry["name"]
            for entry in all_teams.values()
        }
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                logger.error("[national] Unhandled error: %s", exc, exc_info=True)


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cup competition + national team scraper")
    parser.add_argument("--competition", choices=list(CUP_MAPPING.keys()), help="CL, EURO, or WC")
    parser.add_argument("--season", help="TM saison_id, e.g. 2025 for CL 25/26, 2023 for Euro 2024, "
                                          "2021 for World Cup 2022")
    parser.add_argument("--national-teams", action="store_true",
                         help="Scrape all national-team rosters from the FIFA ranking list")
    parser.add_argument("--fullscrape", action="store_true", default=False)
    args = parser.parse_args()

    if args.national_teams:
        run_national_teams(force=args.fullscrape)
    elif args.competition and args.season:
        run_cup(args.competition, args.season, force=args.fullscrape)
    else:
        parser.error("Either --national-teams, or both --competition and --season, are required.")