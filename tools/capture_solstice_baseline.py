"""Append a daily snapshot of every public Solstice number to a JSONL log.

Runs in CI (.github/workflows/solstice-baseline.yml) where there is no DB and
no pip install, so it deliberately avoids `requests` and never touches SQLite.
The JSONL is the durable record; `set_solstice_total.py --import-file` loads the
flare totals into flares_snapshots on whatever machine has the DB.

WHY CAPTURE EVERYTHING: none of these endpoints expose history — they return
only the current value. Any number not written down on the day it was published
is gone. One line a day costs ~2KB and buys a permanent series for supply, peg,
TVL, PoR backing, SLX campaign parameters and the quest catalog.

The quest catalog matters most: S3 launched with 5 quests and S2 grew to 24, so
a diff of `quests.by_code` between consecutive lines is how we find out a new
partner quest went live — and `quests_changed` flags exactly that day.

Each line records the season the totals belong to, because Solstice's endpoints
always report the CURRENTLY live season and the totals reset at each boundary.
Importing without that field would eventually staple one season's total onto
another's series.

Endpoints are fetched independently: one failing never discards the others, and
whatever failed is listed under `errors` so a partial line is never mistaken for
a complete one.

CADENCE: the workflow runs twice a day (00:05 and 12:05 UTC). The flare total
itself only moves at Solstice's 00:00 UTC publish, so the midday run is mostly a
free retry — if the 00:05 run hit a network blip, the day is not lost. It does
add real intraday resolution for the numbers that move continuously (eUSX peg,
USX supply, TVL, partner APYs).

Dedup is therefore time-based, not one-per-day: a capture is skipped if the last
one for this season was less than `--min-interval-hours` (default 6) ago. That
lets the two scheduled runs both land while making an accidental re-run or a
manual dispatch minutes later a no-op.

Usage:  python3 tools/capture_solstice_baseline.py data/solstice_totals.jsonl
        python3 tools/capture_solstice_baseline.py FILE --min-interval-hours 0
Exit 0 always (a transient API failure must not fail the workflow); sets
`appended=false` on GITHUB_OUTPUT when nothing was written.
"""
import json, os, ssl, sys, urllib.request
import datetime as dt

BASE = 'https://app.solstice.finance'
ENDPOINTS = {
    'flares':    '/api/flares/analytics',
    'rewards':   '/api/rewards/global/analytics',
    'protocol':  '/api/protocol',
    'tvl':       '/api/protocol/tvl',
    'por':       '/api/protocol/por',
    'quests':    '/api/flares/quests',
    'partners':  '/api/partners',
}


def _ssl_context():
    """CI (ubuntu) has a system CA bundle and the default context just works.
    The python.org macOS builds ship none, so a local run dies with
    CERTIFICATE_VERIFY_FAILED — use certifi's bundle when it's importable.
    Still stdlib-only where it matters: CI never needs the fallback."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def emit(**kw):
    """Write GitHub Actions step outputs (no-op outside CI)."""
    p = os.environ.get('GITHUB_OUTPUT')
    if not p:
        return
    with open(p, 'a') as fh:
        for k, v in kw.items():
            fh.write(f'{k}={v}\n')


def fetch_all(ctx):
    """{name: payload} for every endpoint that answered, plus {name: error}."""
    out, errors = {}, {}
    for name, path in ENDPOINTS.items():
        try:
            req = urllib.request.Request(
                BASE + path, headers={'User-Agent': 'solstice-flares-dashboard/1.0'})
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                out[name] = json.load(r)
        except Exception as e:
            errors[name] = f'{type(e).__name__}: {e}'
            print(f'  WARN {path}: {e}', file=sys.stderr)
    return out, errors


def _num(v):
    """Solstice returns some numbers as strings (e.g. tvl). Normalize or drop."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_row(raw: dict, errors: dict) -> dict | None:
    """Compact one fetch into a single line. Returns None if the flare total —
    the one field the importer needs — is missing or zero."""
    fl = raw.get('flares') or {}
    season = fl.get('season') or {}
    total = _num(fl.get('totalFlares'))
    if total is None:
        # Fall back to the rewards endpoint, which reports the same figure.
        total = _num(((raw.get('rewards') or {}).get('flare') or {}).get('totalFlare'))
    if not total or total <= 0:
        return None

    rw = (raw.get('rewards') or {}).get('flare') or {}
    pr = raw.get('protocol') or {}
    por = (raw.get('por') or {}).get('porReport') or {}
    quests = (raw.get('quests') or {}).get('quests') or []
    partners = (raw.get('partners') or {}).get('partners') or []

    row = {
        'date_utc':    dt.datetime.now(dt.UTC).strftime('%Y-%m-%d'),
        'captured_at': dt.datetime.now(dt.UTC).isoformat(),
        # --- keys the importer reads (schema-stable, do not rename) ---
        'season_id':    season.get('id'),
        'season_name':  season.get('name'),
        'total_flares': total,
        'total_users':  int(fl.get('totalUsers') or 0),
        # --- SLX campaign parameters ---
        'campaign': {
            'slx_allocation_base':   _num(fl.get('campaignSlxAllocationBase')),
            'slx_allocation_change': _num(fl.get('campaignSlxAllocationChange')),
            'slx_total_supply':      _num(fl.get('slxTotalSupply')),
            'slx_burned':            _num(fl.get('slxBurned')),
            'slx_bought_back':       _num(fl.get('slxBoughtBack')),
            'slx_value':             _num(rw.get('slxValue')),
            'change':                rw.get('campaignChange'),
        },
        # --- protocol state: supply, peg, TVL, proof-of-reserve ---
        'protocol': {
            'usx_supply':   _num(pr.get('usxSupply')),
            'eusx_supply':  _num(pr.get('eusxSupply')),
            'eusx_backing': _num(pr.get('eusxBacking')),
            'eusx_price':   _num(pr.get('eusxPrice')),
            'tvl':          _num((raw.get('tvl') or {}).get('tvl')),
            'por_healthy':        por.get('healthy'),
            'por_total_backing':  _num(por.get('totalBacking')),
            'por_custodians':     len(por.get('custodiansVerified') or []),
        },
        # --- quest catalog, compacted: descriptions/links dropped, the fields
        #     that actually change kept. This is the new-quest tripwire. ---
        'quests': {
            'n': len(quests),
            'by_code': {q['questCode']: {'m': q.get('multiplier'),
                                         'active': q.get('isActive'),
                                         'min': q.get('minBalance')}
                        for q in quests if q.get('questCode')},
        },
        'partners': {p.get('partner'): _num(p.get('apy'))
                     for p in partners if p.get('partner')},
    }
    if errors:
        row['errors'] = errors
    return row


def read_lines(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _hours_since(iso: str | None) -> float:
    """Hours since an ISO8601 capture stamp; +inf when absent or unparseable
    (so a line without one never blocks a capture)."""
    if not iso:
        return float('inf')
    try:
        t = dt.datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.UTC)
        return (dt.datetime.now(dt.UTC) - t).total_seconds() / 3600.0
    except ValueError:
        return float('inf')


def main():
    args = [a for a in sys.argv[1:]]
    min_gap = 6.0
    if '--min-interval-hours' in args:
        i = args.index('--min-interval-hours')
        min_gap = float(args[i + 1])
        del args[i:i + 2]
    path = args[0] if args else 'data/solstice_totals.jsonl'
    raw, errors = fetch_all(_ssl_context())
    row = build_row(raw, errors)
    if row is None:
        # Between seasons the endpoints can report 0. Recording that would draw
        # a collapse to zero on the chart, so skip and keep the last real point.
        print('No usable flare total (0/empty or endpoint down) — nothing recorded.')
        emit(appended='false')
        return 0

    existing = read_lines(path)
    # Time-based idempotency (see CADENCE above): both scheduled runs land, but
    # a re-run or manual dispatch inside the window is a no-op.
    same_season = [e for e in existing if e.get('season_name') == row['season_name']]
    if min_gap > 0 and same_season:
        gap = _hours_since(same_season[-1].get('captured_at'))
        if gap < min_gap:
            print(f'Last {row["season_name"]} capture was {gap:.1f}h ago '
                  f'(< {min_gap:g}h) — nothing to do.')
            emit(appended='false')
            return 0

    prev = existing[-1] if existing else None
    if prev is not None:
        row['quests_changed'] = (
            (prev.get('quests') or {}).get('by_code') != row['quests']['by_code'])

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a') as fh:
        fh.write(json.dumps(row, separators=(',', ':')) + '\n')

    print(f'Appended {row["date_utc"]}  {row["season_name"]}  '
          f'flares={row["total_flares"]:,.2f}  users={row["total_users"]:,}  '
          f'quests={row["quests"]["n"]}  usx_supply={row["protocol"]["usx_supply"] or 0:,.0f}')
    if row.get('quests_changed'):
        old = set((prev.get('quests') or {}).get('by_code') or {})
        new = set(row['quests']['by_code'])
        if new - old:
            print(f'  QUEST CATALOG CHANGED — new: {sorted(new - old)}')
        if old - new:
            print(f'  QUEST CATALOG CHANGED — removed: {sorted(old - new)}')
        if new == old:
            print(f'  QUEST CATALOG CHANGED — multiplier/minBalance/isActive edit')
    if errors:
        print(f'  PARTIAL: {len(errors)} endpoint(s) failed: {sorted(errors)}')
    emit(appended='true', date_utc=row['date_utc'],
         total_flares=f'{row["total_flares"]:,.0f}',
         quests_changed='true' if row.get('quests_changed') else 'false',
         n_quests=row['quests']['n'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
