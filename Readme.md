# GOAL4U Sports API

> **Zero-rate-limit caching proxy for football data.**
> A self-hosted FastAPI backend that decouples your frontend entirely from third-party API rate limits by using GitHub Actions as an automated data pipeline.

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [How It Works — The Full Picture](#how-it-works--the-full-picture)
- [Repository Structure](#repository-structure)
- [Data File Layout](#data-file-layout)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Local Setup](#local-setup)
  - [Running the API Server](#running-the-api-server)
  - [Running Workers Manually](#running-workers-manually)
- [GitHub Actions Setup](#github-actions-setup)
  - [Required Secret](#required-secret)
  - [Workflow Schedule](#workflow-schedule)
  - [How the Commit Step Works](#how-the-commit-step-works)
- [API Reference](#api-reference)
  - [Base URL & Versioning](#base-url--versioning)
  - [Standard Response Envelopes](#standard-response-envelopes)
  - [Status Codes & Error Format](#status-codes--error-format)
  - [Competitions](#competitions)
  - [Matches — Competition-Scoped](#matches--competition-scoped)
  - [Matches — Global Feed](#matches--global-feed)
  - [Teams](#teams)
  - [Health Check](#health-check)
  - [Match Status Enum](#match-status-enum)
- [Data Schemas](#data-schemas)
  - [Competition Object](#competition-object)
  - [Match Object](#match-object)
  - [Team Object](#team-object)
  - [Standing Row Object](#standing-row-object)
- [Worker Architecture](#worker-architecture)
  - [fetch\_matches.py](#fetch_matchespy)
  - [fetch\_teams.py](#fetch_teamspy)
  - [fetch\_competitions.py](#fetch_competitionspy)
  - [workers/utils.py — Shared Utilities](#workersutilspy--shared-utilities)
- [The Three Ingestion Rules](#the-three-ingestion-rules)
- [In-Memory Store — How FastAPI Serves Data](#in-memory-store--how-fastapi-serves-data)
- [Adding a New Competition](#adding-a-new-competition)
- [Configuration Reference](#configuration-reference)
- [Tracked Competitions](#tracked-competitions)

---

## Why This Exists

Football data APIs like [football-data.org](https://www.football-data.org) enforce strict per-minute and per-day rate limits. On their free tier, you get **10 requests per minute**. The moment your frontend starts polling for live scores during a match window — or you get any meaningful number of simultaneous users — you hit `429 Too Many Requests` errors that surface directly to end-users.

On top of that, calling a third-party API directly from frontend code means your **API key is exposed in the browser**. Anyone can open DevTools, grab it, and exhaust your quota.

The standard solution is a backend proxy, but a traditional proxy still bottlenecks every frontend request through a live upstream call. The approach here goes further: **the third-party API is never in the request path at all.**

```
❌  Frontend → football-data.org    (rate-limited, key exposed)
❌  Frontend → Backend → football-data.org   (still rate-limited under load)
✅  GitHub Actions → football-data.org → JSON files → FastAPI → Frontend
```

The upstream API is called exactly once per scheduled interval, by a single GitHub Actions cron job, regardless of how many users are hitting your frontend. The FastAPI server reads from files that are already on disk — every response is served from memory in under 50ms with zero external calls.

---

## How It Works — The Full Picture

The system has four layers. Data only ever flows in one direction: **external API → workers → JSON files → FastAPI → frontend**. No layer reverses this flow.

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — SOURCE                                                    │
│  football-data.org v4 API  (rate-limited, authenticated)            │
└────────────────────────────┬────────────────────────────────────────┘
                             │  HTTP GET (once per schedule interval)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 2 — INGESTION (GitHub Actions cron jobs)                     │
│                                                                      │
│  fetch_matches.py      → runs every 15 min                          │
│  fetch_competitions.py → runs hourly (standings) / weekly (meta)    │
│  fetch_teams.py        → runs daily at 06:00 UTC                    │
│                                                                      │
│  Each worker: fetches → validates → flattens → writes JSON          │
└────────────────────────────┬────────────────────────────────────────┘
                             │  git commit (only if data changed)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — STORAGE (repo JSON files, organized by competition code) │
│                                                                      │
│  data/competitions.json       ← all competition metadata            │
│  data/matches/{CODE}.json     ← per-league match feed               │
│  data/teams/{CODE}.json       ← per-league team rosters             │
│  data/standings/{CODE}.json   ← per-league standing tables          │
└────────────────────────────┬────────────────────────────────────────┘
                             │  loaded into memory at startup (once)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 4 — SERVER (FastAPI, Python)                                 │
│                                                                      │
│  All data held in-memory after startup                              │
│  Query filtering done in Python — zero disk reads per request       │
│  Response time target: < 50ms                                       │
└────────────────────────────┬────────────────────────────────────────┘
                             │  JSON over HTTP
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 5 — CONSUMER (your frontend)                                 │
│                                                                      │
│  Unlimited concurrent requests                                      │
│  No API key required                                                │
│  No rate limits                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### The Startup Load

When the FastAPI server starts, `api/main.py` calls `load_all_data()` once. This function walks the `data/` directory tree, reads every JSON file, and builds four in-memory collections:

| Store Key | Type | Populated From |
|---|---|---|
| `store["competitions"]` | `list` | `data/competitions.json` |
| `store["matches"]` | `list` | All `data/matches/{CODE}.json` files, merged into one flat list |
| `store["teams"]` | `dict` keyed by `str(team_id)` | All `data/teams/{CODE}.json` files, each team indexed by its ID |
| `store["standings"]` | `dict` keyed by `competition_code` | All `data/standings/{CODE}.json` files |

After startup, the server never touches the filesystem again until it restarts. Every router reads directly from `store`. This is why the `< 50ms` response time target is achievable regardless of query complexity.

### The GitHub Actions Sync

Each GitHub Actions workflow:

1. Checks out the repository
2. Installs dependencies
3. Runs the relevant Python worker with `FOOTBALL_DATA_API_KEY` injected from secrets
4. Checks whether the data files actually changed (`git diff`)
5. **Only commits if something changed** — prevents thousands of empty commits cluttering your history

If the upstream API fails (network error, 429, 5xx), the worker logs the error, writes nothing, and GitHub Actions exits with a non-zero status. The existing files on disk are untouched. The next run at the next schedule interval picks up where it left off.

---

## Repository Structure

```
GOAL4U-api/
│
├── data/                          # Static JSON files — the "database"
│   ├── competitions.json          # All tracked competition metadata
│   ├── matches/
│   │   ├── PL.json                # Premier League matches
│   │   ├── PD.json                # La Liga matches
│   │   ├── BL1.json               # Bundesliga matches
│   │   └── ...                    # One file per tracked competition
│   ├── teams/
│   │   ├── PL.json                # All Premier League clubs (array)
│   │   ├── PD.json                # All La Liga clubs (array)
│   │   └── ...                    # One file per tracked competition
│   └── standings/
│       ├── PL.json                # Premier League table (TOTAL/HOME/AWAY)
│       ├── PD.json                # La Liga table
│       └── ...                    # One file per tracked competition
│
├── workers/                       # GitHub Actions data fetchers
│   ├── utils.py                   # Shared: HTTP client, safe_write, normalizers
│   ├── fetch_matches.py           # Fetches + writes data/matches/{CODE}.json
│   ├── fetch_competitions.py      # Fetches + writes competitions.json + standings
│   └── fetch_teams.py             # Fetches + writes data/teams/{CODE}.json
│
├── api/                           # FastAPI application
│   ├── main.py                    # App entry point + startup data loader
│   ├── store.py                   # In-memory data store (shared singleton)
│   ├── utils.py                   # Shared router helpers (errors, list wrapper)
│   └── routers/
│       ├── competitions.py        # /api/v1/competitions endpoints
│       ├── matches.py             # /api/v1/matches endpoints
│       └── teams.py               # /api/v1/teams endpoints
│
├── .github/
│   └── workflows/
│       ├── sync_matches.yml       # Cron: every 15 minutes
│       ├── sync_standings.yml     # Cron: every hour
│       ├── sync_teams.yml         # Cron: daily 06:00 UTC
│       └── sync_competitions.yml  # Cron: weekly Monday 00:00 UTC
│
├── config.py                      # Single source of truth: codes, paths, strip lists
├── requirements.txt
└── README.md
```

---

## Data File Layout

Every JSON file written by the workers follows the same envelope structure:

```json
{
  "_meta": {
    "last_synced": "2026-06-10T14:30:00+00:00",
    "source": "football-data.org v4"
  },
  "data": [ ... ]
}
```

The `_meta` block is used by the `/health` endpoint to report last-sync times. The `data` key contains the actual payload — a list for matches and teams files, a dict for standings and competitions.

### Why per-competition files?

Storing data by competition code (e.g. `data/matches/PL.json`) rather than in a single monolithic file or by team/match ID provides several practical benefits:

- **Targeted syncing** — the matches workflow only writes the file for each competition it successfully fetches. A failure for CL doesn't prevent PL from updating.
- **Conditional Fallback isolation** — if the API returns a bad response for one competition, only that competition's file is preserved unchanged. Other files update normally.
- **Git diff readability** — `git log --stat` shows exactly which league changed in each commit.
- **Faster server startup** — files can be loaded in parallel if needed; the server doesn't have to parse one massive JSON blob.
- **Debuggability** — you can open `data/matches/PL.json` in any editor and inspect what the frontend is actually being served for that league.

---

## Getting Started

### Prerequisites

- Python 3.11+
- A free API key from [football-data.org](https://www.football-data.org/client/register)
- Git

### Local Setup

```bash
# Clone the repo
git clone https://github.com/your-username/goal4u-api.git
cd goal4u-api

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows PowerShell

# Install dependencies
pip install -r requirements.txt

# Set your API key
export FOOTBALL_DATA_API_KEY=your_key_here
# On Windows: $env:FOOTBALL_DATA_API_KEY = "your_key_here"
```

### Running the API Server

The server requires data files to exist before startup. Either run the workers first (see below) or use any pre-populated `data/` directory.

```bash
# Start the server from the repo root
uvicorn api.main:app --reload

# Server starts at http://localhost:8000
# Interactive docs at http://localhost:8000/docs
# ReDoc at http://localhost:8000/redoc
```

On startup you will see log lines like:

```
INFO  Loaded 8 competitions
INFO  Loaded 2847 matches from 8 competition files
INFO  Loaded 160 teams
INFO  Loaded standings for 5 competitions
```

If any data file is missing, the server still starts — it just returns empty results for those endpoints and logs a warning. This means you can run the server before the first sync completes.

### Running Workers Manually

Run any worker from the **repo root** (not from inside the `workers/` directory — they use `sys.path.insert(0, ".")` to find `config.py`):

```bash
# Fetch all matches → writes data/matches/{CODE}.json for each competition
python workers/fetch_matches.py

# Fetch all team rosters → writes data/teams/{CODE}.json for each competition
python workers/fetch_teams.py

# Fetch competition metadata only
python workers/fetch_competitions.py --mode competitions

# Fetch standings only
python workers/fetch_competitions.py --mode standings

# Fetch both (default)
python workers/fetch_competitions.py --mode all
```

Expected log output for `fetch_matches.py`:

```
2026-06-10T14:30:00Z [INFO] fetch_matches: === fetch_matches started at 2026-06-10T14:30:00Z ===
2026-06-10T14:30:01Z [INFO] fetch_matches: Fetching matches for PL ...
2026-06-10T14:30:01Z [INFO] fetch_matches:   380 matches returned for PL
2026-06-10T14:30:02Z [INFO] workers.utils: Wrote data/matches/PL.json (148332 bytes)
2026-06-10T14:30:02Z [INFO] fetch_matches: Fetching matches for PD ...
...
2026-06-10T14:30:10Z [INFO] fetch_matches: === fetch_matches complete: 8/8 competitions written, 2847 matches total ===
```

---

## GitHub Actions Setup

### Required Secret

Go to your repository → **Settings → Secrets and variables → Actions → New repository secret**.

| Secret Name | Value |
|---|---|
| `FOOTBALL_DATA_API_KEY` | Your football-data.org API token |

This is the **only** manual setup step required. The `GITHUB_TOKEN` secret used to commit data files back to the repo is automatically provided by GitHub Actions — you don't need to create it.

> **Security note:** The API key never appears in any committed file, log output, or workflow run summary. It is only ever injected as an environment variable for the duration of the worker process.

### Workflow Schedule

| Workflow File | Cron Expression | Frequency | What It Syncs |
|---|---|---|---|
| `sync_matches.yml` | `*/15 * * * *` | Every 15 minutes | `data/matches/{CODE}.json` for all competitions |
| `sync_standings.yml` | `0 * * * *` | Every hour | `data/standings/{CODE}.json` for all competitions |
| `sync_teams.yml` | `0 6 * * *` | Daily at 06:00 UTC | `data/teams/{CODE}.json` for all competitions |
| `sync_competitions.yml` | `0 0 * * 1` | Weekly, Monday 00:00 UTC | `data/competitions.json` |

**Why these intervals?**

- **Matches every 15 min** — live scores update continuously during a match. 15 minutes is a safe polling interval that stays within the free tier's rate limits across all tracked competitions.
- **Standings every hour** — the league table only changes once a match finishes. Hourly is more than sufficient.
- **Teams daily** — squad changes (transfers, loans, injuries) happen slowly. A daily sync at 06:00 UTC catches overnight transfer news before the first matches of the day.
- **Competition metadata weekly** — season dates, competition names, and area codes almost never change mid-season.

All workflows can also be triggered manually from the GitHub Actions UI using the `workflow_dispatch` trigger — useful for testing or forcing a refresh after a code change.

### How the Commit Step Works

Each workflow uses `git diff` to detect whether the data actually changed before committing:

```yaml
# From sync_matches.yml
if git diff --quiet data/matches/; then
  echo "No changes — skipping commit"
else
  TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%MZ")
  git add data/matches/
  git commit -m "chore(sync): update matches ${TIMESTAMP}"
  git push
fi
```

This means:
- Runs where the upstream API returned identical data (nothing changed since last sync) produce **zero commits** — your git log stays clean.
- Each commit message is timestamped: `chore(sync): update matches 2026-06-10T14:30Z`
- `git log --stat` on a match day shows exactly which competition files changed in each run.

---

## API Reference

### Base URL & Versioning

All endpoints are prefixed with `/api/v1/`. The version prefix is hardcoded in `api/main.py` via `app.include_router(..., prefix="/api/v1")` and will only change if a breaking schema change requires a v2.

```
http://localhost:8000/api/v1/     (local)
https://your-deployment.com/api/v1/   (production)
```

Interactive API documentation (Swagger UI) is available at `/docs`. ReDoc is at `/redoc`.

### Standard Response Envelopes

**List responses** always use this wrapper — never a bare array:

```json
{
  "count": 380,
  "filters": {
    "status": "FINISHED",
    "matchday": 12
  },
  "results": [ ... ]
}
```

- `count` — total number of items in `results` after all filters are applied
- `filters` — echo of query parameters that were active in this request (omitted keys were not provided)
- `results` — the actual array of objects

**Single-object responses** return the object directly without a wrapper.

### Status Codes & Error Format

| HTTP Status | When |
|---|---|
| `200 OK` | Success |
| `400 Bad Request` | Invalid query parameter value (unknown status, bad date format, conflicting params) |
| `404 Not Found` | Unknown competition code, team ID, or match ID |

All errors follow this format:

```json
{
  "error": "Competition with code 'XYZ' not found. Use GET /api/v1/competitions to see all available codes.",
  "status": 404
}
```

---

### Competitions

#### `GET /api/v1/competitions`

Returns all tracked competitions as a list.

**Response:**
```json
{
  "count": 8,
  "filters": {},
  "results": [
    {
      "id": 2021,
      "name": "Premier League",
      "code": "PL",
      "type": "LEAGUE",
      "area": {
        "id": 2072,
        "name": "England",
        "code": "ENG"
      },
      "currentSeason": {
        "id": 733,
        "startDate": "2025-08-14",
        "endDate": "2026-05-23",
        "currentMatchday": 38,
        "winner": null
      }
    }
  ]
}
```

---

#### `GET /api/v1/competitions/{code}`

Returns a single competition by its code. Codes are case-insensitive.

**Path parameter:**

| Parameter | Type | Example |
|---|---|---|
| `code` | string | `PL`, `PD`, `BL1`, `CL` |

**Response:** Competition object (same shape as above, unwrapped).

**Error:** `404` if the code is not in the tracked list.

---

#### `GET /api/v1/competitions/{code}/standings`

Returns the league table for a competition. Returns `404` for CUP-type competitions (Champions League group stage knockout rounds, World Cup) which use a bracket format instead of a points table.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `type` | `TOTAL` \| `HOME` \| `AWAY` | none | Filter to one standings type. Omit to return all three. |

**Response:**
```json
{
  "competition_code": "PL",
  "season": {
    "id": 733,
    "startDate": "2025-08-14",
    "endDate": "2026-05-23",
    "currentMatchday": 38
  },
  "filters": { "type": "TOTAL" },
  "standings": [
    {
      "stage": "REGULAR_SEASON",
      "type": "TOTAL",
      "group": null,
      "table": [
        {
          "position": 1,
          "team": {
            "id": 65,
            "name": "Manchester City FC",
            "shortName": "Man City",
            "tla": "MCI"
          },
          "playedGames": 38,
          "form": "WWWDW",
          "won": 28,
          "draw": 5,
          "lost": 5,
          "points": 89,
          "goalsFor": 96,
          "goalsAgainst": 41,
          "goalDifference": 55
        }
      ]
    }
  ]
}
```

---

#### `GET /api/v1/competitions/{code}/scorers`

Returns top goal scorers for a competition, derived from the goal events stored in the match data. Only counts goals from `FINISHED` matches. Own goals are excluded from a player's tally but the match's goal event is still recorded.

**Query parameters:**

| Parameter | Type | Default | Constraints |
|---|---|---|---|
| `limit` | integer | `10` | min `1`, max `50` |

**Response:**
```json
{
  "competition_code": "PL",
  "filters": { "limit": 10 },
  "count": 10,
  "scorers": [
    {
      "player": { "id": 44826, "name": "Erling Haaland" },
      "team": { "id": 65, "name": "Manchester City FC", "tla": "MCI" },
      "goals": 27,
      "assists": 5,
      "penalties": 4
    }
  ]
}
```

---

#### `GET /api/v1/competitions/{code}/matches`

Returns all matches for a specific competition, with optional filtering.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `matchday` | integer (≥ 1) | Filter to a specific matchday |
| `status` | string | Filter by status (see [Match Status Enum](#match-status-enum)). `LIVE` returns `IN_PLAY` + `PAUSED` combined. |

**Examples:**
```
GET /api/v1/competitions/PL/matches?matchday=1
GET /api/v1/competitions/PL/matches?status=LIVE
GET /api/v1/competitions/PL/matches?matchday=12&status=FINISHED
```

**Response:** Standard list envelope containing [Match objects](#match-object).

---

#### `GET /api/v1/competitions/{code}/teams`

Returns all clubs participating in a competition. Returns full team profiles (including squad) if team data has been synced; falls back to minimal stubs (id, name, tla) derived from match data if team files are not yet available.

**Response:** Standard list envelope containing [Team objects](#team-object).

---

### Matches — Global Feed

#### `GET /api/v1/matches`

Returns matches across **all** tracked competitions, with filtering. All filter parameters are optional and combinable, with one exception: `date` cannot be combined with `dateFrom`/`dateTo`.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `date` | `YYYY-MM-DD` | All matches on an exact date (UTC) |
| `status` | string | Filter by status. `LIVE` returns `IN_PLAY` + `PAUSED` combined. |
| `dateFrom` | `YYYY-MM-DD` | Start of a date range (inclusive) |
| `dateTo` | `YYYY-MM-DD` | End of a date range (exclusive) |

**Examples:**
```
# All matches today
GET /api/v1/matches?date=2026-06-10

# All currently live matches across all leagues
GET /api/v1/matches?status=LIVE

# All matches in a date window
GET /api/v1/matches?dateFrom=2026-06-01&dateTo=2026-06-08

# All finished matches today
GET /api/v1/matches?date=2026-06-10&status=FINISHED
```

**Error cases:**
- `400` if `date` is used together with `dateFrom` or `dateTo`
- `400` if any date string is not in `YYYY-MM-DD` format
- `400` if `status` is not a recognised value

---

#### `GET /api/v1/matches/{match_id}`

Returns the full detail object for a single match.

**Path parameter:**

| Parameter | Type | Description |
|---|---|---|
| `match_id` | integer | The match's numeric ID from football-data.org |

**Error:** `404` if no match with that ID exists in the current data.

---

### Teams

#### `GET /api/v1/teams/{team_id}`

Returns a team profile. By default the `squad` array is omitted to keep the response lightweight. Pass `?include=squad` to add the full player list.

**Path parameter:**

| Parameter | Type | Description |
|---|---|---|
| `team_id` | integer | The team's numeric ID from football-data.org |

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `include` | string | Pass `squad` to include the full squad array |

**Examples:**
```
GET /api/v1/teams/65              # Man City — no squad
GET /api/v1/teams/65?include=squad  # Man City — with full squad
```

**Response (without squad):**
```json
{
  "id": 65,
  "name": "Manchester City FC",
  "shortName": "Man City",
  "tla": "MCI",
  "venue": "Etihad Stadium",
  "area_code": "ENG",
  "coach": {
    "id": 33636,
    "name": "Pep Guardiola",
    "nationality": "Spain",
    "role": "HEAD_COACH"
  }
}
```

**Response (with `?include=squad`):** Same as above with an additional `squad` array:

```json
{
  "squad": [
    {
      "id": 44826,
      "name": "Erling Haaland",
      "position": "Attacker",
      "shirtNumber": 9,
      "nationality": "Norway"
    }
  ]
}
```

**Error:** `404` if no team with that ID exists in the synced data.

---

### Health Check

#### `GET /health`

Returns the server's operational status and last-sync timestamps for all data resources. Useful for monitoring whether the GitHub Actions syncs are running correctly.

**Response:**
```json
{
  "status": "ok",
  "counts": {
    "competitions": 8,
    "matches": 2847,
    "teams": 160,
    "standings": 5
  },
  "last_synced": {
    "competitions": "2026-06-09T00:00:00+00:00",
    "matches": {
      "PL":  "2026-06-10T14:30:00+00:00",
      "PD":  "2026-06-10T14:30:01+00:00",
      "BL1": "2026-06-10T14:30:02+00:00"
    }
  }
}
```

`standings` count will be less than the total number of tracked competitions because CUP-format competitions (WC, EC) do not have league tables.

---

### Match Status Enum

The `status` field on every match object will be one of these values:

| Status | Meaning | Notes |
|---|---|---|
| `SCHEDULED` | Fixture date confirmed, kick-off time not yet set | Common for fixtures announced far in advance |
| `TIMED` | Exact date and time confirmed | Standard pre-match state |
| `IN_PLAY` | Match is currently live | Score updates every sync cycle |
| `PAUSED` | Half-time break | Still counts as "live" for the `LIVE` filter |
| `FINISHED` | Full-time result confirmed | Score is final |
| `SUSPENDED` | Match stopped mid-game | May resume or be replayed |
| `POSTPONED` | Rescheduled to a future date | `utcDate` will be updated once a new date is confirmed |
| `CANCELLED` | Match will not be played | |
| `AWARDED` | Result given without playing | Forfeit, walkover, or disqualification |
| **`LIVE`** | **Pseudo-filter only** | **Not a real status value — expands to `IN_PLAY` + `PAUSED` in API queries** |

---

## Data Schemas

### Competition Object

```json
{
  "id": 2021,                        // football-data.org internal ID
  "name": "Premier League",
  "code": "PL",                      // 2–4 letter unique key used throughout this API
  "type": "LEAGUE",                  // LEAGUE | CUP | LEAGUE_CUP
  "area": {
    "id": 2072,
    "name": "England",
    "code": "ENG"
  },
  "currentSeason": {
    "id": 733,
    "startDate": "2025-08-14",       // ISO 8601 date (no time component)
    "endDate": "2026-05-23",
    "currentMatchday": 38,
    "winner": null                   // null during season; team object after final
  }
}
```

### Match Object

```json
{
  "id": 419238,
  "competition_code": "PL",
  "utcDate": "2026-04-12T14:00:00Z", // ISO 8601 UTC
  "status": "FINISHED",
  "matchday": 32,
  "stage": "REGULAR_SEASON",
  "group": null,                     // non-null for group-stage cup matches
  "minute": null,                    // current minute if IN_PLAY, else null
  "injuryTime": null,                // injury time minutes if applicable
  "attendance": 53274,
  "venue": "Etihad Stadium",
  "homeTeam": {
    "id": 65,
    "name": "Manchester City FC",
    "shortName": "Man City",
    "tla": "MCI"
  },
  "awayTeam": {
    "id": 57,
    "name": "Arsenal FC",
    "shortName": "Arsenal",
    "tla": "ARS"
  },
  "score": {
    "winner": "HOME_TEAM",           // HOME_TEAM | AWAY_TEAM | DRAW | null
    "duration": "REGULAR",          // REGULAR | EXTRA_TIME | PENALTIES
    "fullTime":    { "home": 3, "away": 1 },
    "halfTime":    { "home": 1, "away": 0 },
    "regularTime": { "home": 3, "away": 1 },
    "extraTime":   { "home": null, "away": null },
    "penalties":   { "home": null, "away": null }
  },
  "statistics": {
    "home": {
      "shots": 16, "shots_on_goal": 8, "shots_off_goal": 5,
      "possession": 61, "fouls": 9, "corner_kicks": 7,
      "yellow_cards": 1, "yellow_red_cards": 0, "red_cards": 0,
      "saves": 4, "offsides": 2
    },
    "away": {
      "shots": 9, "shots_on_goal": 4, "shots_off_goal": 3,
      "possession": 39, "fouls": 14, "corner_kicks": 3,
      "yellow_cards": 2, "yellow_red_cards": 0, "red_cards": 0,
      "saves": 5, "offsides": 1
    }
  },
  "goals": [
    {
      "minute": 23,
      "injuryTime": null,
      "type": "REGULAR",             // REGULAR | PENALTY | OWN_GOAL
      "team": { "id": 65, "name": "Manchester City FC", "tla": "MCI" },
      "scorer": { "id": 44826, "name": "Erling Haaland" },
      "assist": { "id": 11671, "name": "Kevin De Bruyne" },
      "score": { "home": 1, "away": 0 }  // score at the moment of this goal
    }
  ],
  "bookings": [
    {
      "minute": 56,
      "team": { "id": 57, "name": "Arsenal FC", "tla": "ARS" },
      "player": { "id": 8812, "name": "Thomas Partey" },
      "card": "YELLOW"               // YELLOW | RED | YELLOW_RED
    }
  ],
  "substitutions": [
    {
      "minute": 68,
      "team": { "id": 65, "name": "Manchester City FC", "tla": "MCI" },
      "playerOut": { "id": 44826, "name": "Erling Haaland" },
      "playerIn":  { "id": 1234,  "name": "Julian Alvarez" }
    }
  ]
}
```

All fields are always present. Fields that are not yet applicable (e.g. `halfTime` score before half-time, `extraTime` in a regular-time match) are explicitly `null` rather than omitted. This is the **Null Normalization** rule — see [The Three Ingestion Rules](#the-three-ingestion-rules).

### Team Object

```json
{
  "id": 65,
  "name": "Manchester City FC",
  "shortName": "Man City",
  "tla": "MCI",
  "venue": "Etihad Stadium",
  "area_code": "ENG",
  "coach": {
    "id": 33636,
    "name": "Pep Guardiola",
    "nationality": "Spain",
    "role": "HEAD_COACH"
  },
  "squad": [
    {
      "id": 44826,
      "name": "Erling Haaland",
      "position": "Attacker",
      "shirtNumber": 9,
      "nationality": "Norway"
    }
  ]
}
```

### Standing Row Object

```json
{
  "position": 1,
  "team": {
    "id": 65,
    "name": "Manchester City FC",
    "shortName": "Man City",
    "tla": "MCI"
  },
  "playedGames": 38,
  "form": "WWWDW",        // last 5 matches: W=win, D=draw, L=loss
  "won": 28,
  "draw": 5,
  "lost": 5,
  "points": 89,
  "goalsFor": 96,
  "goalsAgainst": 41,
  "goalDifference": 55
}
```

---

## Worker Architecture

### `fetch_matches.py`

The most time-sensitive worker. Runs every 15 minutes.

**Pipeline for each competition code:**

```
1. GET /competitions/{code}/matches
   └── Returns raw payload with full match objects for the current season

2. For each match:
   a. strip_fields() — remove lastUpdated, _links, referees, odds
   b. flatten_team_ref() — reduce homeTeam/awayTeam to {id, name, shortName, tla}
   c. flatten_score() — normalize all score sub-nodes, null-fill missing ones
   d. flatten_statistics() — convert upstream list-of-stats to {home: {...}, away: {...}}
      • Maps upstream space-separated names ("Ball Possession") to snake_case ("possession")
      • Strips "%" from possession values, converts to float
   e. flatten_goals/bookings/substitutions() — normalize event arrays
   f. Null Normalization — ensure all MATCH_SCHEMA_KEYS are present

3. Sort matches chronologically by utcDate

4. safe_write("data/matches/{CODE}.json", matches)
   └── Conditional Fallback: if fetch returned None, file is not touched
```

**Failure behavior:**
- If one competition's fetch fails → that `{CODE}.json` is untouched; other competitions continue normally
- If all competitions fail → `sys.exit(1)`, no files written

### `fetch_teams.py`

Runs daily. Makes **two API calls per team**: first to get the list of teams in a competition, then one per team to fetch its full profile including squad.

**Pipeline for each competition code:**

```
1. GET /competitions/{code}/teams
   └── Returns a list of team stubs for all clubs in the competition

2. For each team in the list:
   a. GET /teams/{id}?squad=true
      └── Returns full team profile including coach and squad array
   b. strip_fields() — remove crest, website, founded, clubColors, lastUpdated,
                        _links, address, phone, email, activeCompetitions
   c. flatten_coach() — normalize to {id, name, nationality, role}
   d. flatten_player() for each squad member — keep id, name, position,
                                                shirtNumber, nationality
   e. Null Normalization on TEAM_SCHEMA_KEYS
   f. Append to competition's team list

3. safe_write("data/teams/{CODE}.json", [team1, team2, ...])
   └── Full list for the competition written atomically as one file
```

**Key design choices:**
- `seen_ids` set prevents duplicate fetches if a team appears multiple times in the upstream response
- A failed detail fetch for one team is skipped with a warning — the rest of the competition's teams still write
- If the initial `/competitions/{code}/teams` call fails, the entire `{CODE}.json` is preserved unchanged (Conditional Fallback). The server then loads the previous sync's data from that file on the next restart.

### `fetch_competitions.py`

Runs in two modes on different schedules:

**`--mode competitions` (weekly):**
```
For each tracked competition code:
  GET /competitions/{code}
  → strip, flatten, null-normalize
  → append to list

safe_write("data/competitions.json", [comp1, comp2, ...])
```

**`--mode standings` (hourly):**
```
For each tracked competition code:
  GET /competitions/{code}/standings
  → if None returned → skip (Conditional Fallback, file preserved)
  → if standings list is empty → skip (CUP format, no table)
  → flatten each standings type (TOTAL/HOME/AWAY) and each table row
  → safe_write("data/standings/{CODE}.json", standings_object)
```

CUP-type competitions will never produce a standings file, which is why `standings` count in the `/health` response is lower than the total competition count.

### `workers/utils.py` — Shared Utilities

All three workers import from this module. It contains four main components:

**`fetch(endpoint, params, retries=3)`**

The single HTTP client used across all workers. Handles:
- Attaching `X-Auth-Token` from the environment variable
- `429` rate-limit responses: reads `X-RequestCounter-Reset` header and sleeps for that duration before retrying
- `5xx` server errors: exponential back-off (2s, 4s, 8s)
- `4xx` non-429 errors: logs and returns `None` immediately (don't retry a 404)
- Network errors (`requests.RequestException`): retry with back-off up to `retries` attempts
- Returns `None` on any failure — callers treat `None` as a signal to trigger Conditional Fallback

**`safe_write(path, data)`**

Atomic file writer with built-in Conditional Fallback:
- If `data is None` → logs error, returns `False`, **does not touch the file**
- Wraps data in the `{_meta, data}` envelope
- Writes to `{path}.tmp` first, then `os.replace()` renames atomically — a crash mid-write never produces a corrupt file
- Uses `json.dump(..., indent=2)` for human-readable, diffable output

**`normalize_nulls(obj, schema_keys)`**

Ensures every key in a schema is present in the output dict. Missing keys are set to `None` (explicit JSON `null`) rather than being omitted. This prevents `KeyError` exceptions in the frontend when a field is not yet available (e.g. `halfTime` before the half ends).

**`strip_fields(obj, fields)`**

Removes non-essential fields in-place. Reduces file size and keeps payloads clean. Fields stripped include: emblem/crest URLs, `lastUpdated` timestamps, `_links` API metadata, ticket purchase links, internal admin fields. The strip lists are defined in `config.py`.

---

## The Three Ingestion Rules

These rules govern every worker. They are non-negotiable — violating any of them causes either frontend crashes (missing fields) or data loss (overwriting good data with bad).

### Rule 1 — Conditional Fallback

> If the external API returns any non-200 response, the worker must exit without writing any file.

The existing file on disk is treated as the last known good state. A failed sync preserves it exactly. The next successful sync will overwrite it with fresh data.

This means a 30-minute API outage appears to your frontend users as slightly stale data, not as a broken page with empty arrays or null responses.

### Rule 2 — Null Normalization

> All schema fields must always be present in the output JSON. Missing values are written as `null`, never omitted.

```json
// CORRECT — field present, value is null
"halfTime": { "home": null, "away": null }

// WRONG — field missing entirely
// "halfTime" key does not exist
```

Frontend code that does `match.score.halfTime.home` must never throw a `TypeError: Cannot read property 'home' of undefined`. If the API didn't return a value, we write `null` so the key always exists.

### Rule 3 — Storage Optimization (Strip on Ingest)

> Non-essential fields from the upstream payload are removed during the flattening step, before writing.

Stripped fields include emblem/crest URLs, ticket purchase links, `lastUpdated` API timestamps, `_links` navigation metadata, historical seasons arrays, and admin/partner fields. The full strip lists are in `config.py`.

This keeps each JSON file as small as possible, which reduces startup load time, lowers memory usage in the FastAPI process, and keeps `git diff` output readable.

---

## In-Memory Store — How FastAPI Serves Data

`api/store.py` defines a single module-level dict:

```python
store: dict[str, Any] = {
    "competitions": [],   # flat list of all competition objects
    "matches":      [],   # flat list of ALL matches across all competitions
    "teams":        {},   # dict: str(team_id) → team object
    "standings":    {},   # dict: competition_code → standings object
    "_meta":        {},   # last_synced timestamps
}
```

`api/main.py` populates this dict once during the FastAPI lifespan startup hook. After that, every router in `api/routers/` imports `store` directly and filters in Python.

Keeping `store` in its own module (`api/store.py`) rather than in `api/main.py` is intentional — it breaks the circular import that would otherwise occur when `main.py` imports routers and routers try to import from `main.py`.

**Why a flat list for matches?**

All `data/matches/{CODE}.json` files are merged into a single `store["matches"]` list on load. This means every filter operation (`?status=LIVE`, `?date=2026-06-10`, etc.) scans the full list. For the volumes involved (a few thousand matches per season across 8 competitions), this is faster than a database query with indexes because there is no serialization overhead, no network round-trip, and the data is already in CPU cache.

**Why a dict keyed by `str(team_id)` for teams?**

Team lookups are almost always by ID (e.g. `GET /api/v1/teams/65`). A dict gives O(1) lookup instead of a linear scan. The key is `str(team_id)` rather than `int` because JSON object keys are always strings and using the same type avoids subtle bugs when the ID comes from URL path parameters (which FastAPI parses as `int`) vs JSON data (which Python's `json` module parses object keys as `str`).

---

## Adding a New Competition

`config.py` is the only file you ever need to edit.

```python
# config.py
TRACKED_COMPETITIONS: list[str] = [
    "PL",    # Premier League
    "PD",    # La Liga
    "BL1",   # Bundesliga
    "SA",    # Serie A
    "FL1",   # Ligue 1
    "CL",    # Champions League
    "EC",    # European Championship
    "WC",    # World Cup
    "PPL",   # Primeira Liga (Portugal) ← add this
]
```

On the next workflow run, all four workers will automatically include the new code in their fetch loops. New files will appear at:

- `data/matches/PPL.json`
- `data/teams/PPL.json`
- `data/standings/PPL.json`

The new competition will appear in `GET /api/v1/competitions` immediately after the next weekly sync, and matches will start appearing in `GET /api/v1/competitions/PPL/matches` after the next 15-minute sync.

To find the correct competition code, browse the [football-data.org documentation](https://www.football-data.org/documentation/quickstart) or check the `code` field in the API's `/v4/competitions` response.

---

## Configuration Reference

All constants are defined in `config.py`.

| Constant | Type | Description |
|---|---|---|
| `TRACKED_COMPETITIONS` | `list[str]` | Competition codes to sync. Adding a code here is the only change needed to track a new league. |
| `COMPETITIONS_FILE` | `str` | Path: `data/competitions.json` |
| `MATCHES_DIR` | `str` | Directory: `data/matches` — workers write `{CODE}.json` files here |
| `STANDINGS_DIR` | `str` | Directory: `data/standings` |
| `TEAMS_DIR` | `str` | Directory: `data/teams` |
| `COMPETITION_STRIP_FIELDS` | `list[str]` | Fields stripped from upstream competition payloads: `emblem`, `lastUpdated`, `_links`, `seasons` |
| `MATCH_STRIP_FIELDS` | `list[str]` | Fields stripped from upstream match payloads: `lastUpdated`, `_links`, `referees`, `odds` |
| `TEAM_STRIP_FIELDS` | `list[str]` | Fields stripped from upstream team payloads: `crest`, `website`, `founded`, `clubColors`, `lastUpdated`, `_links`, `address`, `phone`, `email`, `activeCompetitions`, `runningCompetitions` |
| `PERSON_STRIP_FIELDS` | `list[str]` | Fields stripped from person objects (scorers, assisters): `dateOfBirth`, `marketValue`, `contract`, `lastUpdated` |

---

## Tracked Competitions

| Code | Competition | Country/Region |
|---|---|---|
| `PL` | Premier League | England |
| `PD` | La Liga | Spain |
| `BL1` | Bundesliga | Germany |
| `SA` | Serie A | Italy |
| `FL1` | Ligue 1 | France |
| `CL` | UEFA Champions League | Europe |
| `EC` | UEFA European Championship | Europe |
| `WC` | FIFA World Cup | International |

> Coverage depends on your football-data.org subscription tier. Free tier covers PL, PD, BL1, SA, FL1, CL, EC, and WC. Premium tiers add more leagues — add their codes to `TRACKED_COMPETITIONS` in `config.py` and they will be picked up automatically.