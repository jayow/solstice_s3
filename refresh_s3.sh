#!/usr/bin/env bash
# Full Season-3 refresh.
#
# S3 shares the engine repo's walkers and its ~41GB data/solstice.db with S2 —
# only the season-varying inputs differ, and those come from SOLSTICE_SEASON.
# This script runs the engine's code and writes the dashboard payloads back
# into THIS repo's site/ directory.
#
#   SOLSTICE_ENGINE=/path/to/SolsticeAirdropUsers ./refresh_s3.sh
#
# Phases:
#   1. HOLD TWAB cache   — S3 has no DAILY quest, so gt_hold_cache builds the
#                          cache the 1MO/3MO tiers read. Cold on first run
#                          (hours); a cache hit afterwards.
#   2. Tier walkers      — the four S3_HOLD_* quests + referral.
#   3. Resync            — recompute tiers from cache, repairing wallets whose
#                          cache was fixed after the walker ran.
#   4. Dashboard build   — data.json / daily_totals.json / wallets/.
#   5. Baseline import   — load this repo's captured Solstice totals into
#                          flares_snapshots so the inflation chart has its
#                          reference series.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="${SOLSTICE_ENGINE:-}"
if [ -z "$ENGINE" ]; then
  for c in "$HERE/../SolsticeAirdropUsers" "$HOME/Downloads/Claude Projects/SolsticeAirdropUsers"; do
    [ -f "$c/src/flares_estimator/season_config.py" ] && ENGINE="$c" && break
  done
fi
if [ ! -f "${ENGINE:-/nonexistent}/src/flares_estimator/season_config.py" ]; then
  echo "ERROR: engine repo not found. Set SOLSTICE_ENGINE=/path/to/SolsticeAirdropUsers" >&2
  exit 1
fi
echo "engine: $ENGINE"
echo "site:   $HERE/site"

# The python.org framework build is the only interpreter here with requests +
# base58 + solders. Homebrew's python3 shadows it and every walker dies on
# `import requests` — while the shell guards make that look like a clean run.
FRAMEWORK_BIN="/Library/Frameworks/Python.framework/Versions/3.13/bin"
[ -d "$FRAMEWORK_BIN" ] && export PATH="$FRAMEWORK_BIN:$PATH"
python3 -c "import requests, base58, solders" 2>/dev/null || {
  echo "ERROR: python3 ($(command -v python3)) is missing requests/base58/solders." >&2
  echo "       Put the py3.13 framework build first on PATH." >&2
  exit 1
}

export SOLSTICE_SEASON=S3
export SOLSTICE_SITE_DIR="$HERE/site"
mkdir -p "$HERE/site" /tmp/walker_logs
cd "$ENGINE"

echo "[$(date '+%H:%M:%S')] Phase 1: S3 HOLD TWAB cache"
( cd src && python3 -u -m flares_estimator.gt_walkers.gt_hold_cache --mint both ) \
  2>&1 | tee /tmp/walker_logs/s3_hold_cache.log | grep -E "USD-days|wallets with balance|⚠️|candidates|implied|closest" || true

echo "[$(date '+%H:%M:%S')] Phase 2: S3 tier walkers"
for w in gt_hold_usx_1mo gt_hold_eusx_1mo gt_hold_usx_3mo gt_hold_eusx_3mo gt_referral_bonus; do
  ( cd src && python3 -u -m flares_estimator.gt_walkers.$w ) > "/tmp/walker_logs/s3_$w.log" 2>&1 \
    && echo "    ✓ $w" || echo "    ✗ $w FAILED — see /tmp/walker_logs/s3_$w.log"
done

echo "[$(date '+%H:%M:%S')] Phase 3: HOLD cache → wallet_quests resync"
python3 tools/resync_hold_quests.py > /tmp/walker_logs/s3_resync.log 2>&1 \
  || echo "  ⚠️  resync errored — see /tmp/walker_logs/s3_resync.log"

echo "[$(date '+%H:%M:%S')] Phase 4: dashboard build"
python3 server/build_data.py
python3 server/build_daily_totals.py
python3 server/build_wallet_details.py > /tmp/walker_logs/s3_wallets.log 2>&1 \
  || echo "  ⚠️  wallet details errored — see /tmp/walker_logs/s3_wallets.log"

echo "[$(date '+%H:%M:%S')] Phase 5: import Solstice baseline totals"
python3 tools/set_solstice_total.py --import-file "$HERE/data/solstice_totals.jsonl"

echo "[$(date '+%H:%M:%S')] done — payloads in $HERE/site"
