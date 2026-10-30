"""GeckoTerminal leftover-pool cross-check (default OFF). Offline: HTTP is stubbed."""
import email.utils
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gecko
from gecko import GeckoCrossCheck, parse_reserve
from providers import DataError, MarketData, normalize_pair, normalize_rug
from screening import screen_13

NOW = 1_800_000_000.0
MINT = 'So1aPaperMint1111111111111111111111111111'
QUOTED = 'QuotedPoo1111111111111111111111111111111111'
LEFT = 'LeftoverPoo1111111111111111111111111111111'
SOL = 'So11111111111111111111111111111111111111112'


def ds_pairs(qliq=50_000.0):
    base = dict(chainId='solana', baseToken=dict(address=MINT, symbol='T'), quoteToken=dict(address=SOL),
                priceUsd='0.01', pairCreatedAt=(NOW - 7200) * 1000, txns=dict(h1=dict(buys=30, sells=20)))
    return [dict(base, pairAddress=QUOTED, liquidity=dict(usd=qliq)), dict(base, pairAddress=LEFT)]


def rug(depth=50_000.0, extra=()):
    return dict(mint=MINT, token=dict(mintAuthority=None, freezeAuthority=None), rugged=False, risks=[],
                markets=[dict(pubkey=QUOTED, lp=dict(baseUSD=depth / 2, quoteUSD=depth / 2, lpLockedPct=100))] + list(extra),
                topHolders=[dict(owner='dev', address='a', pct=1.0, insider=False)], knownAccounts={},
                creator='dev', graphInsidersDetected=0, score_normalised=1)


def gt_pool(addr, reserve, base=MINT, quote=SOL, network='solana', id_addr=None):
    return dict(id='%s_%s' % (network, id_addr or addr), type='pool',
                attributes=dict(address=addr, reserve_in_usd=reserve, pool_created_at='2026-10-30T00:00:00Z'),
                relationships=dict(base_token=dict(data=dict(id='solana_' + base)),
                                   quote_token=dict(data=dict(id='solana_' + quote))))


class Stub:
    def __init__(self, rows=None, status=200, body=None, date=NOW, exc=None):
        self.calls, self.rows, self.status, self.body, self.date, self.exc = [], rows, status, body, date, exc

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.exc:
            raise self.exc
        h = {'cache-control': 'max-age=30'}
        if self.date is not None:
            h['date'] = email.utils.formatdate(self.date, usegmt=True)
        body = self.body if self.body is not None else json.dumps(dict(data=self.rows)).encode()
        return self.status, h, body


def fresh(attrs, headers, fetched_at):
    return {'test_only': 'injected freshness evidence'}


def make(stub, freshness=fresh, cap=5, clock=None):
    sleeps = []
    g = GeckoCrossCheck(get=stub, clock=clock or (lambda: NOW), sleep=sleeps.append, monotonic=lambda: 0.0,
                        cap=cap, freshness=freshness)
    g.sleeps = sleeps
    return g


def snap(g, pairs=None, rc=None, mint=MINT):
    pairs = pairs or ds_pairs()
    q = normalize_pair(pairs, mint, NOW)
    res = None if g is None else g.resolver(mint, q, pairs)
    return {**q, **normalize_rug(rc or rug(), mint, q['pair'], NOW, q['unmeasured_pools'], None, resolve_unmeasured=res)}


GOOD = [gt_pool(QUOTED, '50000.1234'), gt_pool(LEFT, '1234.5678')]


class Decisions(unittest.TestCase):
    def reject(self, stub, contains, **kw):
        g = make(stub, **{k: v for k, v in kw.items() if k in ('freshness', 'cap', 'clock')})
        with self.assertRaises(DataError) as c:
            snap(g, kw.get('pairs'), kw.get('rc'))
        self.assertIn('ambiguous unmeasured pool; gecko:', str(c.exception))
        self.assertIn(contains, str(c.exception))
        return g

    def test_default_freshness_rejects_even_with_valid_data(self):
        self.reject(Stub(GOOD), 'freshness unverified', freshness=gecko.no_freshness_evidence)

    def test_valid_strictly_below_resolves_and_proceeds(self):
        s = snap(make(Stub(GOOD)))
        self.assertEqual(s['pair'], QUOTED)
        self.assertEqual(s['liquidity_usd'], 50_000.0)
        self.assertTrue(screen_13(s).checks_passed >= 10 or screen_13(s).eligible_for_grok)

    def test_min_liquidity_still_enforced_after_resolution(self):
        pairs = ds_pairs(20_000.0)
        rows = [gt_pool(QUOTED, '20000.12'), gt_pool(LEFT, '100.50')]
        s = snap(make(Stub(rows)), pairs, rug(20_000.0))
        self.assertEqual(screen_13(s).reason, 'thin_pool')

    def test_identity_pool_address_mismatch(self):
        self.reject(Stub([gt_pool(QUOTED, '50000.12'), gt_pool(LEFT.lower(), '1.50')]), 'pool not indexed')

    def test_identity_id_network_mismatch(self):
        self.reject(Stub([gt_pool(QUOTED, '50000.12'), gt_pool(LEFT, '100.50', network='eth')]), 'identity/network')

    def test_identity_base_token_mismatch(self):
        self.reject(Stub([gt_pool(QUOTED, '50000.12'), gt_pool(LEFT, '100.50', base=SOL)]), 'base token mismatch')

    def test_identity_quote_token_mismatch(self):
        self.reject(Stub([gt_pool(QUOTED, '50000.12'), gt_pool(LEFT, '100.50', quote=MINT)]), 'quote token mismatch')

    def test_pool_not_indexed(self):
        self.reject(Stub([gt_pool(QUOTED, '50000.12')]), 'pool not indexed')

    def test_missing_null_nonnumeric_negative(self):
        for v, want in ((None, 'missing/null'), (1234.5, 'not a decimal string'), ('abc', 'non-numeric'),
                        ('-5.00', 'non-numeric'), ('NaN', 'non-numeric'), ('1e5', 'non-numeric'), ('', 'non-numeric')):
            with self.subTest(v=v):
                self.reject(Stub([gt_pool(QUOTED, '50000.12'), gt_pool(LEFT, v)]), want)
        rows = [gt_pool(QUOTED, '50000.12'), gt_pool(LEFT, '1.50')]
        del rows[1]['attributes']['reserve_in_usd']
        self.reject(Stub(rows), 'missing/null')

    def test_zero_semantics(self):
        for v in ('0', '0.0', '0.00', '0.0000'):
            with self.subTest(v=v):
                self.reject(Stub([gt_pool(QUOTED, '50000.12'), gt_pool(LEFT, v)]), 'zero is not a verified zero')

    def test_rounded_values(self):
        for v, want in (('1234', 'possibly rounded'), ('1234.5', 'possibly rounded'), ('0.04', 'below display precision')):
            with self.subTest(v=v):
                self.reject(Stub([gt_pool(QUOTED, '50000.12'), gt_pool(LEFT, v)]), want)

    def test_stale_or_missing_date(self):
        self.reject(Stub(GOOD, date=NOW - 3600), 'stale response')
        self.reject(Stub(GOOD, date=None), 'Date header missing')

    def test_contradictory_sources(self):
        self.reject(Stub([gt_pool(QUOTED, '20000.12'), gt_pool(LEFT, '100.50')]), 'contradictory sources')

    def test_equal_or_greater_depth(self):
        self.reject(Stub([gt_pool(QUOTED, '50000.00'), gt_pool(LEFT, '50000.00')]), 'not strictly below')
        self.reject(Stub([gt_pool(QUOTED, '50000.00'), gt_pool(LEFT, '70000.00')]), 'not strictly below')
        # bound is min of DS, GT and Rugcheck: leftover below DS but not below Rugcheck depth
        self.reject(Stub([gt_pool(QUOTED, '50000.00'), gt_pool(LEFT, '45000.00')]), 'not strictly below',
                    rc=rug(44_000.0))

    def test_timeout_and_errors(self):
        self.reject(Stub(exc=TimeoutError('timed out')), 'timeout')
        self.reject(Stub(exc=OSError('boom')), 'request failed')
        self.reject(Stub(status=503), 'http 503')
        self.reject(Stub(body=b'{not json'), 'malformed JSON')
        self.reject(Stub(body=b'{"data": {}}'), 'malformed response')

    def test_rate_limit_429_no_sleep_no_retry(self):
        stub = Stub(status=429)
        g = self.reject(stub, 'rate limited (429)')
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(g.sleeps, [])

    def test_unexpected_exception_is_dataerror(self):
        def boom(a, h, t):
            raise KeyError('x')
        self.reject(Stub(GOOD), 'internal error', freshness=boom)

    def test_cache_hit_avoids_second_request(self):
        stub = Stub(GOOD)
        g = make(stub)
        snap(g); snap(g)
        self.assertEqual(len(stub.calls), 1)

    def test_cache_expires(self):
        t = [NOW]
        stub = Stub(GOOD)
        g = make(stub, clock=lambda: t[0])
        snap(g)
        t[0] += gecko.CACHE_TTL + 1
        stub.date = t[0]
        snap(g)
        self.assertEqual(len(stub.calls), 2)

    def test_hard_cap_stops_requests(self):
        stub = Stub(status=503)
        g = make(stub, cap=2)
        for i in range(5):
            g.cache.clear()
            with self.assertRaises(DataError) as c:
                snap(g)
        self.assertEqual(len(stub.calls), 2)
        self.assertIn('gecko request cap reached', str(c.exception))

    def test_cap_absolute_max(self):
        self.assertEqual(gecko.request_cap({'SOLANA_GECKO_MAX_REQUESTS': '999'}), gecko.ABSOLUTE_MAX_CAP)
        self.assertEqual(gecko.request_cap({'SOLANA_GECKO_MAX_REQUESTS': 'x'}), gecko.DEFAULT_CAP)
        self.assertEqual(GeckoCrossCheck(cap=500).cap, gecko.ABSOLUTE_MAX_CAP)

    def test_spacing_between_requests(self):
        stub = Stub(status=503)
        g = make(stub)
        for _ in range(2):
            g.cache.clear()
            with self.assertRaises(DataError):
                snap(g)
        self.assertEqual(g.sleeps, [gecko.MIN_SPACING])

    def test_rugcheck_listed_leftover_not_sent_to_gecko(self):
        # Pool listed by Rugcheck with depth >= deepest: existing rule rejects; gecko never consulted.
        stub = Stub(GOOD)
        extra = [dict(pubkey=LEFT, lp=dict(baseUSD=30_000, quoteUSD=30_000, lpLockedPct=0))]
        with self.assertRaises(DataError) as c:
            snap(make(stub), rc=rug(50_000.0, extra))
        self.assertEqual(stub.calls, [])

    def test_evidence_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'ev.jsonl')
            g = make(Stub(GOOD), freshness=gecko.no_freshness_evidence)
            g.evidence_path = p
            with self.assertRaises(DataError):
                snap(g)
            rec = json.loads(Path(p).read_text().splitlines()[0])
            self.assertEqual(rec['source'], 'geckoterminal')
            self.assertIn('/networks/solana/pools/multi/', rec['url'])
            self.assertIn('freshness unverified', rec['decision'])
            self.assertEqual(rec['gt_leftover_usd'], 1234.5678)


class DefaultOff(unittest.TestCase):
    def test_off_rejects_as_before_without_gecko(self):
        with self.assertRaises(DataError) as c:
            snap(None)
        self.assertEqual(str(c.exception), 'ambiguous unmeasured pool')

    def test_marketdata_default_no_gecko_calls(self):
        urls = []
        pairs = ds_pairs()
        def get(url, **k):
            urls.append(url)
            return pairs if 'dexscreener' in url else rug()
        m = MarketData(get=get, clock=lambda: NOW)
        self.assertIsNone(m.gecko)
        with self.assertRaises(DataError) as c:
            m.snapshot(MINT)
        self.assertEqual(str(c.exception), 'ambiguous unmeasured pool')
        self.assertFalse(any('gecko' in u for u in urls))

    def test_bonding_curve_unchanged(self):
        stub = Stub(GOOD)
        g = make(stub)
        pairs = [ds_pairs()[1]]
        with self.assertRaises(DataError) as c:
            q = normalize_pair(pairs, MINT, NOW)
        self.assertEqual(str(c.exception), 'pool liquidity unavailable')
        self.assertEqual(stub.calls, [])

    def test_enabled_flag(self):
        self.assertFalse(gecko.enabled(False, {}))
        self.assertFalse(gecko.enabled(False, {'SOLANA_GECKO_CROSSCHECK': '1'}))
        self.assertTrue(gecko.enabled(False, {'SOLANA_GECKO_CROSSCHECK': 'yes'}))
        self.assertTrue(gecko.enabled(True, {}))

    def test_run_py_help_lists_flag_default_off(self):
        out = subprocess.run([sys.executable, str(Path(__file__).parent / 'run.py'), '--help'],
                             capture_output=True, text=True, timeout=30).stdout
        self.assertIn('--gecko-crosscheck', out)
        self.assertIn('default OFF', out)


if __name__ == '__main__':
    unittest.main()
