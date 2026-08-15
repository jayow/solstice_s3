"""Daily on-chain snapshot of every Exponent YT position. Stdlib only.

WHY THIS RUNS IN CI RATHER THAN LOCALLY
---------------------------------------
Exponent YT balances cannot be reconstructed from events — forward replay,
backward on-chain anchoring, and core-market event indexing were each built and
each failed on a wallet whose position measurably moved 0 -> 17,428.74 YT with
zero indexable events. So YT flares are credited only for days actually
OBSERVED, which makes a missed snapshot a permanently lost day.

That is too fragile to hang on a laptop being awake, so the capture lives here
next to the baseline job: no DB, no RPC key, no secrets. A `dataSlice` keeps the
read to 80 bytes per position (~1,600 accounts, ~2s on the public endpoint), and
the result is appended to data/yt_positions.jsonl for the dashboard build to
import.

ACCOUNT LAYOUT (yield position, disc e35c92311d55475e, 164B):
    @0   discriminator (8)
    @8   owner pubkey (32)
    @40  core market pubkey (32)
    @72  u64 YT balance, 6dp
The slice below covers exactly @0..@80, so the discriminator is verified rather
than assumed from the market filter alone.

The ORDERBOOK holds one pooled position containing every resting maker's
escrowed YT; it is recorded separately as `orderbook_yt` so the importer can
allocate it pro-rata without double-counting.

Usage:  python3 tools/capture_yt_positions.py data/yt_positions.jsonl
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
EXPONENT_PROG = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'
YIELD_POSITION_DISC = 'e35c92311d55475e'

MARKETS = {
    'USX-Sep26':  {'core': 'CdUviheAUJaXUryT7JCRDUoNdPXdVvkxNQY1okC6uY8S',
                   'orderbook': 'A2yaEiehRCvibSdMWWJtrBdmVCYwGRNSNwg1VwdicthU'},
    'eUSX-Sep26': {'core': 'B78XAMSpB5KQqykw9oEec1nFSPeRqYtbTmsxo9EPwAUW',
                   'orderbook': '3mXbVuMynj21doFXXEauJ2tGDV9kS2Q1SnnQDcgD54Bw'},
}

_B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def b58(raw: bytes) -> str:
    """Minimal base58 encode — avoids a dependency in a stdlib-only job."""
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


def fetch_positions(core: str, ctx) -> list | None:
    """[(owner, yt)] for a market, or None if every endpoint failed. An empty
    list is never returned as success — a market with zero positions would be
    indistinguishable from a degraded RPC, and a wrong zero silently erases a
    day for every holder."""
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'getProgramAccounts',
                       'params': [EXPONENT_PROG, {
                           'encoding': 'base64',
                           'dataSlice': {'offset': 0, 'length': 80},
                           'filters': [{'memcmp': {'offset': 40, 'bytes': core}}]}]}).encode()
    import base64 as _b64
    for url in RPC_URLS:
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={'Content-Type': 'application/json',
                         'User-Agent': 'solstice-s3-archive/1.0'})
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                j = json.load(r)
            res = j.get('result')
            if not res:
                print(f'  {url}: empty/error {str(j.get("error"))[:100]}', file=sys.stderr)
                continue
            out = []
            for a in res:
                d = _b64.b64decode(a['account']['data'][0])
                if len(d) < 80 or d[:8].hex() != YIELD_POSITION_DISC:
                    continue
                yt = struct.unpack_from('<Q', d, 72)[0] / 1e6
                if yt > 0:
                    out.append((b58(d[8:40]), yt))
            if out:
                return out
        except Exception as e:
            print(f'  {url}: {type(e).__name__}: {str(e)[:100]}', file=sys.stderr)
    return None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'data/yt_positions.jsonl'
    date_utc = dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')

    existing = set()
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    existing.add((r.get('date_utc'), r.get('market')))
                except json.JSONDecodeError:
                    continue

    ctx = _ctx()
    wrote = 0
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    for label, cfg in MARKETS.items():
        if (date_utc, label) in existing:
            print(f'{label}: {date_utc} already captured — skipping.')
            continue
        pos = fetch_positions(cfg['core'], ctx)
        if pos is None:
            print(f'{label}: ALL endpoints failed — no row written '
                  f'(an unobserved day must stay unobserved).', file=sys.stderr)
            continue
        ob_yt = next((yt for w, yt in pos if w == cfg['orderbook']), 0.0)
        holders = {w: yt for w, yt in pos if w != cfg['orderbook']}
        row = {
            'date_utc': date_utc,
            'market': label,
            'core': cfg['core'],
            'captured_at': dt.datetime.now(dt.UTC).isoformat(),
            'n_holders': len(holders),
            'total_yt': round(sum(holders.values()) + ob_yt, 6),
            'orderbook_yt': round(ob_yt, 6),
            'positions': {w: round(yt, 6) for w, yt in holders.items()},
        }
        with open(path, 'a') as fh:
            fh.write(json.dumps(row, separators=(',', ':')) + '\n')
        wrote += 1
        print(f'{label}: {len(holders):,} holders, {row["total_yt"]:,.2f} YT '
              f'(orderbook pool {ob_yt:,.2f})')

    emit(appended='true' if wrote else 'false', date_utc=date_utc, markets=wrote)
    return 0


if __name__ == '__main__':
    sys.exit(main())
