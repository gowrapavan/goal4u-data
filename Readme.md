# GOAL4U-DATA

> Football data pipeline that collects structured match data, scrapes advanced statistics, and enriches player profiles — producing normalized JSON datasets that any frontend or API can consume directly.

---

## What It Does

```
football-data.org API       ──┐
  Matches, standings,          │  Python workers
  scorers, teams, lineups      │  (GitHub Actions)
                               │
yallashoot.soccer (scraped)  ──┤──▶  JSON files on disk  ──▶  Your frontend
  Possession, xG, shots,       │     /data/
  events, player ratings       │
                               │
transfermarkt.co.in (scraped)──┘
  Market values, trophies,
  player profiles, history
```

No rate limits hit by your frontend. No API key exposed. The upstream sources are called once per scheduled interval by automated workers — your UI reads pre-built JSON files.

---

## Data Sources

| Source | What it provides | How often |
|---|---|---|
| football-data.org | Matches, standings, scorers, teams, lineups | Daily (audit) / Weekly (standings) |
| yallashoot.soccer | In-match stats, xG, events, lineups with ratings | Per finished match |
| transfermarkt.co.in | Market values, trophies, player bios, league history | Bi-monthly (teams change slowly) |

---

## Supported Competitions

| Code | Competition | Country |
|---|---|---|
| `PL` | Premier League | England |
| `PD` | La Liga | Spain |
| `BL1` | Bundesliga | Germany |
| `SA` | Serie A | Italy |
| `FL1` | Ligue 1 | France |
| `CL` | UEFA Champions League | Europe |
| `ELC` | Championship | England |
| `WC` | FIFA World Cup | International |
| `EC` | UEFA Euro | Europe |

---

## Folder Structure

```
data/
├── 2025-2026/                          ← current league season
│   └── {CODE}/                         ← one folder per competition code
│       ├── competitionInfo.json
│       ├── matches.json
│       ├── standing.json
│       ├── topScorer.json
│       ├── teams.json
│       ├── match_stats_links.json
│       └── stats.json
│
├── world-cup/world-cup-2026/           ← World Cup (same 7 files, scorers.json not topScorer.json)
├── euros/euro-2024/                    ← UEFA Euro (same shape)
│
├── league_info/                        ← Transfermarkt league-level data
│   └── {CODE}/
│       ├── league_metadata.json
│       ├── top_scorers.json
│       ├── successful_players.json
│       ├── all_champions.json
│       ├── championship_managers.json
│       ├── market_values.json
│       └── players_of_year.json
│
├── team_informations/                  ← Transfermarkt team data
│   └── {team_id}/
│       ├── team_metadata.json
│       └── {season}-squad.json
│
└── player_information/                 ← Transfermarkt player profiles
    └── {player_id}.json                ← keyed by football-data.org player ID

public/
└── assets/
    ├── player_images/
    │   └── {player_id}.jpg             ← served statically, same ID as player_information/
    └── trophies/
        └── {trophy_safe_name}.jpg
```

---

## Quick Start

```bash
git clone https://github.com/gowrapava/goal4u-data.git
cd goal4u-data
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FOOTBALL_DATA_API_KEY=your_key_here

# Full pipeline — fetches everything for the current season
python main.py

# Leagues only, no tournaments
python main.py --skip worldcup euro

# Single competition
python main.py --competition PL --only matches

# Transfermarkt enrichment only
python main.py --only tm_scraper

# World Cup only
python main.py --only worldcup
```

---

## Pipeline Steps (in order)

| Step | What it does | Output |
|---|---|---|
| `competitions` | League metadata, standings, scorers | `competitionInfo.json`, `standing.json`, `topScorer.json` |
| `teams` | Club squads and coach data | `teams.json` |
| `tm_scraper` | Transfermarkt enrichment (market values, trophies, bios) | `league_info/`, `team_informations/`, `player_information/` + image assets |
| `matches` | Fixtures and results | `matches.json` |
| `match_links` | Maps yallashoot URLs to match IDs | `match_stats_links.json` |
| `match_stats` | Scrapes detailed in-match stats | `stats.json` |
| `worldcup` | All World Cup data (self-contained) | `data/world-cup/world-cup-{year}/` |
| `euro` | All UEFA Euro data (self-contained) | `data/euros/euro-{year}/` |

---

## Documentation

| Document | What's in it |
|---|---|
| [`docs/data-reference.md`](docs/data-reference.md) | Every JSON file explained with full example payloads, field tables, and UI gotchas |
| [`docs/transfermarkt-guide.md`](docs/transfermarkt-guide.md) | Transfermarkt scraper output: all files, image paths, player profile shape, how to look up by player ID |

---

## Configuration (`config.py`)

| Setting | Default | What it controls |
|---|---|---|
| `LEAGUE_COMPETITIONS` | `["PL","PD","BL1","FL1","SA","CL","ELC"]` | Add a code here to track a new league |
| `TOURNAMENT_YEARS["WC"]` | `2026` | World Cup output folder year |
| `TOURNAMENT_YEARS["EC"]` | `2024` | Euro output folder year |
| `_NEW_SEASON_FROM_MONTH` | `6` (June) | Month at which "current season" flips to next year |
| `SCORERS_LIMIT` | `10` | How many top scorers to store per competition |

