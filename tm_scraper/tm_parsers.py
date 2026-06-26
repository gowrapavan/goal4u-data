"""
tm_scraper/tm_parsers.py
────────────────────────────────────────────────────────────────────────────
All BeautifulSoup parsers for Transfermarkt pages.

ID enrichment convention
─────────────────────────
Every parser that returns player or club entries now includes two ID fields:

  player_id  – football-data.org player ID (integer), matched by name from
               the caller's player_index dict.  None if not found (historical
               players, retired legends, etc.).
  tm_player_id – Transfermarkt-internal player ID extracted from the
               /profil/spieler/<ID> URL segment.  Always present when a URL
               is available.

  team_id    – football-data.org team ID (integer), matched by name from the
               caller's team_index dict.  None if not found.
  tm_team_id – Transfermarkt-internal team ID extracted from the
               /startseite/verein/<ID> URL segment.  Always present when a
               URL is available.

Callers (runner.py) build player_index and team_index from all available
data/{season}/{league}/teams.json files and pass them in via the
`enrich(data, player_index, team_index)` helper at the bottom of this file.
"""

from __future__ import annotations

import re
from bs4 import BeautifulSoup, Tag


# ── Internal helpers ──────────────────────────────────────────────────────────

def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")

def _clean(text: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", text or "").strip()

def _text(tag: Tag | None) -> str:
    return _clean(tag.get_text(" ", strip=True)) if tag else ""

def _tm_player_id(url: str | None) -> str | None:
    """Extract TM player ID from /profil/spieler/418560 → '418560'."""
    if not url:
        return None
    m = re.search(r"/spieler/(\d+)", url)
    return m.group(1) if m else None

def _tm_team_id(url: str | None) -> str | None:
    """Extract TM team ID from /startseite/verein/281... → '281'."""
    if not url:
        return None
    m = re.search(r"/verein/(\d+)", url)
    return m.group(1) if m else None


# ── ID enrichment helper ──────────────────────────────────────────────────────

def enrich_player(entry: dict,
                  player_index: dict[str, dict],
                  team_index:   dict[str, int]) -> dict:
    """
    Add player_id, tm_player_id, team_id, tm_team_id to a single entry dict
    that already has player_url and (optionally) club_url / club_name fields.
    Mutates entry in-place and returns it.
    """
    # ── TM IDs (always extractable from URL) ─────────────────────────────────
    entry["tm_player_id"] = _tm_player_id(entry.get("player_url"))
    entry["tm_team_id"]   = _tm_team_id(entry.get("club_url") or entry.get("champion_url"))

    # ── football-data.org player ID ───────────────────────────────────────────
    raw_name = entry.get("player_name") or entry.get("manager_name") or ""
    entry["player_id"] = player_index.get(raw_name.lower(), {}).get("player_id")

    # ── football-data.org team ID ─────────────────────────────────────────────
    club_raw = (
        entry.get("club_name")
        or entry.get("champion_name")
        or entry.get("team_name")
        or ""
    )
    entry["team_id"] = _resolve_team_id(club_raw, team_index)

    return entry


def _resolve_team_id(name: str, team_index: dict[str, int]) -> int | None:
    """Exact-match then fuzzy-match a team name against team_index."""
    if not name:
        return None
    key = name.lower().strip()
    if key in team_index:
        return team_index[key]
    # Try stripping common suffixes (FC, AFC, SC …)
    stripped = re.sub(r"\b(fc|afc|sc|cf)\b", "", key).strip()
    if stripped in team_index:
        return team_index[stripped]
    # Fuzzy: find best overlap
    best_id, best_ratio = None, 0.0
    for k, v in team_index.items():
        shorter, longer = sorted([key, k], key=len)
        if shorter and shorter in longer:
            ratio = len(shorter) / len(longer)
        else:
            common = sum(1 for a, b in zip(key, k) if a == b)
            ratio  = common / max(len(key), len(k), 1)
        if ratio > best_ratio and ratio > 0.70:
            best_ratio, best_id = ratio, v
    return best_id


# ── League-level parsers ──────────────────────────────────────────────────────

def parse_league_metadata(html: str,
                          player_index: dict | None = None,
                          team_index:   dict | None = None) -> dict:
    """
    Parse the league overview / startseite page.
    Returns header stats with IDs enriched where possible.
    """
    soup = _soup(html)
    info: dict = {}
    pi = player_index or {}
    ti = team_index   or {}

    # ── Data header ──────────────────────────────────────────────────────────
    header = soup.find("div", class_="data-header__info-box")
    if header:
        for li in header.find_all("li", class_="data-header__label"):
            label = _clean(li.get_text(" "))
            cont  = li.find("span", class_="data-header__content")
            if "Number of teams" in label:
                info["number_of_teams"] = _clean(cont.get_text()) if cont else ""
            elif "Players:" in label:
                info["players"] = _clean(cont.get_text()) if cont else ""
            elif "Foreigners" in label:
                info["foreigners"] = _clean(cont.get_text()) if cont else ""
            elif "ø-Market value" in label or "Market value" in label.lower():
                info["avg_market_value"] = _clean(cont.get_text()) if cont else ""
            elif "Age" in label:
                info["avg_age"] = _clean(cont.get_text()) if cont else ""
            elif "Most valuable" in label and cont:
                a = cont.find("a")
                p_url = a.get("href") if a else None
                p_name = _text(a)
                info["most_valuable_player"] = {
                    "name":         p_name,
                    "value":        _clean(cont.find_all("span")[-1].get_text()) if cont.find_all("span") else "",
                    "url":          p_url,
                    "player_id":    pi.get((p_name or "").lower(), {}).get("player_id"),
                    "tm_player_id": _tm_player_id(p_url),
                }

    # ── Total market value ────────────────────────────────────────────────────
    mv_box = soup.find("div", class_="data-header__box--small")
    if mv_box:
        mv_a = mv_box.find("a", class_="data-header__market-value-wrapper")
        if mv_a:
            info["total_market_value"] = _clean(mv_a.get_text(" ").split("Total")[0])

    # ── Reigning / record champion ────────────────────────────────────────────
    big_box = soup.find("div", class_="data-header__box--big")
    if big_box:
        club_infos = big_box.find("div", class_="data-header__club-info")
        if club_infos:
            for span in club_infos.find_all("span", class_="data-header__label"):
                label = _clean(span.get_text(" "))
                cont  = span.find("span", class_="data-header__content")
                if "Reigning champion" in label and cont:
                    a = cont.find("a")
                    t_url  = a.get("href") if a else None
                    t_name = _text(a)
                    info["reigning_champion"] = {
                        "name":       t_name,
                        "url":        t_url,
                        "team_id":    _resolve_team_id(t_name, ti),
                        "tm_team_id": _tm_team_id(t_url),
                    }
                elif "Record" in label and cont:
                    a      = cont.find("a")
                    spans  = cont.find_all("span", class_="data-header__content")
                    t_url  = a.get("href") if a else None
                    t_name = _text(a)
                    info["record_champion"] = {
                        "name":       t_name,
                        "titles":     _clean(spans[0].get_text()) if spans else "",
                        "url":        t_url,
                        "team_id":    _resolve_team_id(t_name, ti),
                        "tm_team_id": _tm_team_id(t_url),
                    }
                elif "UEFA coefficient" in label and cont:
                    a_tag = cont.find("a")
                    spans = cont.find_all("span", class_="data-header__content")
                    info["uefa_coefficient"] = {
                        "position": _clean(a_tag.get_text()) if a_tag else "",
                        "points":   _clean(spans[0].get_text()) if spans else "",
                    }

    # ── League level (tier) ───────────────────────────────────────────────────
    for sp in soup.find_all("span", class_="data-header__content"):
        if "Tier" in _text(sp):
            info["league_level"] = _clean(sp.get_text())
            break

    return info


def parse_league_teams(html: str) -> dict:
    """
    Parse the league teams table (startseite page).
    Returns { team_tm_name: href_slug }
    """
    soup  = _soup(html)
    teams = {}
    table = soup.find("table", class_="items")
    if not table:
        return teams
    for row in table.find_all("tr", class_=["odd", "even"]):
        td = row.find("td", class_="hauptlink")
        if td and td.a:
            name = _clean(td.a.get_text())
            href = td.a.get("href", "")
            if name and href:
                teams[name] = href
    return teams


def parse_top_scorers(html: str,
                      player_index: dict | None = None,
                      team_index:   dict | None = None) -> list[dict]:
    """
    Parse the top goalscorers page (torschuetzenkoenige).
    Returns list with player_id, tm_player_id, team_id, tm_team_id added.
    """
    soup    = _soup(html)
    results = []
    pi      = player_index or {}
    ti      = team_index   or {}
    table   = soup.find("table", class_="items")
    if not table:
        return results

    for row in table.find_all("tr", class_=["odd", "even"]):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        entry: dict = {}
        entry["season"] = _clean(cells[0].get_text())

        player_td = row.find("td", class_=lambda c: c and "hauptlink" in c and "no-border-links" not in c)
        if not player_td:
            player_td = row.find("td", class_="hauptlink")
        if player_td and player_td.a:
            p_name = _clean(player_td.a.get_text())
            p_url  = player_td.a.get("href")
            entry["player_name"]   = p_name
            entry["player_url"]    = p_url
            entry["player_id"]     = pi.get(p_name.lower(), {}).get("player_id")
            entry["tm_player_id"]  = _tm_player_id(p_url)
            pos_td = player_td.find_next("td")
            if pos_td and not pos_td.find("a"):
                entry["position"] = _clean(pos_td.get_text())

        for td in cells:
            a = td.find("a")
            if a and "/startseite/verein/" in (a.get("href") or ""):
                c_name = _clean(a.get("title") or a.get_text())
                c_url  = a.get("href")
                entry["club_name"]  = c_name
                entry["club_url"]   = c_url
                entry["team_id"]    = _resolve_team_id(c_name, ti)
                entry["tm_team_id"] = _tm_team_id(c_url)
                break

        goals_td = (row.find("td", class_="zentriert hauptlink")
                    or row.find("td", class_="rechts hauptlink"))
        if goals_td:
            entry["goals"] = _clean(goals_td.get_text())

        flag_img = row.find("img", class_="flaggenrahmen")
        if flag_img:
            entry["nationality"] = flag_img.get("title", "")

        if entry.get("player_name"):
            results.append(entry)

    return results


def parse_successful_players(html: str,
                              player_index: dict | None = None,
                              team_index:   dict | None = None) -> list[dict]:
    """
    Parse the 'Most successful players' page (erfolgreichstespieler).
    Returns list with player_id, tm_player_id added.
    Historical legends will have player_id=None.
    """
    soup    = _soup(html)
    results = []
    pi      = player_index or {}
    table   = soup.find("table", class_="items")
    if not table:
        return results

    for rank, row in enumerate(table.find_all("tr", class_=["odd", "even"]), 1):
        entry: dict = {"rank": rank}

        inline = row.find("table", class_="inline-table")
        if inline:
            a = inline.find("a")
            if a:
                p_name = _clean(a.get_text())
                p_url  = a.get("href")
                entry["player_name"]   = p_name
                entry["player_url"]    = p_url
                entry["player_id"]     = pi.get(p_name.lower(), {}).get("player_id")
                entry["tm_player_id"]  = _tm_player_id(p_url)
            tds = inline.find_all("td")
            for td in tds:
                txt = _clean(td.get_text())
                if txt and not td.find("a") and not td.find("img"):
                    entry["position"] = txt
                    break

        flag = row.find("img", class_="flaggenrahmen")
        if flag:
            entry["nationality"] = flag.get("title", "")

        teams_td = row.find("td", class_="zentriert")
        if teams_td and teams_td.a:
            entry["teams_with_titles"] = _clean(teams_td.a.get_text())

        title_td = row.find("td", class_="zentriert hauptlink")
        if title_td and title_td.a:
            entry["total_titles"] = _clean(title_td.a.get_text())

        if entry.get("player_name"):
            results.append(entry)

    return results


def parse_all_champions(html: str,
                        player_index: dict | None = None,
                        team_index:   dict | None = None) -> list[dict]:
    """
    Parse the 'All champions' page (alle-meister).
    Returns list with team_id, tm_team_id added to each champion entry.
    """
    soup    = _soup(html)
    results = []
    ti      = team_index or {}
    table   = soup.find("table", class_="items")
    if not table:
        return results

    headers = [_clean(th.get_text()) for th in table.find_all("th")]

    for row in table.find_all("tr", class_=["odd", "even"]):
        cells = row.find_all("td")
        if not cells:
            continue
        entry: dict = {}
        entry["season"] = _clean(cells[0].get_text()) if cells else ""

        for td in cells:
            a = td.find("a")
            if a and "/startseite/verein/" in (a.get("href") or ""):
                t_name = _clean(a.get("title") or a.get_text())
                t_url  = a.get("href")
                entry["champion_name"] = t_name
                entry["champion_url"]  = t_url
                entry["team_id"]       = _resolve_team_id(t_name, ti)
                entry["tm_team_id"]    = _tm_team_id(t_url)
                break

        flag = row.find("img", class_="flaggenrahmen")
        if flag:
            entry["country"] = flag.get("title", "")

        for i, cell in enumerate(cells):
            txt = _clean(cell.get_text())
            if i < len(headers):
                h = headers[i].lower()
                if "pts" in h or "points" in h:
                    entry["points"] = txt
                elif h in ("w", "wins"):
                    entry["wins"] = txt
                elif h in ("d", "draw", "draws"):
                    entry["draws"] = txt
                elif h in ("l", "loss", "losses"):
                    entry["losses"] = txt

        if entry.get("champion_name") or entry.get("season"):
            results.append(entry)

    return results


def parse_championship_managers(html: str,
                                 player_index: dict | None = None,
                                 team_index:   dict | None = None) -> list[dict]:
    """
    Parse the 'Championship managers' page (erfolgreichstetrainer).
    Returns list with tm_player_id added (managers don't have football-data IDs).
    """
    soup    = _soup(html)
    results = []
    table   = soup.find("table", class_="items")
    if not table:
        return results

    for rank, row in enumerate(table.find_all("tr", class_=["odd", "even"]), 1):
        entry: dict = {"rank": rank}

        inline = row.find("table", class_="inline-table")
        if inline:
            a = inline.find("a")
            if a:
                m_url  = a.get("href")
                m_name = _clean(a.get_text())
                entry["manager_name"]  = m_name
                entry["manager_url"]   = m_url
                entry["player_id"]     = None   # managers have no fd.org player ID
                entry["tm_player_id"]  = _tm_player_id(m_url)

        flag = row.find("img", class_="flaggenrahmen")
        if flag:
            entry["nationality"] = flag.get("title", "")

        title_td = row.find("td", class_="zentriert hauptlink")
        if title_td and title_td.a:
            entry["titles"] = _clean(title_td.a.get_text())

        if entry.get("manager_name"):
            results.append(entry)

    return results


def parse_market_values(html: str,
                        player_index: dict | None = None,
                        team_index:   dict | None = None) -> list[dict]:
    """
    Parse the market values page.
    Returns list with player_id, tm_player_id, team_id, tm_team_id added.
    """
    soup    = _soup(html)
    results = []
    pi      = player_index or {}
    ti      = team_index   or {}
    table   = soup.find("table", class_="items")
    if not table:
        return results

    for rank, row in enumerate(table.find_all("tr", class_=["odd", "even"]), 1):
        entry: dict = {"rank": rank}

        inline = row.find("table", class_="inline-table")
        if inline:
            a = inline.find("a")
            if a:
                p_name = _clean(a.get_text())
                p_url  = a.get("href")
                entry["player_name"]   = p_name
                entry["player_url"]    = p_url
                entry["player_id"]     = pi.get(p_name.lower(), {}).get("player_id")
                entry["tm_player_id"]  = _tm_player_id(p_url)
            tds = inline.find_all("td")
            for td in tds:
                txt = _clean(td.get_text())
                if txt and not td.find("a") and not td.find("img"):
                    entry["position"] = txt
                    break

        flags = row.find_all("img", class_="flaggenrahmen")
        if flags:
            entry["nationality"] = flags[0].get("title", "")

        for td in row.find_all("td"):
            a = td.find("a")
            if a and "/startseite/verein/" in (a.get("href") or ""):
                c_name = _clean(a.get("title") or a.get_text())
                c_url  = a.get("href")
                entry["club_name"]  = c_name
                entry["club_url"]   = c_url
                entry["team_id"]    = _resolve_team_id(c_name, ti)
                entry["tm_team_id"] = _tm_team_id(c_url)
                break

        age_td = row.find("td", class_="zentriert")
        if age_td:
            entry["age"] = _clean(age_td.get_text())

        mv_td = (row.find("td", class_="rechts hauptlink")
                 or row.find("td", class_="zentriert hauptlink"))
        if mv_td:
            entry["market_value"] = _clean(mv_td.get_text())

        if entry.get("player_name"):
            results.append(entry)

    return results


def parse_players_of_year(html: str,
                           player_index: dict | None = None,
                           team_index:   dict | None = None) -> list[dict]:
    """
    Parse the 'Players of the year' page (spieler-des-jahres).

    TM's spieler-des-jahres page uses a multi-column table where each column
    is an award category (e.g. "Player of the Year", "Young Player of the Year").
    Each row = one season.  Each award cell contains an inline-table with the
    player link AND a separate club link.

    Strategy:
      1. Detect award column headers from <thead>.
      2. For each <tbody> row, iterate cells paired with headers.
      3. Within each cell, look for player links (/profil/spieler/ or /spieler/)
         and club links (/startseite/verein/).
      4. Fall back to any <a> inside the cell if the specialised selectors miss.
    """
    soup    = _soup(html)
    results = []
    pi      = player_index or {}
    ti      = team_index   or {}

    table = soup.find("table", class_="items")
    if not table:
        # Try any table on the page as fallback
        table = soup.find("table")
    if not table:
        return results

    # ── Detect award column headers ───────────────────────────────────────────
    headers: list[str] = []
    thead = table.find("thead")
    if thead:
        for th in thead.find_all("th"):
            txt = _clean(th.get_text(" "))
            headers.append(txt if txt else f"col_{len(headers)}")

    # ── Parse rows ────────────────────────────────────────────────────────────
    tbody = table.find("tbody") or table
    for row in tbody.find_all("tr", class_=["odd", "even"]):
        cells = row.find_all("td")
        if not cells:
            continue

        # First cell is typically the season/year
        season_cell = _clean(cells[0].get_text())

        # Walk remaining cells; each may be a separate award category
        for col_idx, td in enumerate(cells[1:], 1):
            award_label = headers[col_idx] if col_idx < len(headers) else "Player of the Year"

            # ── Try to find player link ───────────────────────────────────────
            p_name, p_url = None, None

            # Priority 1: inline-table (standard TM structure)
            inline = td.find("table", class_="inline-table")
            if inline:
                a = inline.find("a", href=re.compile(r"/spieler/\d+"))
                if not a:
                    a = inline.find("a")
                if a and a.get("href"):
                    p_name = _clean(a.get_text())
                    p_url  = a.get("href")

            # Priority 2: any <a> containing /spieler/ in the cell
            if not p_name:
                for a in td.find_all("a", href=True):
                    if "/spieler/" in a.get("href", ""):
                        p_name = _clean(a.get_text())
                        p_url  = a.get("href")
                        break

            # Priority 3: hauptlink td → first <a>
            if not p_name:
                hl = td.find(class_="hauptlink")
                if hl:
                    a = hl.find("a") if not hl.name == "a" else hl
                    if a and a.get("href"):
                        p_name = _clean(a.get_text())
                        p_url  = a.get("href")

            # Skip cells with no player data (could be empty award slots)
            if not p_name or p_name == "-":
                continue

            entry: dict = {
                "year":         season_cell,
                "award":        award_label,
                "player_name":  p_name,
                "player_url":   p_url,
                "player_id":    pi.get(p_name.lower(), {}).get("player_id"),
                "tm_player_id": _tm_player_id(p_url),
            }

            # ── Club link ─────────────────────────────────────────────────────
            for a in td.find_all("a", href=True):
                if "/startseite/verein/" in a.get("href", ""):
                    c_name = _clean(a.get("title") or a.get_text())
                    c_url  = a.get("href")
                    entry["club_name"]  = c_name
                    entry["club_url"]   = c_url
                    entry["team_id"]    = _resolve_team_id(c_name, ti)
                    entry["tm_team_id"] = _tm_team_id(c_url)
                    break

            # ── Nationality flag ──────────────────────────────────────────────
            flag = td.find("img", class_="flaggenrahmen")
            if flag:
                entry["nationality"] = flag.get("title", "")

            results.append(entry)

    return results


# ── Team-level parsers ────────────────────────────────────────────────────────

def parse_team_info(html: str) -> dict:
    """Parse team overview page: squad stats, market value, etc."""
    soup = _soup(html)
    info: dict = {}

    for item in soup.find_all("li", class_="data-header__label"):
        text = _clean(item.get_text(" "))
        if "Squad size:"   in text: info["squad_size"]    = _clean(text.split(":")[-1])
        if "Average age:"  in text: info["average_age"]   = _clean(text.split(":")[-1])
        if "Foreigners:"   in text: info["foreigners"]    = _clean(text.split(":")[-1])
        if "National team" in text: info["national_team"] = _clean(text.split(":")[-1])
        if "League level"  in text: info["league_level"]  = _clean(text.split(":")[-1])
        if "Stadium"       in text: info["stadium"]       = _clean(text.split(":")[-1])
        if "Coach"         in text: info["coach"]         = _clean(text.split(":")[-1])

    mv = soup.find("a", class_="data-header__market-value-wrapper")
    if mv:
        info["total_market_value"] = _clean(mv.get_text(" ").split("\n")[0])

    return info


def parse_squad_links(html: str) -> dict:
    """Parse team page squad table → { player_name: href_slug }."""
    soup    = _soup(html)
    players = {}
    table   = soup.find("table", class_="items")
    if not table:
        return players
    for td in table.find_all("td", class_="hauptlink"):
        if "rechts" in td.get("class", []):
            continue
        a = td.find("a")
        if a and a.get("href"):
            name = _clean(a.get_text())
            href = a["href"]
            if name and href:
                players[name] = href
    return players


def parse_trophies(html: str) -> list[dict]:
    """Parse a trophies/erfolge page for team or player."""
    soup     = _soup(html)
    trophies = []

    for box in soup.find_all("div", class_="box"):
        title_tag = box.find("h2")
        img_tag   = box.find("img")
        if not (title_tag and img_tag):
            continue

        raw = _clean(title_tag.get_text())
        m   = re.match(r"^(\d+)x\s+(.*)", raw, re.IGNORECASE)
        if m:
            count      = m.group(1)
            clean_name = m.group(2).strip()
        else:
            count      = "1"
            clean_name = raw

        if clean_name.lower() in ("all titles", "all-titles", ""):
            continue

        safe = re.sub(r"[^\w\s-]", "", clean_name).strip()
        safe = re.sub(r"[\s_-]+", "_", safe).lower()

        img_url = img_tag.get("src") or ""
        if img_url:
            img_url = img_url.replace("small", "medium").split("?")[0]

        trophies.append({
            "name":       clean_name,
            "safe_name":  safe,
            "count":      count,
            "local_path": f"/assets/trophies/{safe}.jpg",
            "source_url": img_url or None,
        })

    return trophies


# ── Player-level parsers ──────────────────────────────────────────────────────

def parse_player_full_info(html: str) -> dict:
    """
    Comprehensive player profile parser.

    Extracts:
      - market_value, last_mv_update, shirt_number, is_captain
      - name, full_name, date_of_birth, age, place_of_birth
      - height, citizenship (list), position, other_positions, foot
      - current_club (name, url, joined, contract_expires, last_extension)
      - national_team (name, url, caps, goals)
      - outfitter, player_agent, social_media
      - achievements (badge strip: name + count)
      - youth_clubs, further_information
      - transfer_component_metadata (raw attrs for JS component)
    """
    soup = _soup(html)
    info: dict = {}

    # ── Market value & shirt number ──────────────────────────────────────────
    mv_a = soup.find("a", class_="data-header__market-value-wrapper")
    if mv_a:
        info["market_value"] = _clean(mv_a.get_text(" ").split("Last update")[0])
        update_p = mv_a.find("p", class_="data-header__last-update")
        if update_p:
            txt = _clean(update_p.get_text())
            info["mv_last_update"] = txt.replace("Last update:", "").strip()

    shirt = soup.find("span", class_="data-header__shirt-number")
    if shirt:
        info["shirt_number"] = _clean(shirt.get_text()).replace("#", "")

    captain_img = soup.find("img", alt="Captain")
    info["is_captain"] = captain_img is not None

    # ── Achievement badges ────────────────────────────────────────────────────
    badge_container = soup.find("div", class_="data-header__badge-container")
    achievements = []
    if badge_container:
        for a_tag in badge_container.find_all("a", class_="data-header__success-data"):
            img = a_tag.find("img")
            num = a_tag.find("span", class_="data-header__success-number")
            if img and num:
                achievements.append({
                    "trophy":  img.get("title") or img.get("alt") or "",
                    "count":   _clean(num.get_text()),
                    "img_url": img.get("src") or img.get("data-src") or "",
                })
    info["achievements"] = achievements

    # ── Info table ────────────────────────────────────────────────────────────
    for label_span in soup.find_all(
        "span",
        class_=lambda c: c and "info-table__content--regular" in c,
    ):
        key = _clean(label_span.get_text()).rstrip(":").strip()
        val_span = label_span.find_next_sibling(
            "span",
            class_=lambda c: c and "info-table__content--bold" in c,
        )
        if not val_span:
            continue

        if "social" in key.lower():
            info["social_media"] = [
                a.get("href") for a in val_span.find_all("a", href=True)
            ]
            continue

        val = _clean(val_span.get_text(" "))
        key_snake = re.sub(r"[\s/]+", "_", key.lower())

        if "citizenship" in key_snake:
            info["citizenship"] = [
                img.get("title", "") for img in val_span.find_all("img")
            ] or [val]
            continue

        if "current_club" in key_snake:
            a = val_span.find("a")
            info["current_club_name"] = _text(a) if a else val
            info["current_club_url"]  = a.get("href") if a else None
            continue

        if "agent" in key_snake or "player_agent" in key_snake:
            a = val_span.find("a")
            info["player_agent"] = _text(a) if a else val
            continue

        info[key_snake] = val

    # ── Caps & goals ──────────────────────────────────────────────────────────
    for li in soup.find_all("li", class_="data-header__label"):
        if "Caps" in _text(li):
            links = li.find_all("a", class_="data-header__content")
            if len(links) >= 2:
                info["national_caps"]  = _clean(links[0].get_text())
                info["national_goals"] = _clean(links[1].get_text())
            break

    for li in soup.find_all("li", class_="data-header__label"):
        if "international" in _text(li).lower():
            a = li.find("a")
            if a:
                info["national_team_name"] = _text(a)
                info["national_team_url"]  = a.get("href")
            break

    # ── Position ──────────────────────────────────────────────────────────────
    pos_box = soup.find("div", class_="detail-position")
    if pos_box:
        main_dd = pos_box.find("dd", class_="detail-position__position")
        if main_dd:
            info["position"] = _clean(main_dd.get_text())
        other_dds = pos_box.find_all("dd", class_="detail-position__position")
        if len(other_dds) > 1:
            info["other_positions"] = [_clean(dd.get_text()) for dd in other_dds[1:]]

    # ── Youth clubs ───────────────────────────────────────────────────────────
    youth_h = soup.find(lambda t: t.name in ("h2", "span") and "Youth clubs" in t.get_text())
    if youth_h:
        div = youth_h.find_next_sibling("div", class_="content")
        if div:
            info["youth_clubs"] = _clean(div.get_text())

    # ── Further information ───────────────────────────────────────────────────
    further_h = soup.find(lambda t: t.name in ("h2", "span") and "Further information" in t.get_text())
    if further_h:
        div = further_h.find_next_sibling("div", class_="content")
        if div:
            info["further_information"] = _clean(div.get_text())

    # ── Transfer history component ────────────────────────────────────────────
    th_tag = soup.find("tm-player-transfer-history")
    if th_tag:
        info["transfer_component_metadata"] = dict(th_tag.attrs)

    return info


def extract_player_image(html: str) -> str | None:
    """Return the player profile image URL, or None."""
    soup    = _soup(html)
    img_tag = soup.find("img", class_="data-header__profile-image")
    if img_tag:
        url = img_tag.get("src", "")
        if url:
            return url.replace("small", "medium").split("?")[0]
    return None