"""
tm_scraper/cup_parsers.py
──────────────────────────────────────────────────────────────────────────────
Parsers specific to cup competitions (CL / EURO / World Cup) and the FIFA
world-ranking page (used as the national-team roster source).

Deliberately thin: parse_market_values, parse_top_scorers, parse_team_info,
parse_squad_links, parse_trophies, parse_player_full_info, and
extract_player_image from tm_parsers.py are reused UNCHANGED — they only
depend on TM's site-wide table.items / hauptlink / data-header__* markup
conventions, which hold for cup and national-team pages too. Only the
handful of things that are genuinely page-type-specific live here.

Note: these functions do NOT resolve player_id/team_id — unlike the league
parsers, a cup entity might not have a football-data id at all, and ID
resolution now depends on a live, thread-safe registry (id_registry.py).
That resolution happens in cup_runner.py, not here. These functions just
return names + TM hrefs + tm_player_id/tm_team_id (always derivable from
the URL itself, no external lookup needed).
"""

from __future__ import annotations

import re

from tm_scraper.tm_parsers import _soup, _clean, _tm_team_id


# ── Cup participants ──────────────────────────────────────────────────────────

def parse_cup_participants(html: str) -> dict:
    """
    Parse the 'teilnehmer' (participating teams) page for a cup competition.

    TM splits this page into two tables — teams still in the competition and
    teams already eliminated — so (unlike parse_league_teams, which only
    looks at the first table.items on the page) this walks EVERY table.items
    on the page and merges them into one { team_name: href_slug } dict.

    Returns the same shape as tm_parsers.parse_league_teams so it's a
    drop-in fit for the existing match_name()-based team-matching flow.
    """
    soup = _soup(html)
    teams = {}
    for table in soup.find_all("table", class_="items"):
        for row in table.find_all("tr", class_=["odd", "even"]):
            td = row.find("td", class_="hauptlink")
            if td and td.a:
                name = _clean(td.a.get_text())
                href = td.a.get("href", "")
                if name and href:
                    teams[name] = href
    return teams


# ── All-time top scorers (cup only — no season column) ────────────────────────

def parse_cup_alltime_top_scorers(html: str) -> list[dict]:
    """
    Parse the 'ewigetorschuetzenliste' (all-time top scorers) page for a cup
    competition. Ranked list, one row per player, no season column (unlike
    the per-edition torschuetzenliste, which tm_parsers.parse_top_scorers
    already handles since it shares the league version's markup).
    """
    soup = _soup(html)
    results = []
    table = soup.find("table", class_="items")
    if not table:
        return results

    for rank, row in enumerate(table.find_all("tr", class_=["odd", "even"]), 1):
        entry: dict = {"rank": rank}

        inline = row.find("table", class_="inline-table")
        if inline:
            a = inline.find("a")
            if a:
                entry["player_name"] = _clean(a.get_text())
                entry["player_url"] = a.get("href")
                m = re.search(r"/spieler/(\d+)", entry["player_url"] or "")
                entry["tm_player_id"] = m.group(1) if m else None

        flag = row.find("img", class_="flaggenrahmen")
        if flag:
            entry["nationality"] = flag.get("title", "")

        for td in row.find_all("td"):
            a = td.find("a")
            if a and "/verein/" in (a.get("href") or ""):
                entry["club_name"] = _clean(a.get("title") or a.get_text())
                entry["club_url"] = a.get("href")
                entry["tm_team_id"] = _tm_team_id(entry["club_url"])
                break

        goals_td = row.find("td", class_="zentriert hauptlink") or row.find("td", class_="rechts hauptlink")
        if goals_td:
            entry["goals"] = _clean(goals_td.get_text())

        if entry.get("player_name"):
            results.append(entry)

    return results


# ── National-team FIFA ranking list ───────────────────────────────────────────

def parse_national_ranking_page(html: str) -> list[dict]:
    """
    Parse one page of /statistik/weltrangliste (FIFA world ranking).
    Returns a list of { rank, name, tm_url, tm_team_id, squad_size, avg_age,
    total_value, confederation, points }.

    tm_url is the team's ('verein') page — the same kind of URL a club has —
    so it can be handed straight to tm_parsers.parse_team_info /
    parse_squad_links, same as any club team page.
    """
    soup = _soup(html)
    results = []
    table = soup.find("table", class_="items")
    if not table:
        return results

    for row in table.find_all("tr", class_=["odd", "even"]):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        rank_txt = _clean(cells[0].get_text())
        m = re.match(r"^(\d+)", rank_txt)
        rank = int(m.group(1)) if m else None

        name_td = cells[1]
        a = name_td.find_all("a")[-1] if name_td.find_all("a") else None
        if not a:
            continue
        name = _clean(a.get_text())
        href = a.get("href", "")

        entry = {
            "rank": rank,
            "name": name,
            "tm_url": href,
            "tm_team_id": _tm_team_id(href),
            "squad_size": _clean(cells[2].get_text()) if len(cells) > 2 else "",
            "avg_age": _clean(cells[3].get_text()) if len(cells) > 3 else "",
            "total_value": _clean(cells[4].get_text()) if len(cells) > 4 else "",
            "confederation": _clean(cells[5].get_text()) if len(cells) > 5 else "",
            "points": _clean(cells[6].get_text()) if len(cells) > 6 else "",
        }
        if entry["name"] and entry["tm_team_id"]:
            results.append(entry)

    return results


def get_last_ranking_page(html: str) -> int:
    """Read the pager on a weltrangliste page and return the last page number."""
    soup = _soup(html)
    pages = [0]
    for a in soup.select("ul.tm-pagination a.tm-pagination__link"):
        txt = _clean(a.get_text())
        if txt.isdigit():
            pages.append(int(txt))
    return max(pages) or 1