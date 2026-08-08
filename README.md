# solstice_s3 — Solstice Season 3 tracker

Everything Season-3-specific: the published-metrics archive, the S3 dashboard
payloads, and the refresh that produces them.

## Layout

```
.github/workflows/   twice-daily capture of Solstice's public numbers
tools/               the capture script (stdlib only, no deps)
data/                solstice_totals.jsonl — the append-only archive
site/                S3 dashboard payloads (data.json, daily_totals.json, wallets/)
refresh_s3.sh        drives the shared engine to rebuild everything above
```

## Relationship to the engine repo

The walkers and the ~41 GB `solstice.db` live in the main dashboard repo
(`SolsticeAirdropUsers`) and are **shared** with Season 2 — both seasons run the
same walker code against the same database. Only the season-varying inputs
differ, and those are selected by `SOLSTICE_SEASON`.

So this repo holds S3's *outputs and configuration*, not a copy of the engine:

```
engine repo                          this repo
├── src/flares_estimator/            ├── site/            ← written by the engine
│   ├── season_config.py  (S2 + S3)  ├── data/            ← read by the engine
│   └── gt_walkers/       (shared)   └── refresh_s3.sh    ← drives the engine
├── data/solstice.db      (shared)
└── server/build_*.py     (shared)
```

`refresh_s3.sh` runs the engine's builders with `SOLSTICE_SEASON=S3` and
`SOLSTICE_SITE_DIR=<this repo>/site`, so the S3 payloads land here while S2's
stay in the engine repo. Nothing is duplicated and the two seasons cannot
overwrite each other — cache keys, quest codes and snapshot sources are all
namespaced per season.

## The two legs

Season-3 tracking has two halves that meet only at the comparison:

| leg | where it runs | what it produces |
|---|---|---|
| **walker** | locally via `refresh_s3.sh`, needs the engine repo's DB + Solana RPC | *our own* independently-computed per-wallet flares |
| **baseline** | GitHub Actions in this repo — no DB, no RPC, no secrets | *Solstice's* published numbers |

Keeping the baseline leg here means it never misses a day because a laptop was
asleep, and it can't be broken by a change to the walker pipeline.

**Why the archive exists at all:** none of Solstice's public endpoints expose
history — they return the current value only. Any number not written down on the
day it was published is gone.

## What each line holds

| field | detail |
|---|---|
| `season_id`, `season_name` | the season the totals belong to — they reset at each boundary, so a line without this is unimportable |
| `total_flares`, `total_users` | the headline figures |
| `campaign` | SLX allocation base/change, total supply, burned, bought back, SLX value |
| `protocol` | USX + eUSX supply, eUSX backing + peg, TVL, proof-of-reserve health/backing/custodians |
| `quests` | `n` plus `{code: multiplier, isActive, minBalance}` for every quest |
| `partners` | per-partner APY |
| `quests_changed` | `true` on any day the quest catalog differs from the previous capture |
| `errors` | present only when an endpoint failed, so a partial line is never mistaken for a complete one |

### `quests_changed` is the useful one

Season 2 grew from a handful of quests to 24 over its run; Season 3 launched
with 5. The day Solstice adds an S3 partner quest, `quests_changed` flips and
the commit subject says so — which is the cue to wire up a walker for it.

## Cadence

`5 0,12 * * *`. The 00:05 UTC run catches Solstice's 00:00 publish. The flare
total does not move again until the next publish, so the 12:05 run is mainly a
free retry if the first failed — it does add real intraday resolution for the
numbers that move continuously (peg, supply, TVL, APYs).

Captures dedup on a 6-hour window, so both scheduled runs land while an
accidental re-run or a manual dispatch minutes later is a no-op.

## Running it by hand

```bash
python3 tools/capture_solstice_baseline.py data/solstice_totals.jsonl
python3 tools/capture_solstice_baseline.py data/solstice_totals.jsonl --min-interval-hours 0   # ignore the dedup window
```

Standard library only — no dependencies to install. (It will use `certifi`'s CA
bundle if importable, which the python.org macOS builds need; CI does not.)

## Refreshing the S3 dashboard

```bash
SOLSTICE_ENGINE=/path/to/SolsticeAirdropUsers ./refresh_s3.sh
```

(`SOLSTICE_ENGINE` can be omitted if the engine repo is a sibling directory.)
Phases: S3 HOLD TWAB cache → tier walkers → resync → dashboard build →
baseline import. The first run cold-walks every USX/eUSX holder's signature
history and takes hours; later runs hit the cache.

The script forces the python.org 3.13 framework build onto `PATH` and then
verifies `requests`/`base58`/`solders` import. Homebrew's `python3` shadows the
working interpreter and lacks those, and the usual shell guards make total
walker failure look like a clean run — so the check is a hard failure by design.

### Baseline import on its own

```bash
# from the engine repo, where the SQLite DB lives
python3 tools/set_solstice_total.py --import-file /path/to/solstice_s3/data/solstice_totals.jsonl
```

Each line carries its own season, so one pass files every season under its own
`flares_snapshots` source (`solstice_dashboard` for S2, `solstice_dashboard_s3`
for S3) — appending S3's ~10B total to the S2 series would otherwise read as a
107B collapse.

## Status

The S3 quest set is four HOLD tiers plus referral (`S3_HOLD_USX_1MO` 6×,
`S3_HOLD_USX_3MO` 15×, `S3_HOLD_EUSX_1MO` 4×, `S3_HOLD_EUSX_3MO` 10×,
`S3_REFERRAL_BONUS` 0×) — no partner quests yet. `quests_changed` in the archive
is the tripwire for when that changes.

**Open question — the accrual model.** The tiers are described as "rewarded at
completion" and the first 1-month cycle cannot close before ~2026-08-31, yet
Solstice is already accruing ~1.4 B flares/day. The quest catalog reports
`periodSeconds: 86400` and `minBalance: $100` on those tiers, which points at
daily accrual above a floor rather than pay-at-completion. Until that is
calibrated against the archived totals, `site/` reports zero rather than a
guessed number.

## Source

All data comes from public, unauthenticated endpoints on
`https://app.solstice.finance`: `/api/flares/analytics`,
`/api/rewards/global/analytics`, `/api/protocol`, `/api/protocol/tvl`,
`/api/protocol/por`, `/api/flares/quests`, `/api/partners`.

Per-wallet endpoints (`/api/flares/user/*`) are session-gated and are not
touched by this repo.

This is an independent third-party archive and is not affiliated with Solstice
Finance.
