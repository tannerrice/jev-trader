"""Read-only public data adapters. Missing evidence is never a safe default."""
import json
import math
import re
import time
import urllib.request


class DataError(ValueError):
    pass


def number(value, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise DataError('invalid numeric evidence')
    return value


def mint_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', value):
        raise DataError('invalid Solana mint')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DataError('unexpected provider redirect')


def request_json(url, body=None, headers=None):
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body, allow_nan=False).encode(),
                                     headers={'User-Agent': 'solana-paper-desk/1', 'Content-Type': 'application/json', **(headers or {})})
    # Never include response bodies or request headers in error messages/logs.
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise DataError('provider response too large')
        return json.loads(raw)
    except Exception:
        raise DataError('provider request failed') from None


def normalize_pair(pairs, mint, now, pair_id=None):
    mint_id(mint)
    valid = [p for p in pairs if p.get('chainId') == 'solana' and p.get('baseToken', {}).get('address') == mint
             and (pair_id is None or p.get('pairAddress') == pair_id)]
    if not valid:
        raise DataError('no matching base-token pool')
    def liq(x):
        v = (x.get('liquidity') or {}).get('usd')
        return v if type(v) in (int, float) and math.isfinite(v) and v >= 0 else None
    # DexScreener omits `liquidity` for bonding-curve pools (e.g. pumpfun). An
    # unknown-depth pool must not invalidate a sibling pool with reported depth;
    # it is ignored (never treated as 0). With no measured pool, reject.
    measured = [x for x in valid if liq(x) is not None]
    if not measured:
        raise DataError('pool liquidity unavailable')
    p = max(measured, key=liq)
    liquidity = number(liq(p))
    try:
        price = float(p['priceUsd'])
    except (KeyError, TypeError, ValueError):
        raise DataError('missing price') from None
    number(price, 1e-30)
    created = number(p.get('pairCreatedAt')) / 1000
    if created > now or not isinstance(p.get('pairAddress'), str):
        raise DataError('invalid pool identity/age')
    tx = p.get('txns', {}).get('h1', {})
    buys, sells = number(tx.get('buys')), number(tx.get('sells'))
    if int(buys) != buys or int(sells) != sells:
        raise DataError('invalid transaction counts')
    return dict(mint=mint, pair=p['pairAddress'], symbol=str(p['baseToken'].get('symbol', mint))[:40],
                price=price, liquidity_usd=liquidity, age_minutes=(now-created)/60,
                buys_1h=buys, sells_1h=sells, observed_at=now)


def normalize_rug(d, mint, pair, now):
    if d.get('mint') != mint:
        raise DataError('Rugcheck mint mismatch')
    token = d.get('token')
    if not isinstance(token, dict):
        raise DataError('missing token report')
    def authority(key):
        if key not in token or (token[key] is not None and not isinstance(token[key], str)):
            raise DataError('missing authority evidence')
        return token[key] is not None
    if type(d.get('rugged')) is not bool or not isinstance(d.get('risks'), list):
        raise DataError('missing risk evidence')
    if any(not isinstance(r, dict) or r.get('level') not in ('danger', 'warn', 'info') for r in d['risks']):
        raise DataError('unknown risk level')
    # Require the same pool as our quote, as well as the deepest reported pool.
    markets = d.get('markets')
    if not isinstance(markets, list) or not markets:
        raise DataError('missing markets')
    def depth(m):
        return number(m['lp']['baseUSD']) + number(m['lp']['quoteUSD'])
    biggest = max(markets, key=depth)
    if biggest.get('pubkey') != pair:
        raise DataError('quote is not deepest Rugcheck pool')
    holders, known = d.get('topHolders'), d.get('knownAccounts')
    if not isinstance(holders, list) or not holders or not isinstance(known, dict):
        raise DataError('missing holder attribution')
    eligible = []
    insiders = number(d.get('graphInsidersDetected'))
    for h in holders:
        if type(h.get('insider')) is not bool:
            raise DataError('missing insider evidence')
        insiders += int(h['insider'])
        tag = known.get(h.get('owner'), known.get(h.get('address'), {}))
        if str(tag.get('type', '')).upper() not in ('AMM', 'LOCKER'):
            eligible.append(number(h.get('pct')))
    # creatorBalance units are not guaranteed by Swagger. Require explicit creator
    # holder percentage instead of dividing potentially mixed decimal/raw units.
    creator = d.get('creator')
    creators = [h for h in holders if creator and h.get('owner') == creator]
    if not creators:
        raise DataError('creator percentage unverified')
    return dict(rugged=d['rugged'], mint_authority_open=authority('mintAuthority'),
                freeze_authority_open=authority('freezeAuthority'),
                danger_flag=any(r['level'] == 'danger' for r in d['risks']),
                risk_score=number(d.get('score_normalised')),
                lp_locked_pct=number(biggest['lp'].get('lpLockedPct')),
                top10_pct=sum(sorted(eligible, reverse=True)[:10]),
                dev_pct=sum(number(h['pct']) for h in creators), insiders=insiders,
                rug_observed_at=now)


class MarketData:
    def __init__(self, get=request_json, clock=time.time):
        self.get, self.clock = get, clock

    def discover(self):
        rows = self.get('https://api.dexscreener.com/token-profiles/latest/v1')
        if not isinstance(rows, list):
            raise DataError('invalid discovery response')
        return list(dict.fromkeys(mint_id(r['tokenAddress']) for r in rows if r.get('chainId') == 'solana'))[:10]

    def quote(self, mint, pair=None):
        rows = self.get('https://api.dexscreener.com/token-pairs/v1/solana/' + mint_id(mint))
        return normalize_pair(rows, mint, self.clock(), pair)

    def snapshot(self, mint):
        q = self.quote(mint)
        d = self.get('https://api.rugcheck.xyz/v1/tokens/' + mint_id(mint) + '/report')
        return {**q, **normalize_rug(d, mint, q['pair'], self.clock())}
