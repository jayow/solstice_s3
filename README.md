# solstice_s3 — Solstice public-metrics archive

A twice-daily snapshot of every number Solstice publishes openly, kept as an
append-only log.

**Why this exists.** None of Solstice's public endpoints expose history — they
return the current value only. Any number not written down on the day it was
published is gone. This repo writes them down.

```
data/solstice_totals.jsonl     one JSON object per capture, ~2.4 KB each
```

## The two legs

Season-3 tracking has two halves that meet only at the comparison:

| leg | where it runs | what it produces |
|---|---|---|
| **walker** | locally, needs a ~41 GB SQLite DB + Solana RPC | *our own* independently-computed per-wallet flares |
| **baseline** (this repo) | GitHub Actions, no DB, no RPC, no secrets | *Solstice's* published numbers |

Keeping the baseline here means it never misses a day because a laptop was
asleep, and it can't be broken by a change to the walker pipeline.

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

## Loading it into the dashboard DB

From the main dashboard repo, where the SQLite DB lives:

```bash
SOLSTICE_SEASON=S3 python3 tools/set_solstice_total.py \
    --import-file /path/to/solstice_s3/data/solstice_totals.jsonl
```

Each line carries its own season, so one pass files every season under its own
`flares_snapshots` source (`solstice_dashboard` for S2, `solstice_dashboard_s3`
for S3) — appending S3's ~10B total to the S2 series would otherwise read as a
107B collapse.

## Source

All data comes from public, unauthenticated endpoints on
`https://app.solstice.finance`: `/api/flares/analytics`,
`/api/rewards/global/analytics`, `/api/protocol`, `/api/protocol/tvl`,
`/api/protocol/por`, `/api/flares/quests`, `/api/partners`.

Per-wallet endpoints (`/api/flares/user/*`) are session-gated and are not
touched by this repo.

This is an independent third-party archive and is not affiliated with Solstice
Finance.
