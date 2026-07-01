# TM Data — Frontend / UI Developer Guide

This is **not** the scraper internals doc (see `tm_scraper_guide.md` for
that). This is the "I'm building a UI or API on top of this data and don't
care how it got there" doc. Everything below describes what you'll find on
disk under `data/` and `public/assets/`, and how to read it safely.

---

## 1. The three folders you'll actually touch

```
data/league_info/{CODE}/...          competitions — leagues AND cups
data/team_informations/{team_id}/... clubs AND national teams
data/player_information/{id}.json    every player, one flat folder
public/assets/player_images/{id}.jpg
public/assets/trophies/{safe_name}.jpg
```

That's the whole surface area. Leagues (`GB1`, `ES1`, `IT1`, `L1`, `FR1`)
and cups (`CL`, `EURO`, `WC`) live side by side under the same
`league_info/` folder — same for club vs. national-team folders under
`team_informations/`. There is no separate "cup" or "national team" tree to
know about; the `{CODE}` or `{id}` you're already using is the only thing
that changes.

---

## 2. Every file has the same envelope — always unwrap `.data`

**Every single JSON file**, no exceptions, looks like this:

```json
{
  "_meta": {
    "last_synced": "2026-06-30T14:22:01.123456+00:00",
    "source": "football-data.org v4"
  },
  "data": { ... the thing you actually want ... }
}
```

Always read `.data`, never the top level. This trips people up constantly
(see §7) — write one small loader and use it everywhere:

```js
async function loadTmJson(path) {
  const res = await fetch(path);
  if (!res.ok) return null;
  const raw = await res.json();
  return raw?.data ?? null;
}
```

```python
import json

def load_tm_json(path):
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return raw.get("data") if isinstance(raw, dict) else raw
```

`_meta.last_synced` is handy for "data as of ..." labels in the UI.

---

## 3. IDs: the one rule that matters

Every team and every player has an integer-ish `id` used **as the
filename/folder name**. Two ranges:

| Range | Meaning |
|---|---|
| `< 1,000,000` | Real football-data.org ID — this entity also exists in the upstream football-data pipeline's `teams.json` |
| `≥ 1,000,000` | Synthetic ID, assigned locally (national teams, non-tracked clubs — anything with no football-data.org record) |

You never need to compute or care *which* ID an entity has — just treat
`team_id` / `player_id` as an opaque key into `team_informations/` /
`player_information/`. The range only matters if you're debugging why two
entities that should be the same aren't (see §7).

**A separate field, `tm_player_id` / `tm_team_id`, also exists in most
records** — that's Transfermarkt's *own* internal ID, useful only if you
want to deep-link to the actual Transfermarkt page. Don't use it to look up
local files; it's never a folder/filename.

---

## 4. `league_info/{CODE}/` — competitions

`{CODE}` is one of: `GB1` `ES1` `IT1` `L1` `FR1` (leagues) or `CL` `EURO`
`WC` (cups).

### Leagues — flat, no season folder, always "current/all-time"
```
league_info/GB1/
├── league_metadata.json          overview stats — squads, market value, current champion
├── top_scorers.json              golden boot, one row per season (all seasons in one file)
├── successful_players.json       most-decorated players, ranked
├── all_champions.json            title history, one row per season
├── championship_managers.json    winning-est managers, ranked
├── market_values.json            current top-valued players in the league
└── players_of_year.json          award winners, one row per (season, award)
```

### Cups — split into all-time + per-edition
```
league_info/CL/
├── top_scorers_all_time.json         spans every edition ever played
└── {season_label}/
    ├── market_values.json            this edition's most valuable players
    └── top_scorers.json               this edition's top scorers
```

`{season_label}` examples: `"2025-2026"` (CL), `"2024"` (Euro 2024),
`"2022"` (World Cup 2022), or `"current"` for an edition without a
confirmed TM season ID yet. **If you're building a season picker, list the
subfolders of `league_info/{CUP_CODE}/` rather than hardcoding years** —
that's the authoritative list of editions actually scraped.

Cups don't have `league_metadata.json`, `successful_players.json`,
`all_champions.json`, `championship_managers.json`, or
`players_of_year.json` — those are league-only concepts. Don't build a UI
that assumes every `{CODE}` folder has all 7 files; branch on whether
`{CODE}` is a league or a cup.

---

## 5. `team_informations/{team_id}/` — clubs and national teams

```
team_informations/{team_id}/
├── team_metadata.json            club/country facts + trophies — ONE file, always current
├── {season}-squad.json           league squad, e.g. "2025-2026-squad.json"
├── cl-{season_label}.json        Champions League squad for that edition
├── euro-{season_label}.json      Euro squad for that edition
├── world-cup-{season_label}.json World Cup squad for that edition
└── national-team-current.json    FIFA-ranking-sourced roster (no tournament tie-in)
```

**Important UI implication:** a folder can contain *multiple squad files*
because the same entity plays in multiple competitions. Don't assume one
squad per team folder — **list the directory and pattern-match filenames**
to figure out which competitions/seasons that team has data for:

```js
// pseudo — list files, classify by filename pattern
const files = await listDir(`data/team_informations/${teamId}/`);
const squads = files.filter(f => f !== "team_metadata.json");
// "2025-2026-squad.json"      → league squad, season = "2025-2026"
// "cl-2025-2026.json"         → Champions League, edition = "2025-2026"
// "euro-2024.json"            → Euro, edition = "2024"
// "world-cup-2026.json"       → World Cup, edition = "2026"
// "national-team-current.json"→ national-team snapshot, no edition
```

A simple regex covers all of them:
```
^(?:(?<season>\d{4}-\d{4})-squad|(?<comp>cl|euro|world-cup|national-team)-(?<label>[\w-]+))\.json$
```

`team_metadata.json` is **shared and overwritten in place** — there is only
ever one per team, regardless of how many competitions it plays in. Don't
expect a per-competition version of it.

### `team_metadata.json` shape
```json
{
  "team_id": 66,
  "tm_name": "Real Madrid",
  "tm_url": "https://www.transfermarkt.../real-madrid/startseite/verein/418",
  "tm_stats": {
    "squad_size": "...", "average_age": "...", "foreigners": "...",
    "national_team": "...", "league_level": "...", "stadium": "...",
    "coach": "...", "total_market_value": "..."
  },
  "trophies": [
    {"name": "League Title", "safe_name": "league_title", "count": "3",
     "local_path": "/assets/trophies/league_title.jpg", "source_url": "..."}
  ]
}
```
Trophy images: `public` + `local_path` gives you the servable URL directly.

### Squad file shape (same shape for every competition type)
```json
{
  "team_id": 66,
  "season": "2025-2026",          // or "competition": "CL", "season": "2025-2026" for cups
  "squad_roster": [
    {"player_id": "...", "name": "...", "position": "..."}
  ]
}
```
`squad_roster` gives you names/positions/IDs only — for full bio/stats,
follow `player_id` into `player_information/`.

---

## 6. `player_information/{player_id}.json` — every player, everywhere

One flat folder. A player who's in a domestic league squad *and* a
national team squad *and* a Champions League squad still has exactly **one**
file here — shared and reused across every competition that player
appears in.

```json
{
  "player_id": "315858",
  "name": "Gianluigi Donnarumma",
  "tm_url": "https://www.transfermarkt.../.../profil/spieler/315858",
  "image_path": "/assets/player_images/315858.jpg",
  "details": {
    "market_value": "...", "mv_last_update": "...", "shirt_number": "...",
    "is_captain": false,
    "achievements": [ {"trophy": "...", "count": "...", "img_url": "..."} ],
    "date_of_birth": "...", "place_of_birth": "...", "height": "...",
    "citizenship": ["..."], "foot": "...",
    "current_club_name": "...", "current_club_url": "...",
    "joined": "...", "contract_expires": "...",
    "player_agent": "...", "outfitter": "...",
    "social_media": ["..."],
    "national_caps": "...", "national_goals": "...",
    "national_team_name": "...", "national_team_url": "...",
    "position": "...", "other_positions": ["..."],
    "youth_clubs": ["..."], "further_information": "...",
    "transfer_component_metadata": { ... }
  },
  "trophies": [ ... same shape as team trophies ... ]
}
```

`image_path` may be `null` if no image was ever found/downloaded on TM —
always guard for that before rendering an `<img>`.

`details` is **dynamically keyed** off whatever labels Transfermarkt showed
for that specific player — not every player has every field. Treat
`details` as a loosely-typed bag, not a fixed schema; render only the keys
that are present rather than assuming a complete set.

---

## 7. Gotchas worth knowing before you build against this

**A) The envelope.** Forgetting to unwrap `.data` is the #1 way to get
`undefined`/`KeyError` reading these files. See §2 — this bit the scraper's
own code too (a cache-read bug once returned the raw envelope instead of
the unwrapped record).

**B) `player_id` can be `null`.** In a few historical/all-time league
tables (`successful_players.json`, `championship_managers.json` — managers
never have one at all), a legend predates football-data.org's tracking
window and only `tm_player_id` is populated. Don't assume `player_id` is
always a safe key to `player_information/` — check for `null` first, and if
so, there's no local profile file for that person (only what's inline in
that ranking row).

**C) Not every `{CODE}` folder has the same files.** Leagues have 7 files;
cups have a different, smaller set split by edition folder. See §4.

**D) A team folder can have zero, one, or several squad files.** Don't
assume `{season}-squad.json` exists just because `team_metadata.json`
does — a national team scraped only via the FIFA-ranking pass has
`national-team-current.json` and nothing else until it's actually scraped
for a specific tournament.

**E) Synthetic IDs (≥1,000,000) can occasionally duplicate an entity under
two different IDs** if name-matching missed a variant (e.g. "USA" vs
"United States" not fuzzy-matching each other). If you notice the same
country/club appearing twice with different `team_id`s, that's a known
class of data issue, not a UI bug — flag it upstream rather than trying to
dedupe client-side by name (names aren't guaranteed unique/consistent
across files).

**F) Trophy image filenames are content-derived, not ID-based**
(`safe_name` is a slug of the trophy's display name), so two different
teams' identically-named trophy ("League Title") **share the same image
file** on disk — this is intentional (it's generic artwork, not a
photo of a specific specific trophy instance), don't be surprised if you
see the same file path referenced from multiple teams.

**G) `_meta.last_synced` is per-file, not per-dataset.** Two files in the
same team's folder can have different sync timestamps (e.g. the league
squad was refreshed today, the CL squad three weeks ago) — don't assume a
single "last updated" applies to a whole entity.

**H) Everything is read-only from your side.** These are static JSON files
on disk, not a live API — if you're serving them to a frontend, you'll
want a thin file-serving layer (or a build step that indexes/copies them
into your app), not a database query. There is no live "player X's market
value right now" — it's only as fresh as the last scrape run.

---

## 8. Suggested indexing step (if you're building an API, not just static files)

Because there's no manifest file listing "every player" or "every team",
if you need list/search endpoints, build a small index once at build/deploy
time rather than scanning the filesystem per-request:

```python
import json
from pathlib import Path

def build_player_index():
    index = []
    for f in Path("data/player_information").glob("*.json"):
        rec = json.loads(f.read_text())["data"]
        index.append({
            "player_id": rec.get("player_id"),
            "name": rec.get("name"),
            "current_club_name": rec.get("details", {}).get("current_club_name"),
        })
    return index

def build_team_squad_index():
    """team_id -> list of {kind, season_or_label, filename}"""
    out = {}
    for team_dir in Path("data/team_informations").iterdir():
        squads = []
        for f in team_dir.glob("*.json"):
            if f.name == "team_metadata.json":
                continue
            squads.append(f.name)
        out[team_dir.name] = squads
    return out
```

Re-run this whenever the scraper runs (or on a schedule) and cache the
result — don't rebuild it per HTTP request.

---

## 9. Quick reference — "I want to build ___, what do I read?"

| UI feature | Files to read |
|---|---|
| League table / overview page | `league_info/{CODE}/league_metadata.json` |
| "All-time top scorers" page (league) | `league_info/{CODE}/top_scorers.json` |
| "All-time top scorers" page (cup) | `league_info/{CODE}/top_scorers_all_time.json` |
| This season's top scorers (cup) | `league_info/{CODE}/{season_label}/top_scorers.json` |
| Market-value leaderboard | `league_info/{CODE}/market_values.json` (league) or `.../{season_label}/market_values.json` (cup) |
| Club profile page | `team_informations/{team_id}/team_metadata.json` |
| Club's trophy cabinet | `team_informations/{team_id}/team_metadata.json` → `.trophies` |
| Club's current league squad | `team_informations/{team_id}/{season}-squad.json` |
| Club's Champions League squad for an edition | `team_informations/{team_id}/cl-{season_label}.json` |
| National team's World Cup / Euro squad | `team_informations/{team_id}/world-cup-{label}.json` / `euro-{label}.json` |
| Player profile page | `player_information/{player_id}.json` |
| Player headshot | `player_information/{player_id}.json` → `.image_path`, or directly `public/assets/player_images/{player_id}.jpg` |
| "Which competitions has this team played in that we have data for?" | List `team_informations/{team_id}/` and classify filenames (§5) |
| Season/edition picker for a cup | List subfolders of `league_info/{CUP_CODE}/` |

---

## 10. TL;DR for a new dev on day one

1. Read `.data`, ignore `_meta` (except for a "last updated" label).
2. `team_id`/`player_id` are opaque keys — don't parse or compute them,
   just use them to build a path.
3. A team folder can hold many squad files (one per competition/season) —
   list the directory, don't assume a fixed filename.
4. Leagues and cups share the same `league_info/` tree but have different
   file sets — branch on competition type before assuming a file exists.
5. Some `player_id`/`team_id` fields can be `null` in historical tables —
   guard before using them as a lookup key.
6. Nothing here is live — it's a snapshot as of the last scrape run.