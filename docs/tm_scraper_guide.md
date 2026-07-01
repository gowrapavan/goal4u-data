# TM Scraper — Full Reference Guide

This document explains, page by page, exactly what `tm_scraper` fetches from
Transfermarkt, which parser handles it, which fields get extracted, and
where the result is saved on disk. It's meant to be the "read this before
you touch the scraper" doc.

**Covers two pipelines that share the same output trees:**
- The **league** pipeline (`runner.py`) — the 5 domestic leagues, driven by
  football-data.org's `teams.json`.
- The **cup** pipeline (`cup_runner.py`) — Champions League, Euro, World
  Cup, and national teams, added later, with no football-data.org
  equivalent to key off.

---

## 1. High-level architecture

```
runner.py (league orchestrator)          cup_runner.py (cup orchestrator)
 ├─ config_tm.py    → league list, URL builders, output paths, aliases
 ├─ config_cup.py   → cup competition list, URL builders, season labels
 ├─ cf_fetcher.py   → fetch_html(url): the only way HTML ever gets downloaded
 ├─ tm_parsers.py   → one parse_*() function per league/club/player page type
 ├─ cup_parsers.py  → parsers specific to cup/national-team pages
 ├─ id_registry.py  → synthetic-ID allocator for entities with no football-data ID
 └─ utils.py        → safe_write() (atomic JSON writer)
```

`runner.py` is called once per **league + season** via
`run_season(league_code, season)`.
`cup_runner.py` is called once per **competition + season** via
`run_cup(code, season_id)`, or standalone via `run_national_teams()`.
Both share `cf_fetcher.py`, `utils.py`, and most of `tm_parsers.py` — they
are not separate codebases, just separate entry points into the same
output trees.

### Two data sources feed into one output

1. **football-data.org** (a separate pipeline, not in this scraper) produces
   `data/{season}/{league_code}/teams.json` — the authoritative list of
   teams and squads with **football-data IDs**, for the 5 tracked domestic
   leagues only.
2. **tm_scraper** (this codebase) scrapes Transfermarkt for everything
   football-data.org doesn't have — market values, trophies, images,
   biographical detail, historical records, cup competitions, national
   teams — and **matches it back onto football-data IDs by name wherever
   possible**, falling back to a permanent synthetic ID otherwise (see §2.2).

`run_season()` refuses to run for a league/season if
`data/{season}/{league}/teams.json` doesn't exist yet. `run_cup()` has no
such hard gate — cup competitions have no local "teams.json" of their own —
but it still tries to match every cup team/player against whatever
football-data data already exists on disk, so run the league pipeline first
if you want maximum ID reuse.

---

## 2. The ID system

Every scraped record carries **four ID fields**, not one:

| Field          | Source                                   | Present when                          |
|----------------|-------------------------------------------|----------------------------------------|
| `player_id`    | football-data.org, **or** a synthetic ID from `id_registry.py` | Always populated for anyone actually scraped (see §2.2 for the synthetic case). `None` only appears in a few historical-legend league tables where no player page was ever scraped — see §4.3/§4.4. |
| `tm_player_id` | Extracted from the TM URL (`/spieler/<id>`) | Always, whenever a TM profile link exists. |
| `team_id`      | football-data.org, **or** a synthetic ID from `id_registry.py` | Same rule as `player_id`. |
| `tm_team_id`   | Extracted from the TM URL (`/verein/<id>`) | Always, whenever a TM club link exists. |

**`build_id_indexes(season)`** (in `runner.py`, reused by `cup_runner.py`)
builds two lookup dicts by scanning every `data/*/*/teams.json` file on
disk before any scraping starts:

- `player_index: { "lowercase player name" → {player_id, team_id, team_name} }`
- `team_index:   { "lowercase team name/shortName/tla" → football_data_team_id }`

`_resolve_team_id()` does exact match → suffix-stripped match
(`FC`/`AFC`/`SC`/`CF` removed) → fuzzy overlap match (≥70% similarity), in
that order.

### 2.1 League filenames are keyed by football-data IDs, not Transfermarkt IDs

**Every league-pipeline file and image is named after the football-data.org
ID, never Transfermarkt's own ID**, and that ID is already known — read
straight out of `teams.json` — *before* a single Transfermarkt page is
fetched.

```python
def _scrape_player(p: dict, tm_players: dict, force: bool = False) -> dict | None:
    p_id   = str(p.get("id", ""))      # ← football-data ID, from teams.json
    p_name = p.get("name", "")         # ← football-data name, from teams.json
    ...
    player_file = PLAYER_INFO_DIR / f"{p_id}.json"     # player_information/{p_id}.json
    ...
    img_save    = PLAYER_IMAGES_DIR / f"{p_id}.jpg"    # assets/player_images/{p_id}.jpg
```

Transfermarkt's own ID (`tm_player_id`) is extracted later and stored as a
**field inside** the JSON — never a filename.

**Consequence — the football-data squad list is a hard gate for the league
pipeline:** if `p.get("id")` or `p.get("name")` is empty for a squad entry,
`_scrape_player()` returns `None` immediately. No folder, no image, nothing
gets written for that player, regardless of whether they actually exist on
Transfermarkt.

### 2.2 Cup / national-team IDs — reuse first, synthesize only as a last resort

Cup competitions and national teams have **no football-data.org
equivalent** to read an ID from ahead of time — there's no `teams.json` for
"Champions League" or "Portugal". `cup_runner.py` resolves an ID for every
team/player it scrapes in this order:

1. **Try to match by name against the football-data `team_index` /
   `player_index`** built in §2 above. A club that's *also* in one of the 5
   tracked domestic leagues (e.g. Real Madrid playing in the Champions
   League) resolves to its **existing** `team_informations/{id}/` folder —
   no duplicate folder is ever created, and its existing
   `team_metadata.json` is reused as-is instead of being re-fetched.
2. **Only if that lookup misses** (national teams; clubs outside the 5
   tracked leagues; e.g. Club Brugge, Qarabağ, Bodø/Glimt) does
   `id_registry.py` hand out a **synthetic ID starting at 1,000,000** — well
   clear of football-data's own (much smaller) ID space, so there's no
   collision risk.

That synthetic ID is **permanent**: it's persisted to a single file,
`data/id_registry.json` (not a new folder — one small JSON file sitting
directly in `data/`), keyed by lowercased name:

```json
{
  "teams":   { "portugal": 1000001, "club brugge kv": 1000002 },
  "players": { "cristiano ronaldo": 1000001 },
  "next_team_id": 1000003,
  "next_player_id": 1000002
}
```

This is what lets **"Portugal" resolve to the exact same
`team_informations/1000001/` folder** across every future run and every
competition — its World Cup squad and its Euro squad both land in that one
folder, distinguished only by filename (see §7.4).

`IdRegistry` (in `id_registry.py`) is thread-safe: a lock guards
read-modify-write so concurrent workers (`TEAM_WORKERS × PLAYER_WORKERS`)
can never be handed the same new ID twice, and every allocation is saved to
disk immediately, not batched.

> **Rule of thumb for consumers of this data:** `team_id` / `player_id` <
> ~1,000,000 → real football-data entity, also present in `teams.json`
> somewhere. `team_id` / `player_id` ≥ 1,000,000 → synthetic, cup/national-
> team-only entity, only ever sourced from Transfermarkt.

---

## 3. Output directory layout

**No new top-level folders were introduced for the cup pipeline.**
Everything — league, cup, and national-team data — lives inside the same
three trees:

```
data/
├── league_info/
│   └── {CODE}/                           GB1, ES1, IT1, L1, FR1  (leagues)
│       │                                 CL, EURO, WC            (cups)
│       │
│       ├── league_metadata.json          leagues only — overview stats, no season slug
│       ├── successful_players.json       leagues only
│       ├── all_champions.json            leagues only
│       ├── championship_managers.json    leagues only
│       ├── players_of_year.json          leagues only
│       │
│       ├── top_scorers.json              leagues: all-time, one row per season, no season slug
│       ├── top_scorers_all_time.json     cups only: same idea, cup-specific filename
│       │
│       ├── market_values.json            leagues: current, no season slug
│       │
│       └── {season_label}/               cups only — one folder per edition
│           ├── market_values.json        this edition's most valuable players
│           └── top_scorers.json          this edition's top scorers
│
├── team_informations/
│   └── {team_id}/                        football-data ID OR synthetic ID (§2.2)
│       ├── team_metadata.json            shared/reused across league + every cup
│       ├── {season}-squad.json           league squad, one file per season
│       ├── cl-{season_label}.json        Champions League squad, one file per edition
│       ├── euro-{season_label}.json      Euro squad, one file per edition
│       ├── world-cup-{season_label}.json World Cup squad, one file per edition
│       └── national-team-current.json    standalone weltrangliste-based scrape (§8)
│
├── player_information/
│   └── {player_id}.json                  football-data ID OR synthetic ID — shared
│                                          across league, cup, and national-team scrapes
│
├── id_registry.json                      synthetic-ID allocation table (§2.2)
│
public/assets/
├── player_images/{player_id}.jpg         shared across every pipeline
└── trophies/{safe_trophy_name}.jpg       shared across every pipeline
```

**Why a team's folder can hold several squad files:** the same entity can
play in more than one competition in the same window — a club in its
domestic league *and* the Champions League, or a national side in *both*
the World Cup and the Euro. One `team_id` → one folder → multiple
competition-tagged squad files inside it, e.g.:

```
team_informations/1000001/                (Portugal — synthetic ID)
├── team_metadata.json
├── world-cup-2026.json
└── euro-2024.json

team_informations/66/                     (Real Madrid — real football-data ID)
├── team_metadata.json
├── 2025-2026-squad.json                  (from the league pipeline)
└── cl-2025-2026.json                     (from the cup pipeline)
```

Every JSON file is written by `utils.safe_write()`, which wraps the payload
as:

```json
{
  "_meta": { "last_synced": "<ISO-8601 UTC>", "source": "football-data.org v4" },
  "data": { ... actual content ... }
}
```

and writes atomically (`.tmp` file + `os.replace`), so a crash mid-write
never corrupts an existing file.

> ⚠️ **This envelope matters for anyone reading these files programmatically
> — including the scraper's own code.** See §13.1 for a real bug this
> caused.

---

## 4. League-level pages (7 per league, fetched in parallel)

For each of `PD` (LaLiga), `PL` (Premier League), `SA` (Serie A), `BL1`
(Bundesliga), `FL1` (Ligue 1) — mapped in `config_tm.LEAGUE_MAPPING` to TM's
own `tm_name` / `tm_id` — `_fetch_league_info()` fires 7 concurrent requests
(`ThreadPoolExecutor(max_workers=7)`):

| # | URL builder | Example URL | Parser | Output file |
|---|---|---|---|---|
| 1 | `league_metadata_url` | `.../premier-league/startseite/wettbewerb/GB1` | `parse_league_metadata` | `league_metadata.json` |
| 2 | `league_top_scorers_url` | `.../premier-league/torschuetzenkoenige/wettbewerb/GB1` | `parse_top_scorers` | `top_scorers.json` |
| 3 | `league_successful_players_url` | `.../premier-league/erfolgreichstespieler/wettbewerb/GB1` | `parse_successful_players` | `successful_players.json` |
| 4 | `league_all_champions_url` | `.../premier-league/alle-meister/wettbewerb/GB1` | `parse_all_champions` | `all_champions.json` |
| 5 | `league_championship_managers_url` | `.../premier-league/erfolgreichstetrainer/wettbewerb/GB1` | `parse_championship_managers` | `championship_managers.json` |
| 6 | `league_market_values_url` | `.../premier-league/marktwerte/wettbewerb/GB1` | `parse_market_values` | `market_values.json` |
| 7 | `league_players_of_year_url` | `.../premier-league/spieler-des-jahres/wettbewerb/GB1` | `parse_players_of_year` | `players_of_year.json` |

None of these 7 URLs take a season parameter (except where TM itself
requires one) — they're global/historical pages, not per-season snapshots.

### 4.1 `league_metadata.json` — `parse_league_metadata()`

Scrapes the league overview page's header block (`div.data-header__info-box`,
`div.data-header__box--big`, `div.data-header__box--small`).

Fields: `number_of_teams`, `players`, `foreigners`, `avg_market_value`,
`avg_age`, `most_valuable_player` (`name`, `value`, `url`, `player_id`,
`tm_player_id`), `total_market_value`, `reigning_champion` (`name`, `url`,
`team_id`, `tm_team_id`), `record_champion` (`name`, `titles`, ...),
`uefa_coefficient` (`position`, `points`), `league_level`.

### 4.2 `top_scorers.json` — `parse_top_scorers()`

One row per season from the `table.items` golden-boot table.

Fields per row: `season`, `player_name`, `player_url`, `player_id`,
`tm_player_id`, `position`, `club_name`, `club_url`, `team_id`,
`tm_team_id`, `goals`, `nationality`.

### 4.3 `successful_players.json` — `parse_successful_players()`

Ranked list (all-time), one row per player. Historical legends who predate
football-data.org's tracking window get `player_id: null`.

Fields per row: `rank`, `player_name`, `player_url`, `player_id`,
`tm_player_id`, `position`, `nationality`, `teams_with_titles`,
`total_titles`.

### 4.4 `all_champions.json` — `parse_all_champions()`

One row per season the league has been played.

Fields per row: `season`, `champion_name`, `champion_url`, `team_id`,
`tm_team_id`, `country`, plus whatever of `points` / `wins` / `draws` /
`losses` the table's header columns expose that season (older seasons have
fewer stat columns than recent ones).

### 4.5 `championship_managers.json` — `parse_championship_managers()`

Ranked list of managers with the most titles.

Fields per row: `rank`, `manager_name`, `manager_url`, `player_id` (always
`None` — managers have no football-data ID), `tm_player_id`, `nationality`,
`titles`.

### 4.6 `market_values.json` — `parse_market_values()`

Current top-valued-players ranking for the whole league (not per-club).

Fields per row: `rank`, `player_name`, `player_url`, `player_id`,
`tm_player_id`, `position`, `nationality`, `club_name`, `club_url`,
`team_id`, `tm_team_id`, `age`, `market_value`.

### 4.7 `players_of_year.json` — `parse_players_of_year()`

The trickiest parser — TM lays this page out as a multi-column table where
each **column** is an award category (e.g. "Player of the Year", "Young
Player of the Year") and each **row** is a season. The parser detects
headers from `<thead>`, then walks every cell of every row, so the output
is flattened to one entry per (season, award) pair rather than mirroring
the table shape.

Fields per row: `year`, `award`, `player_name`, `player_url`, `player_id`,
`tm_player_id`, `club_name`, `club_url`, `team_id`, `tm_team_id`,
`nationality`.

---

## 5. Team-level pages (per team, per season) — league pipeline

Driven by `_scrape_team()`, called once per team in
`data/{season}/{league}/teams.json` via a `ThreadPoolExecutor` with
`TEAM_WORKERS = 6` concurrent teams.

> **The `{team_id}` folder name below is the football-data ID, already
> known from `teams.json` before any Transfermarkt page is fetched — not
> Transfermarkt's own `/verein/<id>`. See §2.1.**

### 5.1 Matching a local team to its TM page

1. `run_season()` fetches the league's `startseite` page
   (`league_url(tm_name, tm_id, year)` — this one **does** take
   `saison_id=<year>` explicitly) and runs `parse_league_teams()` to build
   `{ tm_club_name: tm_href_slug }` for every club in the table.
2. For each local team, `TEAM_ALIASES` (in `config_tm.py`) is checked first
   for a manual override (e.g. `"Man City"` → `"Manchester City"`), then
   `match_name()` fuzzy-matches (`SequenceMatcher`, threshold 0.55) against
   the TM club-name dict.
3. If no match is found at all, the team is logged as `[-] Unmatched team`
   and skipped — extend `TEAM_ALIASES` when you see that warning.

### 5.2 Team page → `team_metadata.json` — `parse_team_info()`

URL: `TM_BASE_URL + tm_href_slug` (e.g.
`.../manchester-city/startseite/verein/281`)

Fields: `squad_size`, `average_age`, `foreigners`, `national_team`,
`league_level`, `stadium`, `coach`, `total_market_value` — all read from
`li.data-header__label` items in the club header.

Saved as:
```json
{
  "team_id": <football_data_team_id>,
  "tm_name": "<matched TM club name>",
  "tm_url": "<full TM URL>",
  "tm_stats": { ...parse_team_info() output... },
  "trophies": [ ...see 5.3... ]
}
```

### 5.3 Trophies page → embedded in `team_metadata.json` — `parse_trophies()`

URL: same team URL with `/startseite/` → `/erfolge/`.

Each `div.box` with both a title and an image is treated as one trophy.
`"3x League Title"` → `{count: "3", name: "League Title"}`. `"All titles"`
boxes are filtered out.

Fields per trophy: `name`, `safe_name` (filesystem-safe slug), `count`,
`local_path` (`/assets/trophies/{safe_name}.jpg`), `source_url` (original
TM image URL).

Trophy images are downloaded via `download_asset()` to
`public/assets/trophies/{safe_name}.jpg` — **always skipped if the file
already exists**, since trophy artwork never changes.

### 5.4 Squad list → `{season}-squad.json`

`parse_squad_links()` reads the squad `table.items` on the team page (the
same fetch as 5.2, no extra request) and returns `{ player_name:
tm_href_slug }` for every player row.

For every player in the local `teams.json` squad, `match_name()`
fuzzy-matches against this dict, then hands off to the per-player worker
(§6).

Saved as:
```json
{
  "team_id": <football_data_team_id>,
  "season": "2026-2027",
  "squad_roster": [
    {"player_id": "...", "name": "...", "position": "..."},
    ...
  ]
}
```

---

## 6. Player-level pages (per player) — league pipeline

Driven by `_scrape_player()`, run inside each team's own
`ThreadPoolExecutor` with `PLAYER_WORKERS = 12` concurrent players — so
total concurrency across the whole run can reach `TEAM_WORKERS ×
PLAYER_WORKERS = 72` simultaneous requests.

> **The `{player_id}` in every filename below is the football-data ID,
> already known from `teams.json` before this page is fetched — not
> Transfermarkt's ID. See §2.1.**

### 6.1 Profile page → `player_information/{player_id}.json` — `parse_player_full_info()`

URL: `TM_BASE_URL + tm_href_slug` (e.g.
`.../gianluigi-donnarumma/profil/spieler/315858`)

Fields: `market_value`, `mv_last_update`, `shirt_number`, `is_captain`,
`achievements` (list of `{trophy, count, img_url}` badges from the header
strip), plus everything in the right-hand "Facts and data" info-table —
dynamically snake_cased from whatever labels TM shows for that player
(`date_of_birth`, `place_of_birth`, `height`, `citizenship` (list), `foot`,
`current_club_name`/`current_club_url`, `joined`, `contract_expires`,
`player_agent`, `outfitter`, `social_media` (list of URLs)) — plus
`national_caps`, `national_goals`, `national_team_name`,
`national_team_url`, `position`, `other_positions`, `youth_clubs`,
`further_information`, and `transfer_component_metadata` (raw HTML
attributes off the `<tm-player-transfer-history>` web component).

Saved as:
```json
{
  "player_id": "<football_data_player_id>",
  "name": "<name from local squad data>",
  "tm_url": "<full TM profile URL>",
  "image_path": "/assets/player_images/{player_id}.jpg" | null,
  "details": { ...parse_player_full_info() output... },
  "trophies": [ ...see 6.3... ]
}
```

Cup and national-team players are scraped by the exact same
`parse_player_full_info()` / `extract_player_image()` functions from
`cup_runner._scrape_player()`, and saved with the **identical** record
shape into the same shared `player_information/` folder — the only
difference is where `player_id` came from (§2.2).

### 6.2 Profile image — `extract_player_image()`

Pulled from the same profile-page fetch (no extra request): reads
`img.data-header__profile-image`, upgrades `small` → `medium` in the URL,
strips query params. Downloaded via `download_asset()` to
`public/assets/player_images/{player_id}.jpg` — **always skipped if
already on disk**, shared across every pipeline.

### 6.3 Trophies page → embedded in the player JSON — `parse_trophies()`

Same parser as team trophies (§5.3), same URL-swap pattern:
`/profil/` → `/erfolge/`. Images downloaded to
`public/assets/trophies/{safe_name}.jpg` with `use_lock=True` (a shared
`threading.Lock`) since many players across many concurrent workers can
share the same trophy image and race on the write.

---

## 7. Cup competitions (CL / EURO / WC) — `cup_runner.py`

Scope, by explicit design: **competition info + team info + player info
only.** No match schedules, no group-stage tables, no bracket — TM's
`allTables` / `allGames` / `kophase` data is intentionally never fetched.

Cup URLs use `pokalwettbewerb/{tm_id}` instead of the league's
`wettbewerb/{tm_id}`, and (unlike a league's evergreen "current" pages)
every cup sub-page requires an explicit `saison_id` in the URL — there's no
"current edition" variant of the market-values/scorers/participants pages.

`CUP_MAPPING` (in `config_cup.py`):

| Code | TM name | TM ID |
|---|---|---|
| `CL` | `uefa-champions-league` | `CL` |
| `EURO` | `uefa-euro` | `EURO` |
| `WC` | `world-cup` | `FIWC` |

### 7.1 Season-label resolution — `cup_season_label(code, season_id)`

TM's `saison_id` is one year behind the tournament's actual year for
WC/EURO, and the usual "span" format for CL:

| Code | `season_id` | Label used in filenames |
|---|---|---|
| `CL` | `"2025"` | `"2025-2026"` |
| `WC` | `"2021"` | `"2022"` (2022 World Cup) |
| `EURO` | `"2023"` | `"2024"` (Euro 2024) |
| any | `None` | `"current"` (only used for filenames — you still need a real `saison_id` for the actual fetch) |

### 7.2 Competition info → `league_info/{CODE}/...`

`run_cup_info()` fires 3 requests:

| Output file | URL builder | Parser | Season-scoped? |
|---|---|---|---|
| `league_info/{CODE}/{season_label}/market_values.json` | `cup_market_values_url` | `tm_parsers.parse_market_values` | Yes |
| `league_info/{CODE}/{season_label}/top_scorers.json` | `cup_top_scorers_url` | `tm_parsers.parse_top_scorers` | Yes |
| `league_info/{CODE}/top_scorers_all_time.json` | `cup_top_scorers_alltime_url` | `cup_parsers.parse_cup_alltime_top_scorers` | No — spans every edition ever played |

The first two are enriched with `player_id`/`team_id` via the same
`player_index`/`team_index` the league pipeline builds; the all-time
scorers parser doesn't take those kwargs (it only ever returns
`tm_player_id`/`tm_team_id`, since an all-time ranking spans players who
predate football-data.org entirely).

### 7.3 Participants → `parse_cup_participants()`

URL: `cup_participants_url(tm_name, tm_id, season_id)` →
`.../teilnehmer/pokalwettbewerb/{id}/saison_id/{year}`.

TM splits this page into two tables (still-in-the-competition +
eliminated), so — unlike `parse_league_teams()`, which only looks at the
first `table.items` — this walks **every** `table.items` on the page and
merges them into one `{ team_name: href_slug }` dict.

> If this parses to 0 teams, treat it like a `TEAM_ALIASES` gap: check the
> logs and verify the page structure. This parser has not been verified
> against a live `teilnehmer` page the way `parse_national_ranking_page`
> has — it's inferred from TM's site-wide markup conventions.

### 7.4 Team + squad scraping → `_scrape_team()` (cup_runner.py)

Same shape as the league's `_scrape_team()`, with two differences:

1. `team_id` is resolved via §2.2's reuse-then-synthesize rule instead of
   being read straight from `teams.json`.
2. The squad filename is `{CUP_SLUG[code]}-{season_label}.json`
   (`CUP_SLUG = {"CL": "cl", "EURO": "euro", "WC": "world-cup", "NT":
   "national-team"}`) instead of the league's `{season}-squad.json`, so a
   team already scraped via the league pipeline gets an **additional** file
   in its existing folder rather than a competing one.

`team_metadata.json` is only re-fetched if it doesn't already exist
(`[reuse] ... metadata already exists — not re-fetched` in the logs) —
clubs already scraped via the league pipeline skip straight to squad
scraping.

Saved squad shape:
```json
{
  "team_id": <football_data_or_synthetic_id>,
  "competition": "CL",
  "season": "2025-2026",
  "squad_roster": [
    {"player_id": "...", "name": "...", "position": "..."},
    ...
  ]
}
```

### 7.5 Player scraping — identical to §6, different ID source

`cup_runner._scrape_player()` uses the exact same
`parse_player_full_info()` / `extract_player_image()` / `parse_trophies()`
functions as the league pipeline, writing into the same shared
`player_information/{player_id}.json` and
`public/assets/player_images/{player_id}.jpg`. The only difference is
`player_id` resolution (§2.2).

**Cache-hit gotcha (fixed):** the original code did
`return json.loads(player_file.read_text())` directly when a player's file
already existed — but `safe_write()` wraps every file as `{"_meta": ...,
"data": {...}}`, so that returned the *envelope*, not the record, and
`rec["player_id"]` at the roster-append site raised `KeyError: 'player_id'`
for every already-scraped player. Fixed by unwrapping `raw.get("data",
raw)` before returning the cached record, plus defensive `isinstance`/key
checks at both the roster-append site and the final sort. See §13.1.

Usage:
```bash
python -m tm_scraper.cup_runner --competition CL   --season 2025
python -m tm_scraper.cup_runner --competition EURO --season 2023
python -m tm_scraper.cup_runner --competition WC   --season 2021
```

---

## 8. National teams — FIFA ranking (`weltrangliste`)

Standalone utility, **not tied to one tournament edition** — pulls the
full current national-team roster for every country TM tracks, as an
alternative/fallback source of national-team entities (e.g. useful before
a not-yet-qualified tournament's `teilnehmer` page exists, or just to seed
every country's team once).

TM treats a national team exactly like a club for
`parse_team_info`/`parse_squad_links` purposes — same "verein"-style page —
so no new parsers were needed for the team/squad part, only for the
ranking list itself.

### 8.1 `national_ranking_url(page)` → `/statistik/weltrangliste`

Paginated. `get_last_ranking_page()` reads `ul.tm-pagination` to find how
many pages to walk. `parse_national_ranking_page()` returns, per row:
`rank`, `name`, `tm_url`, `tm_team_id`, `squad_size`, `avg_age`,
`total_value`, `confederation`, `points`.

### 8.2 `run_national_teams()`

Walks every ranking page, dedupes teams by `tm_url`, then scrapes each one
through the same `_scrape_team()` used for cup competitions, passing
`code="NT"`, `season_label="current"` — so the squad file lands as
`team_informations/{team_id}/national-team-current.json`, in the **same**
folder a World Cup or Euro run for that same country would also write into
(the `team_id` always matches, since both paths resolve through the same
`id_registry`/`team_index` lookup — §2.2).

Usage:
```bash
python -m tm_scraper.cup_runner --national-teams
```

---

## 9. How HTML actually gets fetched — `cf_fetcher.fetch_html()`

Transfermarkt sits behind Cloudflare, so every single request above funnels
through one cascade, tried in order until one succeeds or all fail:

1. **`curl_cffi` direct** — TLS-fingerprint-spoofed request (Chrome
   impersonation, no proxy). Fast (~1–3s). Works for plenty of pages,
   especially locally.
2. **`curl_cffi` + Webshare rotating proxy** — same TLS spoofing routed
   through `WEBSHARE_PROXY_URL`. Empirically the method that reliably
   clears Transfermarkt's Cloudflare rules from datacenter IPs.
3. **Playwright + stealth (headless Chromium)** — last resort only, and
   skipped entirely on CI by default (`SKIP_PLAYWRIGHT` env var, default
   `"true"` when `CI=true`).

Any page that all three methods fail on returns `None`, and the caller
either logs a warning and skips, or leaves the existing file untouched
(`safe_write(None, ...)` is a no-op).

### 9.1 ⚠️ Local-machine memory crash under high concurrency (observed, not yet hard-fixed)

If `WEBSHARE_PROXY_URL` is unset, every request that fails step 1 falls
straight through to Playwright (step 3). With `TEAM_WORKERS=6 ×
PLAYER_WORKERS=10` (cup) or `×12` (league), that means **dozens of headless
Chromium instances can try to launch at once** on a single machine. On a
real run this produced:

```
Fatal process out of memory: Worklist::Segment::Create
[Playwright] Failed: Page.goto: Connection closed while reading from the driver
[Playwright] Failed: 'PlaywrightContextManager' object has no attribute '_playwright'
```

The run still completes (each failure is caught and retried/skipped, not
fatal to the whole process), but it wastes a lot of time and RAM. Two ways
to avoid it:

- **Set `WEBSHARE_PROXY_URL`** so step 2 catches most failures before they
  ever reach Playwright — this is the main fix, since Playwright has a
  documented ~0% success rate against this site outside of very specific
  environments anyway.
- **Set `SKIP_PLAYWRIGHT=true`** to disable step 3 entirely on a local
  Windows box, same as CI already does by default.

If neither is set, consider lowering `TEAM_WORKERS`/`PLAYER_WORKERS` in
`runner.py`/`cup_runner.py` as a blunter fallback.

---

## 10. Skip / force / resume logic

Every output file is checked with `_file_has_data(path)` before a fetch is
attempted:

- **Missing file** → always fetch.
- **File exists but `data` is an empty list/dict** (previous run got
  CF-blocked and something upstream still wrote a shell file) → treated as
  "not really done", retried automatically without needing `--fullscrape`.
- **File exists with real content, `force=False`** → skipped.
- **`force=True`** (`--fullscrape` CLI flag) → always re-fetched and
  overwritten, **except images**, which are always skipped if already on
  disk regardless of `force`.

This makes a normal run naturally resumable: kill it mid-way and re-run,
and it picks up exactly where it left off.

**`id_registry.json` is not subject to this skip logic** — it isn't a
scraped-data file, it's a permanent allocation table. It's written to
immediately on every new ID assignment (§2.2) and never overwritten wholesale.

---

## 11. Concurrency model

```
run_season(league)                          run_cup(code, season)
 └─ ThreadPoolExecutor(TEAM_WORKERS=6)        └─ ThreadPoolExecutor(TEAM_WORKERS=6)
     └─ _scrape_team()                            └─ _scrape_team()  [cup_runner]
         └─ ThreadPoolExecutor(PLAYER_WORKERS=12)     └─ ThreadPoolExecutor(PLAYER_WORKERS=10)
             └─ _scrape_player()                          └─ _scrape_player()  [cup_runner]
```

League max theoretical concurrent HTTP requests: `6 × 12 = 72`. Cup: `6 ×
10 = 60`. Both share the same `cf_fetcher.fetch_html()` cascade
independently — there's no global rate limiter, which is why
`WEBSHARE_PROXY_URL`'s rotating IP pool matters (see §9.1 for what happens
without it).

Plus a separate small pool for the info-page fetches — `max_workers=7` for
league info, `run_cup_info()`'s 3 tasks run sequentially (not pooled).

---

## 12. GitHub Actions workflows

| Workflow | Trigger | Season | Leagues | `FULLSCRAPE` |
|---|---|---|---|---|
| `tm-scraper-manual.yml` | `workflow_dispatch` (manual) | input, or all seasons found on disk if blank | input, or all if blank | input, default `true` |
| `tm-scraper-yearly.yml` | cron `0 3 1 7 *` (03:00 UTC, July 1) + manual | always current season, auto-resolved | always all | always `true` |

**These workflows only run `runner.py` (the league pipeline).**
`cup_runner.py` is not currently wired into any GitHub Actions workflow —
it's run manually/locally for now. If you want CL/EURO/WC/national-teams
kept fresh automatically, that's a TODO: add a job calling
`python -m tm_scraper.cup_runner ...` per competition, ideally after the
league workflow so ID reuse (§2.2) is maximized.

**Important sequencing note:** neither workflow calls the football-data.org
`teams` step. For a season that has never been scraped before, the
football-data pipeline must be run first — see §1.

---

## 13. Known bugs & fixes log

### 13.1 `KeyError: 'player_id'` on every already-scraped cup player (fixed)

**Symptom:** `_scrape_team` in `cup_runner.py` crashed with
`KeyError: 'player_id'` for every player whose `player_information/{id}.json`
file already existed on disk, while newly-scraped players worked fine.

**Root cause:** `safe_write()` always wraps saved JSON as `{"_meta": {...},
"data": {...actual record...}}`. The cache-hit branch in `_scrape_player`
read the file and did `return json.loads(player_file.read_text())` —
returning the raw envelope, not the unwrapped `data` payload. The
roster-append site then did `rec["player_id"]` directly, which doesn't
exist at the envelope's top level.

**Fix applied:**
```python
raw = json.loads(player_file.read_text(encoding="utf-8"))
cached = raw.get("data", raw) if isinstance(raw, dict) else raw
if isinstance(cached, dict) and "player_id" in cached:
    return cached
```
plus defensive guards at the roster-append site (`isinstance(rec, dict)
and "player_id" in rec` instead of a bare `if rec:` + direct indexing) and
the final `roster.sort()` (guards against a `None` `player_id`).

**⚠️ The league pipeline (`runner.py`) has the identical root-cause bug,
just masked:** its cache-hit branch does the same unwrapped
`json.loads(...)` return, but the caller guards with `if result and
isinstance(result, dict) and "player_id" in result:` instead of indexing
directly — so instead of crashing, it **silently drops every
already-scraped player from `squad_roster` on any resumed run.** This has
not yet been patched in `runner.py`. If league squad rosters look thinner
than expected on repeat runs, this is why.

### 13.2 Playwright OOM crash under high local concurrency

See §9.1. Not a code bug per se — a resource-exhaustion issue from running
too many headless Chromium instances at once without `WEBSHARE_PROXY_URL`
or `SKIP_PLAYWRIGHT` set. Documented, not yet auto-mitigated (e.g. no
built-in cap on simultaneous Playwright launches).

---

## 14. Quick troubleshooting index

| Symptom in logs | Cause | Where to look |
|---|---|---|
| `Local teams DB not found: data/{season}/{league}/teams.json` | football-data `teams` step hasn't run for that season yet | §1, §12 |
| `Could not fetch HTML for <url> — skipping` | All 3 fetch methods in the cascade failed for that page | §9 |
| `Fatal process out of memory` / `[Playwright] Failed: Connection closed while reading from the driver` | Too many concurrent headless Chromium launches with no `WEBSHARE_PROXY_URL`/`SKIP_PLAYWRIGHT` set | §9.1, §13.2 |
| `KeyError: 'player_id'` in `_scrape_team` | Fixed — see §13.1. If seen again, check for a fresh regression in the cache-hit unwrap logic. | §13.1 |
| `[-] Unmatched team: X (Searched as: Y) — skipping` | Fuzzy name match failed against TM's club list | Add an entry to `TEAM_ALIASES` in `config_tm.py` (§5.1) |
| `[<code> <season>] 0 teams parsed` (cup) | `parse_cup_participants` mismatch — page structure may differ from assumed markup | §7.3 |
| `[league] Parser returned empty result for <file> — page may be CF-blocked or structure changed; not saving` | Either the fetch cascade actually returned blocked/challenge HTML, or TM changed the page's CSS classes | §4 |
| A JSON file never updates on non-fullscrape runs | `_file_has_data()` sees existing non-empty content and skips it — by design | Use `--fullscrape` |
| A player/team exists on Transfermarkt but never gets a file (league pipeline) | The football-data squad entry in `teams.json` is missing `id` or `name` | §2.1 — check `teams.json`, not the TM parsers |
| Player/team file saved under an unexpected number | Football-data ID (<1,000,000) or synthetic ID (≥1,000,000) — never Transfermarkt's own ID, which lives only in the `tm_player_id`/`tm_team_id` fields inside the JSON | §2.1, §2.2 |
| Same country appears twice under different `team_id`s | Name-matching miss — `id_registry.json` assigned two different synthetic IDs for name variants (e.g. "USA" vs "United States") that didn't fuzzy-match each other | Add a `TEAM_ALIASES`-style override, or manually merge the two registry entries in `id_registry.json` |