"""Daily snapshot of every USX / eUSX / strcUSX token-account holder. Stdlib only.

WHY THIS IS PERISHABLE AND THE BALANCES ARE NOT
-----------------------------------------------
A wallet's balance history can always be rebuilt later: signatures are archival,
which is exactly what the HOLD walkers do. Discovery cannot. The walkers find
who to walk with `getProgramAccounts` over the token program — a CURRENT-state
read. When a wallet drains its position and closes the ATA, the account stops
existing and that wallet vanishes from every future scan. Its history is still
on chain, but nothing points at it any more, so it is never walked and its
season contribution silently reads zero.

That failure is invisible from inside the pipeline: the totals stay plausible
because the wallet simply is not in the denominator. Recording the holder set
each day means a wallet that appears once is walkable forever.

WHAT IS STORED, AND WHY NOT BALANCES
------------------------------------
Only the owner SET, delta-encoded: each line carries the owners added and
removed since the previous line, with a full row weekly (and whenever the log
is empty) so a replay never depends on an unbroken chain. Balances are left out
on purpose — they change every day as yield accrues, so a balance delta is a
full row every day, and balances are reconstructible anyway. Membership is not.

Full rows cost ~650 KB; a typical delta is a few hundred bytes, which keeps a
four-month season in single-digit MB instead of ~150 MB.

`n_holders` and `total` are kept per line as scalars — free, and they give the
walkers a same-day cross-check on coverage without a replay.

Usage:  python3 tools/capture_holder_set.py data/holder_sets.jsonl
Exit 0 always; sets appended=false on GITHUB_OUTPUT when nothing was written.
"""
import datetime as dt
import json
import os
import ssl
import struct
import sys
import urllib.request

RPC_URLS = [
    'https://api.mainnet-beta.solana.com',
    'https://solana-rpc.publicnode.com',
]
TOKEN_PROG  = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022  = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'

# SPL token account: mint(32) | owner(32) | amount(u64) | ...  — 165 bytes.
# The slice below covers exactly @0..@72, so `mint` is verified from the data
# rather than trusted from the filter alone.
MINTS = {
    'USX':         ('6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG', TOKEN_PROG),
    'eUSX':        ('3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC', TOKEN_PROG),
    'JR_strcUSX':  ('BQ6LPc68knpko292UsMLbQYfaHhWD7S84sA98632hrzX', TOKEN_PROG),
    'SR_strcUSX':  ('3uZLfBgY9XaLXG7C2DDdVjQWgS9x9kEb9VLpfe4yED4P', TOKEN_PROG),
}

_B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def b58(raw: bytes) -> str:
    n = int.from_bytes(raw, 'big')
    out = ''
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return '1' * (len(raw) - len(raw.lstrip(b'\0'))) + out


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def emit(**kw):
    p = os.environ.get('GITHUB_OUTPUT')
    if not p:
        return
    with open(p, 'a') as fh:
        for k, v in kw.items():
            fh.write(f'{k}={v}\n')


def fetch_holders(mint: str, program: str, ctx) -> dict | None:
    """{owner: ui_amount} for a mint, or None if every endpoint failed.

    An empty dict is never returned as success. A degraded RPC answering `[]`
    is indistinguishable from a mint nobody holds, and recording that would
    erase the holder set for the day — the precise failure that poisoned 18
    HOLD caches on 2026-08-12.
    """
    import base64 as _b64
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'getProgramAccounts',
                       'params': [program, {
                           'encoding': 'base64',
                           'dataSlice': {'offset': 0, 'length': 72},
                           'filters': [{'dataSize': 165},
                                       {'memcmp': {'offset': 0, 'bytes': mint}}]}]}).encode()
    for url in RPC_URLS:
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={'Content-Type': 'application/json',
                         'User-Agent': 'solstice-s3-archive/1.0'})
            with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
                j = json.load(r)
            res = j.get('result')
            if not res:
                print(f'  {url}: empty/error {str(j.get("error"))[:100]}', file=sys.stderr)
                continue
            out = {}
            for a in res:
                d = _b64.b64decode(a['account']['data'][0])
                if len(d) < 72 or b58(d[0:32]) != mint:
                    continue
                amt = struct.unpack_from('<Q', d, 64)[0] / 1e6
                if amt > 0:
                    owner = b58(d[32:64])
                    out[owner] = out.get(owner, 0.0) + amt
            if out:
                return out
        except Exception as e:
            print(f'  {url}: {type(e).__name__}: {str(e)[:100]}', file=sys.stderr)
    return None


FULL_EVERY_DAYS = 7


def replay(path: str) -> tuple[dict, set, int]:
    """({asset: set(owners)}, {(date, asset)} seen, lines since last full row).

    Replays the log so a delta can be diffed against the current membership.
    A malformed line is skipped rather than fatal — a corrupt tail must not
    stop today's capture, which is the only part that cannot be redone later.
    """
    state, seen, since_full = {}, set(), {}
    if not os.path.exists(path):
        return state, seen, since_full
    with open(path) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            a = r.get('asset')
            if not a:
                continue
            seen.add((r.get('date_utc'), a))
            cur = state.setdefault(a, set())
            if r.get('mode') == 'full':
                state[a] = set(r.get('owners') or [])
                since_full[a] = 0
            else:
                cur.update(r.get('added') or [])
                cur.difference_update(r.get('removed') or [])
                since_full[a] = since_full.get(a, 0) + 1
    return state, seen, since_full


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'data/holder_sets.jsonl'
    date_utc = dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')

    prev_state, existing, since_full = replay(path)

    ctx = _ctx()
    wrote = 0
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    for label, (mint, prog) in MINTS.items():
        if (date_utc, label) in existing:
            print(f'{label}: {date_utc} already captured — skipping.')
            continue
        holders = fetch_holders(mint, prog, ctx)
        if holders is None:
            print(f'{label}: ALL endpoints failed — no row written '
                  f'(a wrong empty set would erase the day).', file=sys.stderr)
            continue
        owners = set(holders)
        prev = prev_state.get(label)
        full = prev is None or since_full.get(label, 0) >= FULL_EVERY_DAYS
        row = {
            'date_utc': date_utc,
            'asset': label,
            'mint': mint,
            'captured_at': dt.datetime.now(dt.UTC).isoformat(),
            'mode': 'full' if full else 'delta',
            'n_holders': len(owners),
            'total': round(sum(holders.values()), 6),
        }
        if full:
            row['owners'] = sorted(owners)
        else:
            row['added'] = sorted(owners - prev)
            row['removed'] = sorted(prev - owners)
        with open(path, 'a') as fh:
            fh.write(json.dumps(row, separators=(',', ':')) + '\n')
        wrote += 1
        churn = ('full snapshot' if full
                 else f"+{len(row['added'])} / -{len(row['removed'])}")
        print(f'{label}: {len(owners):,} holders, {row["total"]:,.2f} total  ({churn})')

    emit(appended='true' if wrote else 'false', date_utc=date_utc, assets=wrote)
    return 0


if __name__ == '__main__':
    sys.exit(main())
