"""Optional GeckoTerminal cross-check for an unmeasured LEFTOVER pool (default OFF).

Must stay disabled until the code is reviewed (see GECKOTERMINAL.md). Enabled only
by `run.py --gecko-crosscheck` or SOLANA_GECKO_CROSSCHECK=yes.

Scope: called only from providers.normalize_rug's ambiguity rule, for a DexScreener
pool that has no liquidity figure, is not the quoted pool, and is absent from
Rugcheck markets. It can only *resolve* that ambiguity; every other check
(deepest pool, exact-pool LP lock, $30,000 minimum, creator, ...) still applies.
Bonding-curve-only tokens never reach it (normalize_pair rejects them first).

Fail-closed: every failure raises providers.DataError('ambiguous unmeasured pool;
gecko: <reason>'). Missing/null/zero/rounded reserves are never treated as a
verified zero. GeckoTerminal publishes no per-pool data timestamp, so by default
freshness is NOT established and the ambiguous pool is still rejected
('gecko freshness unverified'). Local fetch time proves when we asked, not how
old the upstream number is.
"""
import email.utils
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

from providers import DataError, mint_id

BASE = 'https://api.geckoterminal.com/api/v2'
ACCEPT = 'application/json;version=20230203'  # version string from api.geckoterminal.com/docs
TIMEOUT = 10
CACHE_TTL = 120.0
DEFAULT_CAP = 5           # requests per process run
ABSOLUTE_MAX_CAP = 10     # env can lower/raise up to this, never above
MIN_SPACING = 2.0         # seconds between requests (documented limit is ~5-30/min, shared by IP)
MAX_RETRY_AFTER_WAIT = 0  # never sleep on 429; treat as unavailable
MAX_DATE_SKEW = 120.0     # response Date header must be within this of local clock
CONTRADICTION_TOLERANCE = 0.25  # |GT quoted - DS quoted| / DS quoted
MIN_MEANINGFUL_USD = 1.0
RESERVE_RE = re.compile(r'(0|[1-9][0-9]{0,15})\.([0-9]{2,})')
SOL_NETWORK = 'solana'


def enabled(flag=False, env=None):
    env = os.environ if env is None else env
    return bool(flag) or env.get('SOLANA_GECKO_CROSSCHECK') == 'yes'


def request_cap(env=None):
    env = os.environ if env is None else env
    try:
        cap = int(env.get('SOLANA_GECKO_MAX_REQUESTS', DEFAULT_CAP))
    except ValueError:
        cap = DEFAULT_CAP
    return max(0, min(cap, ABSOLUTE_MAX_CAP))


def http_get(url, timeout=TIMEOUT):
    """Returns (status, headers{lower: value}, body bytes). Never raises for HTTP status."""
    req = urllib.request.Request(url, headers={'Accept': ACCEPT, 'User-Agent': 'solana-paper-desk/1'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(1_000_001)
            return r.status, {k.lower(): v for k, v in r.headers.items()}, body
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, b''


def no_freshness_evidence(attrs, headers, fetched_at):
    """GeckoTerminal's documented pool schema has no last-updated/data timestamp.
    pool_created_at is creation time; Cache-Control/Age describe HTTP caching, not
    indexer lag. So freshness can't be established: return None (=> reject)."""
    return None


def parse_reserve(value):
    """Return a float for a precise, positive measured reserve; else raise with reason."""
    if value is None:
        raise ValueError('reserve missing/null')
    if not isinstance(value, str):
        raise ValueError('reserve not a decimal string')
    if not re.fullmatch(r'[0-9]{1,16}(\.[0-9]+)?', value):
        raise ValueError('reserve non-numeric')
    v = float(value)
    if not math.isfinite(v) or v < 0:
        raise ValueError('reserve invalid')
    if v == 0:
        # GeckoTerminal returns "0.0" for unindexed/bonding-curve leftovers; the docs
        # give no indicator distinguishing a measured zero from "not computed".
        raise ValueError('reserve zero is not a verified zero')
    if not RESERVE_RE.fullmatch(value):
        raise ValueError('reserve possibly rounded (<2 decimals)')
    if v < MIN_MEANINGFUL_USD:
        raise ValueError('reserve below display precision')
    return v


class GeckoCrossCheck:
    def __init__(self, get=http_get, clock=time.time, sleep=time.sleep, monotonic=time.monotonic,
                 cap=None, evidence_path=None, freshness=no_freshness_evidence):
        self.get, self.clock, self.sleep, self.monotonic = get, clock, sleep, monotonic
        self.cap = request_cap() if cap is None else max(0, min(int(cap), ABSOLUTE_MAX_CAP))
        self.requests, self.cache, self.last_request = 0, {}, None
        self.evidence_path, self.freshness = evidence_path, freshness

    # -- HTTP with cache, cap, spacing --------------------------------------
    def _fetch(self, addresses):
        key = ','.join(addresses)
        hit = self.cache.get(key)
        now = self.clock()
        if hit and now - hit[0] <= CACHE_TTL:
            return hit[1], True
        if self.requests >= self.cap:
            raise ValueError('gecko request cap reached')
        if self.last_request is not None:
            wait = MIN_SPACING - (self.monotonic() - self.last_request)
            if wait > 0:
                self.sleep(wait)
        self.requests += 1
        self.last_request = self.monotonic()
        url = '%s/networks/%s/pools/multi/%s' % (BASE, SOL_NETWORK, key)
        fetched_at = self.clock()
        try:
            status, headers, body = self.get(url, TIMEOUT)
        except Exception as e:
            raise ValueError('timeout' if 'timed out' in str(e).lower() or isinstance(e, TimeoutError)
                             else 'request failed') from None
        if status == 429:
            raise ValueError('rate limited (429)')
        if status != 200:
            raise ValueError('http %s' % status)
        if len(body) > 1_000_000:
            raise ValueError('response too large')
        try:
            data = json.loads(body)
        except Exception:
            raise ValueError('malformed JSON') from None
        result = dict(url=url, fetched_at=fetched_at, headers=headers, data=data)
        self.cache[key] = (fetched_at, result)
        return result, False

    def _log(self, rec):
        if not self.evidence_path:
            return
        try:
            with open(self.evidence_path, 'a') as f:
                f.write(json.dumps(rec, sort_keys=True, default=str) + '\n')
        except Exception:
            pass

    # -- decision ------------------------------------------------------------
    def resolver(self, mint, quote, pairs):
        """Return f(leftover_address, rugcheck_deepest_depth) that returns evidence or raises."""
        def resolve(leftover, rc_depth):
            rec = dict(source='geckoterminal', mint=mint, leftover=leftover, quoted=quote.get('pair'),
                       at=self.clock())
            try:
                rec.update(self._decide(mint, quote, pairs, leftover, rc_depth, rec))
                rec['decision'] = 'resolved'
                self._log(rec)
                return rec
            except Exception as e:  # never raise anything but DataError
                reason = str(e) if isinstance(e, (ValueError, DataError)) else 'internal error'
                rec['decision'] = 'reject: ' + reason
                self._log(rec)
                raise DataError('ambiguous unmeasured pool; gecko: ' + reason) from None
        return resolve

    def _decide(self, mint, quote, pairs, leftover, rc_depth, rec):
        mint_id(mint)
        quoted = quote.get('pair')
        for a in (leftover, quoted):
            if not isinstance(a, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', a):
                raise ValueError('invalid pool address')
        if leftover == quoted:
            raise ValueError('leftover is the quoted pool')
        ds = {p.get('pairAddress'): p for p in pairs if isinstance(p, dict)}
        if leftover not in ds or quoted not in ds:
            raise ValueError('pool not in DexScreener pair list')
        ds_left = ds[leftover]
        if (ds_left.get('liquidity') or {}).get('usd') is not None:
            raise ValueError('leftover has a DexScreener liquidity figure')
        ds_quoted_liq = quote.get('liquidity_usd')
        if type(ds_quoted_liq) not in (int, float) or not math.isfinite(ds_quoted_liq) or ds_quoted_liq <= 0:
            raise ValueError('quoted pool depth invalid')
        res, cached = self._fetch([quoted, leftover])
        rec.update(url=res['url'], fetched_at=res['fetched_at'], cached=cached,
                   http_date=res['headers'].get('date'), http_age=res['headers'].get('age'),
                   cache_control=res['headers'].get('cache-control'))
        # Response Date must exist and be close to local time (else possibly a stale replay/cache).
        try:
            hdate = email.utils.parsedate_to_datetime(res['headers']['date']).timestamp()
        except Exception:
            raise ValueError('response Date header missing') from None
        if abs(hdate - res['fetched_at']) > MAX_DATE_SKEW or self.clock() - res['fetched_at'] > CACHE_TTL:
            raise ValueError('stale response')
        rows = (res['data'] or {}).get('data') if isinstance(res['data'], dict) else None
        if not isinstance(rows, list):
            raise ValueError('malformed response')
        def pick(addr):
            found = [r for r in rows if isinstance(r, dict) and (r.get('attributes') or {}).get('address') == addr]
            if len(found) != 1:
                raise ValueError('pool not indexed' if not found else 'duplicate pool')
            r = found[0]
            if r.get('type') != 'pool' or r.get('id') != SOL_NETWORK + '_' + addr:
                raise ValueError('pool identity/network mismatch')
            rel = r.get('relationships') or {}
            tid = lambda k: ((rel.get(k) or {}).get('data') or {}).get('id')
            exp_base = SOL_NETWORK + '_' + ds[addr].get('baseToken', {}).get('address', '')
            exp_quote = SOL_NETWORK + '_' + str(ds[addr].get('quoteToken', {}).get('address', ''))
            if tid('base_token') != SOL_NETWORK + '_' + mint or tid('base_token') != exp_base:
                raise ValueError('base token mismatch')
            if tid('quote_token') != exp_quote:
                raise ValueError('quote token mismatch')
            return r['attributes']
        a_left = pick(leftover)
        a_quoted = pick(quoted)
        rec.update(gt_leftover_raw=a_left.get('reserve_in_usd'), gt_quoted_raw=a_quoted.get('reserve_in_usd'))
        gt_left = parse_reserve(a_left.get('reserve_in_usd'))
        try:
            gt_quoted = parse_reserve(a_quoted.get('reserve_in_usd'))
        except ValueError as e:
            raise ValueError('quoted pool ' + str(e)) from None
        rec.update(gt_leftover_usd=gt_left, gt_quoted_usd=gt_quoted, ds_quoted_usd=ds_quoted_liq,
                   rc_deepest_usd=rc_depth)
        if abs(gt_quoted - ds_quoted_liq) / ds_quoted_liq > CONTRADICTION_TOLERANCE:
            raise ValueError('contradictory sources (quoted pool GT %.0f vs DS %.0f)' % (gt_quoted, ds_quoted_liq))
        bound = min(ds_quoted_liq, gt_quoted, rc_depth)
        if not gt_left < bound:
            raise ValueError('leftover %.2f not strictly below quoted depth %.2f' % (gt_left, bound))
        ev = self.freshness(a_left, res['headers'], res['fetched_at'])
        if not ev:
            raise ValueError('gecko freshness unverified (no upstream data timestamp)')
        rec['freshness_evidence'] = ev
        return dict(leftover_usd=gt_left, bound_usd=bound)
