# pops-guide-data

Cloud-based scheduled data refresh for the three Pops' Guide sites:

- [lakewayguide.com](https://lakewayguide.com) — Pops' Guide to Lakeway
- [thehillsguide.com](https://thehillsguide.com) — Pops' Guide to The Hills of Lakeway
- [costabellaguide.com](https://costabellaguide.com) — Pops' Guide to Costa Bella

GitHub Actions workflows run on a cron schedule, fetch source data (CivicWeb, LCRA, MUD websites, etc.), write JSON to `data/`, and commit any changes back to the repo. GitHub Pages serves `data/` as a public read-only API; the three sites' frontends fetch from there.

This replaces the prior macOS scheduled-tasks workflow that required the user's Mac to be awake and the Claude app open.

## Directory layout

```
.github/workflows/   one YAML per cron job
scripts/             Python scripts called from workflows
scripts/lib/         shared helpers (timezone, commit, civicweb playwright)
data/                JSON outputs (published as GitHub Pages)
```

## JSON outputs (consumed by the sites)

| File | Updated by | Purpose |
|---|---|---|
| `data/lakeway-boil.json` | water-daily | Active boil-water notices for districts serving Lakeway |
| `data/hills-boil.json` | water-daily | Active boil-water notices for districts serving The Hills |
| `data/costabella-boil.json` | water-daily | Active boil-water notices for WCID #17 |
| `data/watering-stages.json` | water-daily | Current stage (1/2/3) for each of 6 districts |
| `data/burn-ban.json` | water-daily | Travis County burn-ban status |
| `data/lakeway-meetings.json` | meetings-daily + meetings-watch | Lakeway public meetings (Council, ZAPCO, BOA, Ethics) |
| `data/hills-meetings.json` | meetings-daily + meetings-watch | Hills Village Council meetings |
| `data/lake-travis-level.json` | lake-level-daily | Current Lake Travis elevation + percent-full |
| `data/solid-waste-rates.json` | solid-waste-weekly | Lakeway Solid Waste pricing |
| `data/parks-closures.json` | parks-closures-weekly | LCRA + Travis County park closures |
| `data/link-health.json` | link-validator-weekly | External link health across all 3 sites |
| `data/monthly-status.json` | monthly-sweep | Pharmacy/vet hours, GFiber buildout, HOA cert refresh, etc |

## Workflows

| Workflow | Cron (UTC) | Frequency | Source |
|---|---|---|---|
| `water-daily.yml` | `30 11 * * *` | 6:30 AM CT daily | LMUD, HCM, WCID#17, Crossroads, Travis Co burn-ban |
| `lake-level-daily.yml` | `0 13 * * *` | 8:00 AM CT daily | Water Data for Texas / LCRA hydromet |
| `meetings-daily.yml` | `30 11 * * *` | 6:30 AM CT daily | CivicWeb (Playwright) |
| `meetings-watch.yml` | `0 16,20 * * *` | 11 AM + 3 PM CT daily | CivicWeb per-meeting pages (Playwright) |
| `solid-waste-weekly.yml` | `0 14 * * 1` | Mon 9 AM CT | lakeway-tx.gov |
| `link-validator-weekly.yml` | `0 13 * * 0` | Sun 8 AM CT | All 3 sites + every external link |
| `parks-closures-weekly.yml` | `0 12 * * 3` | Wed 7 AM CT | parks.traviscountytx.gov, lcraparks.com |
| `monthly-sweep.yml` | `0 12 1 * *` | 1st of month, 7 AM CT | Various |

> **DST note:** GitHub Actions cron runs in UTC. The above expressions match CDT (summer). Times drift back 1 hour during CST (winter, Nov–Mar). Each script also stamps the actual CT time it ran, so you can always tell when data was captured.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
TZ=America/Chicago python scripts/water_check.py
TZ=America/Chicago python scripts/lake_level.py
cat data/lake-travis-level.json
```

## Site consumption

After deploying GitHub Pages on this repo, each Pops Guide changes its fetch URLs from local origin (`/active-boil-notices.json`) to:

```js
const DATA_BASE = 'https://USERNAME.github.io/pops-guide-data/data/';
fetch(`${DATA_BASE}lakeway-boil.json`)
```

Set `DATA_BASE` once near the top of each site's JS and switch every existing fetch in one pass.

## Adding a new workflow

1. Add the script to `scripts/`
2. Add a YAML to `.github/workflows/` (copy `water-daily.yml` as a template)
3. Make sure the JSON output is well-formed and the script is idempotent — commits should only happen when data actually changes (handled by `commit_if_changed.py`)

## Operational guarantees

- **Atomic writes.** Scripts write to a temp file then rename, so an in-progress crash never leaves a half-written JSON.
- **No-op friendly.** Workflows only commit when JSON changes. Quiet days produce no commits.
- **Failure visibility.** Failed runs surface in GitHub's Actions tab and (by default) email the repo owner.
- **Idempotent.** Re-running any workflow manually (workflow_dispatch) is always safe.
