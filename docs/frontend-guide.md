# GOAL4U-DATA — Frontend Developer Reference

> Everything a UI developer needs to know about where data lives, what shape it's in, and how to consume it.

---

## Table of Contents

- [What This Project Does (in one picture)](#what-this-project-does-in-one-picture)
- [Folder Layout — Where Every File Lives](#folder-layout--where-every-file-lives)
- [The JSON Envelope — How Every File is Wrapped](#the-json-envelope--how-every-file-is-wrapped)
- [League Data — File by File](#league-data--file-by-file)
  - [competitionInfo.json](#competitioninfojson)
  - [matches.json](#matchesjson)
  - [standing.json](#standingjson)
  - [topScorer.json](#topscorerJson)
  - [teams.json](#teamsjson)
  - [match_stats_links.json](#match_stats_linksjson)
  - [stats.json](#statsjson)
- [Tournament Data — World Cup & Euro](#tournament-data--world-cup--euro)
- [Full Data Shape Reference](#full-data-shape-reference)
  - [Match Object](#match-object)
  - [Team Object](#team-object)
  - [Standing Row](#standing-row)
  - [Stats Entry (per match)](#stats-entry-per-match)
- [Competition Codes Cheat-Sheet](#competition-codes-cheat-sheet)
- [How Data Gets Here (Pipeline Summary)](#how-data-gets-here-pipeline-summary)
- [Config Knobs That Affect Output](#config-knobs-that-affect-output)
- [Common UI Patterns & Gotchas](#common-ui-patterns--gotchas)

---

## What This Project Does (in one picture)

```
football-data.org API  ──────────────────────────────────────┐
  (match results, standings, scorers, teams, lineups)        │
                                                             │ workers fetch + flatten
yallashoot.soccer (scraped HTML) ────────────────────────────┤
  (possession %, xG, shots, timeline events, player ratings) │
                                                             ▼
                                              JSON files on disk
                                              (this repo's /data/ folder)
                                                             │
                                                             ▼
                                              YOUR FRONTEND reads these files
                                              (directly, or via a FastAPI layer)
```

**You never call football-data.org or yallashoot.** Those calls happen in automated workers (GitHub Actions). The output is clean, pre-flattened JSON sitting on disk. Your frontend reads it.

---

## Folder Layout — Where Every File Lives

```
data/
│
├── 2025-2026/                    ← current league season (auto-resolved by config)
│   ├── PL/                       ← Premier League
│   │   ├── competitionInfo.json
│   │   ├── matches.json
│   │   ├── standing.json
│   │   ├── topScorer.json
│   │   ├── teams.json
│   │   ├── match_stats_links.json
│   │   └── stats.json            ← ALL match stats in one file, keyed by match_id
│   │
│   ├── PD/                       ← La Liga  (same 7 files)
│   ├── BL1/                      ← Bundesliga
│   ├── SA/                       ← Serie A
│   ├── FL1/                      ← Ligue 1
│   ├── CL/                       ← Champions League
│   └── ELC/                      ← Championship
│
├── world-cup/
│   └── world-cup-2026/           ← year from config.TOURNAMENT_YEARS["WC"]
│       ├── competitionInfo.json
│       ├── matches.json
│       ├── standing.json
│       ├── scorers.json          ← note: tournaments use "scorers.json"
│       ├── teams.json            ←        leagues use "topScorer.json"
│       ├── match_stats_links.json
│       └── stats.json
│
└── euros/
    └── euro-2024/                ← year from config.TOURNAMENT_YEARS["EC"]
        ├── competitionInfo.json
        ├── matches.json
        ├── standing.json
        ├── scorers.json
        ├── teams.json
        ├── match_stats_links.json
        └── stats.json
```

### Path formula

| Type | Formula |
|---|---|
| **League** | `data/{season}/{CODE}/{file}.json` |
| **World Cup** | `data/world-cup/world-cup-{year}/{file}.json` |
| **Euro** | `data/euros/euro-{year}/{file}.json` |

Current season is always resolved by `config.py`:
- Jan–May of a year → previous year's season (`2025-2026`)
- Jun–Dec of a year → current year's season (`2026-2027`)

---

## The JSON Envelope — How Every File is Wrapped

**Every single JSON file** written by this pipeline uses the same outer wrapper:

```json
{
  "_meta": {
    "last_synced": "2026-06-10T14:30:00+00:00",
    "source": "football-data.org v4"
  },
  "data": <actual payload here>
}
```

`_meta` is for your health checks and "last updated" UI labels.
`data` is what you actually render. It is either a list (`matches`, `teams`, `scorers`) or an object (`standing`, `stats`).

**`stats.json` has a slightly richer `_meta`:**

```json
{
  "_meta": {
    "last_synced": "...",
    "competition": "PL",
    "season": "2025-2026",
    "total_entries": 347
  },
  "data": { "494130": { ... }, "494131": { ... } }
}
```

**`match_stats_links.json` has its own `_meta` shape too** (it's written by a different scraper):

```json
{
  "_meta": {
    "last_synced": "...",
    "competition": "PL",
    "season": "2025-2026",
    "total_urls": 380,
    "matched": 371,
    "unmatched": 9
  },
  "data": [ ... ]
}
```

---

## League Data — File by File

### `competitionInfo.json`

Basic identity card for the competition. Rarely changes — updated once per season.

```json
{
  "_meta": { ... },
  "data": {
    "id": 2021,
    "name": "Premier League",
    "code": "PL",
    "type": "LEAGUE",
    "emblem": "https://...",
    "area": {
      "id": 2072,
      "name": "England",
      "code": "ENG",
      "flag": "https://..."
    },
    "currentSeason": {
      "id": 733,
      "startDate": "2025-08-14",
      "endDate": "2026-05-23",
      "currentMatchday": 38,
      "winner": null
    }
  }
}
```

`winner` is `null` during the season. After the final matchday it becomes a team object `{ "id": ..., "name": "..." }`.

---

### `matches.json`

All fixtures and results for the competition season. This is the most frequently updated file (daily audit, live matches checked every run).

```json
{
  "_meta": { ... },
  "data": [
    {
      "id": 494130,
      "competition_code": "PL",
      "area": { "id": 2072, "name": "England", "code": "ENG", "flag": "..." },
      "competition": {
        "id": 2021, "name": "Premier League",
        "code": "PL", "type": "LEAGUE", "emblem": "..."
      },
      "season": {
        "id": 733,
        "startDate": "2025-08-14",
        "endDate": "2026-05-23",
        "currentMatchday": 38,
        "winner": null
      },
      "utcDate": "2025-08-17T14:00:00Z",
      "status": "FINISHED",
      "matchday": 1,
      "stage": "REGULAR_SEASON",
      "group": null,
      "minute": null,
      "injuryTime": null,
      "attendance": 60234,
      "venue": "Emirates Stadium",
      "homeTeam": {
        "id": 57,
        "name": "Arsenal FC",
        "shortName": "Arsenal",
        "tla": "ARS",
        "crest": "...",
        "leagueRank": 1,
        "formation": "4-3-3",
        "coach": { "id": 99, "name": "Mikel Arteta", "nationality": "Spain" },
        "lineup": [ { "id": 1234, "name": "Raya", ... }, ... ],
        "bench":  [ { "id": 5678, "name": "Ramsdale", ... }, ... ]
      },
      "awayTeam": { ... },
      "score": {
        "winner": "HOME_TEAM",
        "duration": "REGULAR",
        "fullTime":    { "home": 2, "away": 1 },
        "halfTime":    { "home": 1, "away": 0 },
        "regularTime": { "home": 2, "away": 1 },
        "extraTime":   { "home": null, "away": null },
        "penalties":   { "home": null, "away": null }
      },
      "statistics": {
        "home": {
          "shots": 14, "shots_on_goal": 6, "shots_off_goal": 5,
          "possession": 58, "fouls": 11, "corner_kicks": 7,
          "yellow_cards": 1, "yellow_red_cards": 0, "red_cards": 0,
          "saves": 3, "offsides": 2
        },
        "away": { ... }
      },
      "goals": [
        {
          "minute": 23,
          "injuryTime": null,
          "type": "REGULAR",
          "team": { "id": 57, "name": "Arsenal FC" },
          "scorer": { "id": 44826, "name": "Bukayo Saka" },
          "assist": { "id": 11671, "name": "Martin Odegaard" },
          "score": { "home": 1, "away": 0 }
        }
      ],
      "bookings": [
        {
          "minute": 56,
          "team": { "id": 70, "name": "Brighton" },
          "player": { "id": 8812, "name": "João Pedro" },
          "card": "YELLOW"
        }
      ],
      "substitutions": [
        {
          "minute": 68,
          "team": { "id": 57, "name": "Arsenal FC" },
          "playerOut": { "id": 44826, "name": "Bukayo Saka" },
          "playerIn":  { "id": 2222, "name": "Leandro Trossard" }
        }
      ],
      "referees": [
        { "id": 11, "name": "Michael Oliver", "type": "REFEREE", "nationality": "England" }
      ],
      "odds": { "homeWin": null, "draw": null, "awayWin": null }
    }
  ]
}
```

#### Match status values

| Value | What it means |
|---|---|
| `TIMED` | Date and kick-off time confirmed |
| `SCHEDULED` | Date confirmed, no exact time yet |
| `IN_PLAY` | Live right now |
| `PAUSED` | Half-time |
| `FINISHED` | Final result confirmed |
| `POSTPONED` | Rescheduled — new date TBD |
| `SUSPENDED` | Stopped mid-game |
| `CANCELLED` | Won't be played |
| `AWARDED` | Forfeit / walkover |

`lineup` and `bench` arrays inside `homeTeam`/`awayTeam` are populated only after the match starts (and only if the API returned them). Before kick-off they are empty arrays `[]`.

`minute` is only non-null when `status` is `IN_PLAY` or `PAUSED`.

All score sub-objects (`extraTime`, `penalties`) are always present — they are `{ "home": null, "away": null }` when not applicable, never missing entirely.

---

### `standing.json`

League table. Updated weekly (or after any match changes during audit). Leagues have three standing types: `TOTAL`, `HOME`, `AWAY`.

```json
{
  "_meta": { ... },
  "data": {
    "competition_code": "PL",
    "display_title": "Premier League",
    "season": {
      "id": 733,
      "startDate": "2025-08-14",
      "endDate": "2026-05-23",
      "currentMatchday": 38
    },
    "standings": [
      {
        "stage": "REGULAR_SEASON",
        "type": "TOTAL",
        "group": null,
        "table": [
          {
            "position": 1,
            "team": {
              "id": 57,
              "name": "Arsenal FC",
              "shortName": "Arsenal",
              "tla": "ARS",
              "crest": "..."
            },
            "playedGames": 38,
            "form": "WWWDW",
            "won": 26,
            "draw": 6,
            "lost": 6,
            "points": 84,
            "goalsFor": 91,
            "goalsAgainst": 38,
            "goalDifference": 53
          },
          ...
        ]
      },
      {
        "stage": "REGULAR_SEASON",
        "type": "HOME",
        "group": null,
        "table": [ ... ]
      },
      {
        "stage": "REGULAR_SEASON",
        "type": "AWAY",
        "group": null,
        "table": [ ... ]
      }
    ]
  }
}
```

`form` is a 5-character string of `W`, `D`, `L` for the last 5 matches.

For **tournaments** (WC, EC), the `standings` array is split by group instead of by TOTAL/HOME/AWAY:

```json
"standings": [
  { "stage": "GROUP_STAGE", "type": "TOTAL", "group": "GROUP_A", "table": [ ... ] },
  { "stage": "GROUP_STAGE", "type": "TOTAL", "group": "GROUP_B", "table": [ ... ] },
  ...
]
```

---

### `topScorer.json`

Top scorers for the season. League competitions only — tournaments use `scorers.json` (same shape, different filename).

```json
{
  "_meta": { ... },
  "data": {
    "competition_code": "PL",
    "display_title": "Premier League",
    "season": {
      "id": 733,
      "startDate": "2025-08-14",
      "endDate": "2026-05-23",
      "currentMatchday": 38
    },
    "count": 10,
    "scorers": [
      {
        "player": {
          "id": 44826,
          "name": "Erling Haaland",
          "firstName": "Erling",
          "lastName": "Haaland",
          "dateOfBirth": "2000-07-21",
          "nationality": "Norway",
          "position": "Centre-Forward",
          "shirtNumber": 9
        },
        "team": {
          "id": 65,
          "name": "Manchester City FC",
          "shortName": "Man City",
          "tla": "MCI",
          "crest": "..."
        },
        "playedMatches": 35,
        "goals": 27,
        "assists": 5,
        "penalties": 4
      }
    ]
  }
}
```

Default limit is 10 scorers. Configured in `config.py` as `SCORERS_LIMIT`.

---

### `teams.json`

Full squad data for all clubs in the competition. One entry per club, including the starting coach and every registered player.

```json
{
  "_meta": { ... },
  "data": [
    {
      "id": 57,
      "name": "Arsenal FC",
      "shortName": "Arsenal",
      "tla": "ARS",
      "crest": "...",
      "website": "https://www.arsenal.com",
      "founded": 1886,
      "clubColors": "Red / White",
      "venue": "Emirates Stadium",
      "area": {
        "id": 2072,
        "name": "England",
        "code": "ENG",
        "flag": "..."
      },
      "area_code": "ENG",
      "activeCompetitions": [ ... ],
      "runningCompetitions": [ ... ],
      "marketValue": null,
      "coach": {
        "id": 99,
        "firstName": "Mikel",
        "lastName": "Arteta",
        "name": "Mikel Arteta",
        "nationality": "Spain",
        "dateOfBirth": "1982-03-26",
        "contract": { "start": "2020-12-26", "until": "2027-06-30" }
      },
      "squad": [
        {
          "id": 44826,
          "name": "Bukayo Saka",
          "firstName": "Bukayo",
          "lastName": "Saka",
          "position": "Right Winger",
          "dateOfBirth": "2001-09-05",
          "nationality": "England",
          "shirtNumber": 7,
          "marketValue": null,
          "contract": null
        },
        ...
      ],
      "staff": []
    }
  ]
}
```

`marketValue` and `contract` come from the API when available but are often `null` on the free tier.

---

### `match_stats_links.json`

The URL index that connects football-data.org match IDs to yallashoot.soccer match pages. You don't render this directly — it's used internally by `fetch_match_stats.py` to know which URL to scrape for each match.

```json
{
  "_meta": {
    "last_synced": "...",
    "competition": "PL",
    "season": "2025-2026",
    "total_urls": 380,
    "matched": 371,
    "unmatched": 9
  },
  "data": [
    {
      "match_id": 494130,
      "date": "2025-08-17",
      "home_team": "Arsenal FC",
      "away_team": "Brighton & Hove Albion FC",
      "url": "https://yallashoot.soccer/live/arsenal-brighton-2025-08-17/",
      "slug": "arsenal-brighton-2025-08-17"
    },
    {
      "match_id": null,
      "date": "2025-08-17",
      "home_team": null,
      "away_team": null,
      "url": "https://yallashoot.soccer/live/some-match-2025-08-17/",
      "slug": "some-match-2025-08-17"
    }
  ]
}
```

Entries with `match_id: null` are yallashoot pages the scraper found but couldn't match to a football-data.org match (low fuzzy-match score or date mismatch). You can safely ignore these.

---

### `stats.json`

**This is the richest file.** One entry per finished match, keyed by `str(match_id)`. Contains detailed in-match statistics from yallashoot: possession, expected goals, shots, cards, corners, timeline events with minute-by-minute detail, and full player lineups with ratings.

```json
{
  "_meta": {
    "last_synced": "...",
    "competition": "PL",
    "season": "2025-2026",
    "total_entries": 347
  },
  "data": {
    "494130": {
      "match_id": 494130,
      "fd_competition_code": "PL",
      "scraped_at": "2026-06-10T14:30:00+00:00",
      "source_url": "https://yallashoot.soccer/live/arsenal-brighton-2025-08-17/",
      "status": "full time",
      "home_team": "Arsenal",
      "away_team": "Brighton",
      "score": {
        "home": 2,
        "away": 1,
        "ht_home": 1,
        "ht_away": 0
      },
      "stats": {
        "corners":        { "home": 8,      "away": 3    },
        "possession":     { "home": "58%",  "away": "42%" },
        "shots":          { "home": 16,     "away": 9    },
        "shots_on_target":{ "home": 6,      "away": 4    },
        "shots_off_goal": { "home": 7,      "away": 3    },
        "blocked_shots":  { "home": 3,      "away": 2    },
        "fouls":          { "home": 11,     "away": 14   },
        "offsides":       { "home": 2,      "away": 1    },
        "yellow_cards":   { "home": 1,      "away": 2    },
        "red_cards":      { "home": 0,      "away": 0    },
        "expected_goals": { "home": 1.82,   "away": 0.94 },
        "goalkeeper_saves":{ "home": 3,     "away": 4    },
        "total_passes":   { "home": 542,    "away": 381  },
        "passes_accurate":{ "home": 478,    "away": 312  }
      },
      "events": [
        {
          "minute": 23,
          "type": "goal",
          "team": "home",
          "player": "Bukayo Saka",
          "goal_type": "normal",
          "assistant": "Martin Odegaard"
        },
        {
          "minute": 34,
          "type": "yellow_card",
          "team": "away",
          "player": "João Pedro"
        },
        {
          "minute": 67,
          "type": "substitution",
          "team": "home",
          "player": "Leandro Trossard",
          "player_out": "Bukayo Saka"
        },
        {
          "minute": 72,
          "type": "goal",
          "team": "home",
          "player": "Gabriel Martinelli",
          "goal_type": "penalty"
        },
        {
          "minute": 88,
          "type": "red_card",
          "team": "away",
          "player": "Danny Welbeck"
        }
      ],
      "lineups": {
        "home": {
          "formation": null,
          "starting": [
            { "name": "David Raya",    "number": "1",  "position": "Goalkeeper", "rating": "7.2" },
            { "name": "Ben White",     "number": "2",  "position": "Defender",   "rating": "7.0" },
            ...
          ],
          "subs": [
            { "name": "Karl Hein",     "number": "34", "position": "Goalkeeper", "rating": null  },
            ...
          ],
          "coach": "Mikel Arteta"
        },
        "away": {
          "formation": null,
          "starting": [ ... ],
          "subs":     [ ... ],
          "coach": "Fabian Hürzeler"
        }
      }
    },
    "494131": { ... }
  }
}
```

#### `events[].type` values

| Value | What it is |
|---|---|
| `goal` | A scored goal. Has `goal_type` and optional `assistant` |
| `yellow_card` | Yellow card booking |
| `red_card` | Red card (includes second-yellow reds) |
| `substitution` | Substitution. `player` = coming on, `player_out` = going off |
| `penalty_missed` | Penalty attempt that didn't score |
| `other` | Anything else the scraper found |

#### `events[].goal_type` values

| Value | What it is |
|---|---|
| `normal` | Regular open-play goal |
| `penalty` | Scored from the penalty spot |
| `own_goal` | Own goal (credited to the other team) |

`stats` keys are present only when the source page had them. Some matches may not have `expected_goals` or `total_passes` if yallashoot didn't publish those stats for that game.

`lineups[side].rating` is a string like `"7.2"` or `null` if not available.

---

## Tournament Data — World Cup & Euro

Tournaments have the **same 7 files** as leagues, with two differences:

**Filename difference:** `scorers.json` instead of `topScorer.json`.

**Standings difference:** `standing.json` is split by group (`GROUP_A`, `GROUP_B`, ...) not by TOTAL/HOME/AWAY. Knockout-round matches (Quarter-Final, Semi-Final, Final) appear in `matches.json` with `stage` values like `QUARTER_FINAL`, `SEMI_FINAL`, `FINAL` and `group: null`.

**Path difference:** See folder layout above — they live outside the season folder.

---

## Full Data Shape Reference

### Match Object

Every field is **always present**. Fields that are not yet applicable are `null`, never missing.

| Field | Type | Notes |
|---|---|---|
| `id` | `int` | Unique match ID from football-data.org |
| `competition_code` | `string` | e.g. `"PL"` |
| `utcDate` | `string` | ISO 8601 UTC e.g. `"2025-08-17T14:00:00Z"` |
| `status` | `string` | See status table above |
| `matchday` | `int \| null` | Matchday number within season |
| `stage` | `string` | `"REGULAR_SEASON"`, `"GROUP_STAGE"`, `"QUARTER_FINAL"`, etc. |
| `group` | `string \| null` | `"GROUP_A"` etc. for tournaments; `null` for leagues |
| `minute` | `int \| null` | Current minute if `IN_PLAY` or `PAUSED` |
| `injuryTime` | `int \| null` | Added minutes if applicable |
| `attendance` | `int \| null` | |
| `venue` | `string \| null` | Stadium name |
| `homeTeam` | `object` | See below |
| `awayTeam` | `object` | Same shape as `homeTeam` |
| `score.winner` | `string \| null` | `"HOME_TEAM"`, `"AWAY_TEAM"`, `"DRAW"`, or `null` |
| `score.duration` | `string \| null` | `"REGULAR"`, `"EXTRA_TIME"`, `"PENALTIES"` |
| `score.fullTime` | `{home, away}` | Both `null` if not yet finished |
| `score.halfTime` | `{home, away}` | Both `null` before half-time |
| `score.regularTime` | `{home, away}` | Both `null` if N/A |
| `score.extraTime` | `{home, away}` | Both `null` if match didn't go to extra time |
| `score.penalties` | `{home, away}` | Both `null` if no shootout |
| `statistics` | `{home: {...}, away: {...}}` | API-level stats (pre-match only on many plans) |
| `goals` | `array` | Empty `[]` before kick-off |
| `bookings` | `array` | Empty `[]` before kick-off |
| `substitutions` | `array` | Empty `[]` before kick-off |
| `referees` | `array` | |
| `odds` | `{homeWin, draw, awayWin}` | Usually all `null` on free tier |

**`homeTeam` / `awayTeam` fields:**

| Field | Type | Notes |
|---|---|---|
| `id` | `int` | |
| `name` | `string` | Full name e.g. `"Arsenal FC"` |
| `shortName` | `string` | e.g. `"Arsenal"` |
| `tla` | `string` | 3-letter code e.g. `"ARS"` |
| `crest` | `string \| null` | Crest image URL |
| `leagueRank` | `int \| null` | Position in table at time of match |
| `formation` | `string \| null` | e.g. `"4-3-3"` — populated after kick-off |
| `coach` | `object \| null` | `{id, name, nationality}` |
| `lineup` | `array` | Player objects — empty before match starts |
| `bench` | `array` | Player objects — empty before match starts |

---

### Team Object (from `teams.json`)

| Field | Type |
|---|---|
| `id` | `int` |
| `name` | `string` — full name |
| `shortName` | `string` |
| `tla` | `string` — 3-letter code |
| `crest` | `string \| null` |
| `website` | `string \| null` |
| `founded` | `int \| null` |
| `clubColors` | `string \| null` e.g. `"Red / White"` |
| `venue` | `string \| null` |
| `area` | `{id, name, code, flag}` |
| `area_code` | `string` e.g. `"ENG"` — convenience duplicate of `area.code` |
| `coach` | `{id, firstName, lastName, name, nationality, dateOfBirth, contract}` |
| `squad` | Array of player objects (see below) |
| `staff` | Array — usually empty |

**Player object (inside `squad`):**

| Field | Type |
|---|---|
| `id` | `int` |
| `name` | `string` |
| `firstName` | `string \| null` |
| `lastName` | `string \| null` |
| `position` | `string \| null` e.g. `"Centre-Forward"` |
| `dateOfBirth` | `string \| null` e.g. `"2001-09-05"` |
| `nationality` | `string \| null` |
| `shirtNumber` | `int \| null` |
| `marketValue` | `null` (not populated on free tier) |
| `contract` | `null` (not populated on free tier) |

---

### Standing Row

Each entry in any `standings[n].table` array:

| Field | Type |
|---|---|
| `position` | `int` |
| `team` | `{id, name, shortName, tla, crest}` |
| `playedGames` | `int` |
| `form` | `string` — 5 chars of `W`/`D`/`L`, e.g. `"WWDLW"` |
| `won` | `int` |
| `draw` | `int` |
| `lost` | `int` |
| `points` | `int` |
| `goalsFor` | `int` |
| `goalsAgainst` | `int` |
| `goalDifference` | `int` |

---

### Stats Entry (per match, from `stats.json`)

Access via `data[String(matchId)]`.

| Field | Type | Notes |
|---|---|---|
| `match_id` | `int` | Same as the key, cast back to int |
| `fd_competition_code` | `string` | e.g. `"PL"` |
| `scraped_at` | `string` | ISO 8601 timestamp of when this was scraped |
| `source_url` | `string` | yallashoot URL |
| `status` | `string \| null` | Page-level status string e.g. `"full time"`, `"ft"` |
| `home_team` | `string \| null` | Name as on yallashoot page (may differ slightly from fd name) |
| `away_team` | `string \| null` | |
| `score.home` | `int \| null` | Final score |
| `score.away` | `int \| null` | |
| `score.ht_home` | `int \| null` | Half-time score |
| `score.ht_away` | `int \| null` | |
| `stats` | `object` | Keys vary by match; see stat key table below |
| `events` | `array` | Sorted by `minute` ascending |
| `lineups.home` | `object` | `{formation, starting, subs, coach}` |
| `lineups.away` | `object` | Same shape |

**Known stat keys** (not all present in every match):

| Key | What it measures |
|---|---|
| `corners` | Corner kicks |
| `possession` | Ball possession — value is `"58%"` (string with `%`) |
| `shots` | Total shots |
| `shots_on_target` | Shots on target |
| `shots_off_goal` | Shots off goal |
| `blocked_shots` | Blocked shots |
| `shots_insidebox` | Shots from inside the box |
| `shots_outsidebox` | Shots from outside the box |
| `fouls` | Fouls committed |
| `offsides` | Offside calls |
| `yellow_cards` | Yellow cards |
| `red_cards` | Red cards |
| `expected_goals` | xG — float e.g. `1.82` |
| `goalkeeper_saves` | Saves by goalkeeper |
| `total_passes` | Total passes attempted |
| `passes_accurate` | Passes completed |
| `goals` | Goals (redundant with score but present when scraped) |

All stat values are `{ "home": <value>, "away": <value> }` where value is `int`, `float`, or `string` (only for possession).

---

## Competition Codes Cheat-Sheet

| Code | Competition | Country | Data folder |
|---|---|---|---|
| `PL` | Premier League | England | `data/{season}/PL/` |
| `PD` | La Liga | Spain | `data/{season}/PD/` |
| `BL1` | Bundesliga | Germany | `data/{season}/BL1/` |
| `SA` | Serie A | Italy | `data/{season}/SA/` |
| `FL1` | Ligue 1 | France | `data/{season}/FL1/` |
| `CL` | Champions League | Europe | `data/{season}/CL/` |
| `ELC` | Championship | England | `data/{season}/ELC/` |
| `WC` | FIFA World Cup | International | `data/world-cup/world-cup-{year}/` |
| `EC` | UEFA Euro | Europe | `data/euros/euro-{year}/` |

Current season year is resolved by `config.py` based on today's date (switches in June).
Tournament years are fixed in `config.TOURNAMENT_YEARS` — currently `WC: 2026`, `EC: 2024`.

---

## How Data Gets Here (Pipeline Summary)

The pipeline runs via `python main.py` or via individual worker scripts. Steps run in this order:

```
1. competitions   → writes competitionInfo.json, standing.json, topScorer.json
2. teams          → writes teams.json
3. tm_scraper     → Transfermarkt enrichment (separate data folder — see below)
4. matches        → writes matches.json
5. match_links    → writes match_stats_links.json  (needs matches.json to exist first)
6. match_stats    → writes stats.json              (needs match_stats_links.json first)
7. worldcup       → writes all 7 files into data/world-cup/world-cup-{year}/
8. euro           → writes all 7 files into data/euros/euro-{year}/
```

You can run any single step in isolation:

```bash
python main.py --only matches --competition PL        # PL matches only
python main.py --only worldcup --mode standings       # WC standings only
python main.py --only match_stats --workers 12        # parallel stats scrape
python main.py --only match_stats --force             # re-scrape all matches
```

### Audit mode (incremental updates)

The daily audit (`audit_matches.py`) only re-fetches individual matches that are stale — it does not re-download the entire competition. A match is considered stale if:
- Status is `IN_PLAY` or `PAUSED` (always re-fetched)
- Status is `POSTPONED` (always re-fetched — needs new date)
- Status is `FINISHED` but score, goals list, or lineups are missing
- Status is `TIMED`/`SCHEDULED` and kick-off is within 2 hours

This means most FINISHED matches are never touched again after their data is complete.

---

## Config Knobs That Affect Output

Everything below is in `config.py`:

| Setting | Current value | What it controls |
|---|---|---|
| `LEAGUE_COMPETITIONS` | `["PL","PD","BL1","FL1","SA","CL","ELC"]` | Which leagues get processed. Add a code here to start tracking a new league. |
| `TOURNAMENT_YEARS["WC"]` | `2026` | Which World Cup folder is written/read |
| `TOURNAMENT_YEARS["EC"]` | `2024` | Which Euro folder is written/read |
| `_NEW_SEASON_FROM_MONTH` | `6` (June) | When the "current season" flips to the next year |
| `SCORERS_LIMIT` | `10` | How many top scorers are stored in `topScorer.json` / `scorers.json` |

**To add a new league:** add its code to `LEAGUE_COMPETITIONS`. On the next pipeline run, all 7 files will be created at `data/{season}/{CODE}/`.

**To update the tournament year** (e.g. next Euro): change `TOURNAMENT_YEARS["EC"]` to the new year. The output folder path changes automatically.

---

## Common UI Patterns & Gotchas

**Reading a match from stats.json:**
```js
// stats.json data is an object, not an array
const entry = statsData.data[String(matchId)]  // always stringify the ID
if (!entry) { /* stats not scraped yet for this match */ }
```

**Possession is a string with %:**
```js
// Don't try to do math on it directly
const poss = entry.stats.possession?.home  // "58%"
const possNum = parseFloat(poss)           // 58
```

**Lineups are empty before kick-off:**
```js
// Always check before rendering
const hasLineup = match.homeTeam.lineup?.length > 0
```

**Scorers file name differs between leagues and tournaments:**
```
Leagues:     data/{season}/{CODE}/topScorer.json
Tournaments: data/world-cup/world-cup-{year}/scorers.json
             data/euros/euro-{year}/scorers.json
```

**Tournament standings are grouped differently:**
```js
// Leagues: standing.data.standings has TOTAL, HOME, AWAY entries
// Tournaments: standing.data.standings has GROUP_A, GROUP_B, ... entries
const isGroupStage = standing.data.standings[0]?.group !== null
```

**`stats.json` entry may exist but be empty:**
The scraper can save a "shell" entry if the yallashoot page loaded but hadn't rendered data yet. Always check the key fields before rendering:
```js
const hasStats = entry.home_team !== null && Object.keys(entry.stats).length > 0
```

**All dates in `matches.json` are UTC:**
`utcDate` is always `"YYYY-MM-DDTHH:MM:SSZ"`. Convert to local time in the browser before displaying.

**`display_title` in standings/scorers includes the year for tournaments:**
```
"Premier League"      ← leagues
"FIFA World Cup 2026" ← tournaments (year appended by pipeline)
```

**`match_id` in `stats.json` is an integer, but the key is a string:**
```js
// Both are equivalent to look up
statsData.data["494130"]   // ✓ key access (string)
statsData.data[494130]     // ✗ won't work — JS object keys are strings
```

**Events are sorted by minute ascending**, but minute `90` can appear before `90+1`. Sort by `minute` only if you need strict chronological order within injury time periods.

**`stats.json` entries for tournament matches** live in the tournament folder, not the league season folder. When looking up a World Cup match's stats, read from `data/world-cup/world-cup-2026/stats.json`, not `data/2025-2026/{CODE}/stats.json`.