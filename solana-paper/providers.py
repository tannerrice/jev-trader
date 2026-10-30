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
    unmeasured = sorted({x.get('pairAddress') for x in valid if liq(x) is None}, key=str)
    if any(not isinstance(u, str) for u in unmeasured):
        raise DataError('unidentified unmeasured pool')
    return dict(mint=mint, pair=p['pairAddress'], unmeasured_pools=unmeasured, symbol=str(p['baseToken'].get('symbol', mint))[:40],
                price=price, liquidity_usd=liquidity, age_minutes=(now-created)/60,
                buys_1h=buys, sells_1h=sells, observed_at=now)


def normalize_rug(d, mint, pair, now, unmeasured=(), creator_pct=None, resolve_unmeasured=None):
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
    # Conservative ambiguity rule: every DexScreener pool lacking a liquidity
    # figure must appear in Rugcheck markets with a measured depth strictly below
    # the quoted (deepest) pool. Otherwise it could be the real main pool: reject.
    by_key = {}
    for m in markets:
        by_key.setdefault(m.get('pubkey'), []).append(m)
    for u in unmeasured:
        if u == pair or len(by_key.get(u, [])) != 1 or not depth(by_key[u][0]) < depth(biggest):
            # Optional GeckoTerminal cross-check (default OFF): only for a leftover
            # pool Rugcheck does not list at all. It raises DataError unless it
            # proves the leftover is strictly shallower with established freshness.
            if resolve_unmeasured is not None and u != pair and u not in by_key:
                resolve_unmeasured(u, depth(biggest))
                continue
            raise DataError('ambiguous unmeasured pool')
    if sum(1 for m in markets if depth(m) == depth(biggest)) > 1:
        raise DataError('ambiguous deepest pool')
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
    if creators:
        dev_pct, dev_source = sum(number(h['pct']) for h in creators), 'rugcheck_top_holders'
    elif creator_pct is not None and isinstance(creator, str) and creator:
        # Optional verified source (default OFF): RPC balance of the explicit creator.
        dev_pct, dev_source = number(creator_pct(mint, creator)), 'rpc_creator_holdings'
    else:
        raise DataError('creator percentage unverified')
    return dict(rugged=d['rugged'], mint_authority_open=authority('mintAuthority'),
                freeze_authority_open=authority('freezeAuthority'),
                danger_flag=any(r['level'] == 'danger' for r in d['risks']),
                risk_score=number(d.get('score_normalised')),
                lp_locked_pct=number(biggest['lp'].get('lpLockedPct')),
                top10_pct=sum(sorted(eligible, reverse=True)[:10]),
                dev_pct=dev_pct, dev_pct_source=dev_source, insiders=insiders,
                rug_observed_at=now)


TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022_PROGRAM = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
PUBLIC_RPC = 'https://api.mainnet-beta.solana.com'
MAX_SLOT_SPREAD = 150  # ~1 minute; responses may come from different RPC nodes
HIDDEN_BALANCE_EXTENSIONS = {'confidentialTransferMint', 'confidentialTransferAccount',
                             'confidentialTransferFeeConfig', 'confidentialTransferFeeAmount',
                             'confidentialMintBurn'}


def _raw_amount(value):
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,30}', value):
        raise DataError('invalid RPC token amount')
    return int(value)


def _rpc_value(response):
    if not isinstance(response, dict) or 'error' in response or not isinstance(response.get('result'), dict):
        raise DataError('RPC error')
    slot = response['result'].get('context', {}).get('slot')
    if type(slot) is not int or slot <= 0:
        raise DataError('RPC context slot missing')
    return response['result'].get('value'), slot


def _extensions(info):
    ext = info.get('extensions', [])
    if not isinstance(ext, list) or any(not isinstance(e, dict) for e in ext):
        raise DataError('invalid token extensions')
    if any(e.get('extension') in HIDDEN_BALANCE_EXTENSIONS for e in ext):
        raise DataError('confidential balances cannot be verified')


def rpc_creator_pct(mint, creator, call):
    """Creator's CURRENT direct holdings of `mint` as % of supply, via read-only RPC.

    `creator` must be an explicit provider-reported address (never the mint
    authority by assumption). An empty account list counts as 0 only because the
    RPC call succeeded for that explicit owner. Every failure raises DataError.
    Linked/funded wallets are NOT detected.
    """
    mint_id(mint)
    if not isinstance(creator, str):
        raise DataError('creator address unavailable')
    mint_id(creator)
    acct, s1 = _rpc_value(call('getAccountInfo', [mint, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}]))
    if not isinstance(acct, dict) or acct.get('owner') not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise DataError('mint not owned by a token program')
    program = acct['owner']
    data = acct.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('parsed'), dict) or data['parsed'].get('type') != 'mint':
        raise DataError('mint account not parsed')
    info = data['parsed'].get('info')
    if not isinstance(info, dict) or info.get('isInitialized') is not True:
        raise DataError('mint not initialized')
    decimals = info.get('decimals')
    if type(decimals) is not int or not 0 <= decimals <= 18:
        raise DataError('invalid decimals')
    _extensions(info)
    mint_supply = _raw_amount(info.get('supply'))
    sup, s2 = _rpc_value(call('getTokenSupply', [mint, {'commitment': 'confirmed'}]))
    if not isinstance(sup, dict) or sup.get('decimals') != decimals or _raw_amount(sup.get('amount')) != mint_supply:
        raise DataError('inconsistent supply/decimals')
    if mint_supply <= 0:
        raise DataError('zero supply')
    accounts, s3 = _rpc_value(call('getTokenAccountsByOwner', [creator, {'mint': mint},
                                   {'encoding': 'jsonParsed', 'commitment': 'confirmed'}]))
    if not isinstance(accounts, list):
        raise DataError('invalid owner accounts')
    if max(s1, s2, s3) - min(s1, s2, s3) > MAX_SLOT_SPREAD:
        raise DataError('inconsistent RPC slots')
    held, seen = 0, set()
    for a in accounts:
        try:
            key, acc = a['pubkey'], a['account']
            parsed = acc['data']['parsed']
            ai = parsed['info']
        except (KeyError, TypeError):
            raise DataError('invalid token account') from None
        if key in seen or acc.get('owner') != program or parsed.get('type') != 'account':
            raise DataError('invalid token account')
        seen.add(key)
        if ai.get('mint') != mint or ai.get('owner') != creator:
            raise DataError('token account identity mismatch')
        _extensions(ai)
        amount = ai.get('tokenAmount')
        if not isinstance(amount, dict) or amount.get('decimals') != decimals:
            raise DataError('mismatched account decimals')
        held += _raw_amount(amount.get('amount'))
    if held > mint_supply:
        raise DataError('holdings exceed supply')
    return held * 100 / mint_supply


def rpc_call(url=PUBLIC_RPC, post=request_json):
    def call(method, params):
        if method not in ('getAccountInfo', 'getTokenSupply', 'getTokenAccountsByOwner'):
            raise DataError('RPC method not allowed')
        return post(url, body={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
    return call


def pumpfun_first(pumpfun_resolve, pairs, fallback=None):
    """Route a leftover DexScreener `pumpfun` pool to the pump.fun verifier; any other
    venue goes to `fallback` (gecko, if enabled) or stays rejected as before."""
    def resolve(leftover, rc_depth):
        dex = [p.get('dexId') for p in pairs if isinstance(p, dict) and p.get('pairAddress') == leftover]
        if dex == ['pumpfun'] or fallback is None:
            return pumpfun_resolve(leftover, rc_depth)
        return fallback(leftover, rc_depth)
    return resolve


class MarketData:
    def __init__(self, get=request_json, clock=time.time, rpc=None, gecko=None, pumpfun=None):
        """rpc: optional JSON-RPC callable enabling the RPC creator-holdings source (default OFF).
        gecko: optional gecko.GeckoCrossCheck for unmeasured leftover pools (default OFF).
        pumpfun: optional pumpfun_verify.PumpfunVerifier for leftover pump.fun curves (default OFF)."""
        self.get, self.clock, self.rpc, self.gecko, self.pumpfun = get, clock, rpc, gecko, pumpfun
        self.last_pairs = []

    def discover(self):
        rows = self.get('https://api.dexscreener.com/token-profiles/latest/v1')
        if not isinstance(rows, list):
            raise DataError('invalid discovery response')
        return list(dict.fromkeys(mint_id(r['tokenAddress']) for r in rows if r.get('chainId') == 'solana'))[:10]

    def quote(self, mint, pair=None):
        rows = self.get('https://api.dexscreener.com/token-pairs/v1/solana/' + mint_id(mint))
        self.last_pairs = rows if isinstance(rows, list) else []
        return normalize_pair(rows, mint, self.clock(), pair)

    def snapshot(self, mint):
        q = self.quote(mint)
        d = self.get('https://api.rugcheck.xyz/v1/tokens/' + mint_id(mint) + '/report')
        source = None if self.rpc is None else (lambda m, c: rpc_creator_pct(m, c, self.rpc))
        resolver = None if self.gecko is None else self.gecko.resolver(mint, q, self.last_pairs)
        if self.pumpfun is not None:
            resolver = pumpfun_first(self.pumpfun.resolver(mint, q, self.last_pairs), self.last_pairs, resolver)
        return {**q, **normalize_rug(d, mint, q['pair'], self.clock(), q['unmeasured_pools'], source,
                                     resolve_unmeasured=resolver)}
