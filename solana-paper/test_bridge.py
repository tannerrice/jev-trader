import fcntl
import functools
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch
import bridge
import run
from desk import Desk, Store
from grok import BotReviews
from providers import DataError, normalize_pair, normalize_rug
from server import Handler
from test_desk import MINT, NOW, quote, review as _review


def review(**changes):
    return {**_review(), **changes}

FIX = Path(__file__).parent / 'fixtures'
MINT2 = 'A' * 32


def fixture(name):
    return json.loads((FIX / name).read_text())


class Market:
    def __init__(self, snapshots, discover=None):
        self.snapshots, self.calls = snapshots, []
        self._discover = discover if discover is not None else list(snapshots)
    def discover(self):
        self.calls.append('discover'); return list(self._discover)
    def snapshot(self, mint):
        self.calls.append('snapshot:' + mint)
        v = self.snapshots[mint]
        if isinstance(v, Exception): raise v
        return v
    def quote(self, mint, pair=None):
        self.calls.append('quote:' + mint); return self.snapshots[mint]


class AdapterRegressionTests(unittest.TestCase):
    """Built from trimmed live DexScreener/Rugcheck payloads (2026-10-29)."""
    def test_bonding_curve_pool_without_liquidity_does_not_mask_measured_pool(self):
        pairs = fixture('dexscreener_pumpswap_plus_bonding_curve.json')
        mint = pairs[0]['baseToken']['address']
        self.assertNotIn('liquidity', pairs[1])  # pumpfun pool: key absent upstream
        q = normalize_pair(pairs, mint, pairs[0]['pairCreatedAt'] / 1000 + 3600)
        self.assertEqual(q['pair'], pairs[0]['pairAddress'])
        self.assertEqual(q['liquidity_usd'], pairs[0]['liquidity']['usd'])

    def test_bonding_curve_only_is_still_rejected_not_zero(self):
        pairs = fixture('dexscreener_bonding_curve_only.json')
        with self.assertRaises(DataError):
            normalize_pair(pairs, pairs[0]['baseToken']['address'], pairs[0]['pairCreatedAt'] / 1000 + 60)

    def test_exit_quote_for_pool_without_liquidity_still_rejects(self):
        pairs = fixture('dexscreener_pumpswap_plus_bonding_curve.json')
        with self.assertRaises(DataError):
            normalize_pair(pairs, pairs[1]['baseToken']['address'], pairs[1]['pairCreatedAt'] / 1000 + 60, pairs[1]['pairAddress'])

    def test_null_liquidity_usd_ignored_and_none_measured_rejects(self):
        pairs = fixture('dexscreener_pumpswap_plus_bonding_curve.json')
        pairs[1]['liquidity'] = {'usd': None}
        normalize_pair(pairs, pairs[0]['baseToken']['address'], pairs[0]['pairCreatedAt'] / 1000 + 60)
        pairs[0]['liquidity'] = {'usd': None}
        with self.assertRaises(DataError):
            normalize_pair(pairs, pairs[0]['baseToken']['address'], pairs[0]['pairCreatedAt'] / 1000 + 60)

    def test_creator_outside_top_holders_stays_unverified(self):
        r = fixture('rugcheck_creator_not_in_top_holders.json')
        self.assertEqual(r['creatorBalance'], 0)  # not trusted: also 0 when creator is null
        with self.assertRaisesRegex(DataError, 'creator percentage unverified'):
            normalize_rug(r, r['mint'], r['markets'][0]['pubkey'], NOW)
        r['creator'] = None
        with self.assertRaisesRegex(DataError, 'creator percentage unverified'):
            normalize_rug(r, r['mint'], r['markets'][0]['pubkey'], NOW)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.db, self.queue, self.reviews = str(t / 'account.sqlite3'), t / 'queue', t / 'reviews'
        self.reviews.mkdir()
        self.store = Store(self.db)
        self.desk = Desk(self.store)
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()
    def prep(self, market, now=NOW):
        return run.prepare_queue(self.desk, market, self.queue, self.reviews, lambda: now)
    def put_review(self, now=NOW, **changes):
        return bridge.write_review(json.dumps(review(**changes)), self.reviews, self.queue, now)


class QueueTests(Base):
    def test_only_13_check_passers_queued_without_reserving(self):
        bad = quote(mint=MINT2, lp_locked_pct=10)
        m = Market({MINT: quote(), MINT2: bad})
        out = self.prep(m)
        self.assertEqual(out['queued'], [MINT])
        self.assertEqual(out['rejected'], {'lp_unlocked': 1})
        e = json.loads((self.queue / (MINT + '.json')).read_text())
        self.assertEqual((e['pair'], e['checks_passed'], e['queued_at']), ('pool', 13, NOW))
        self.assertEqual(oct((self.queue / (MINT + '.json')).stat().st_mode & 0o777), '0o600')
        s = self.store.read()
        self.assertEqual(s['reviews'], {})
        self.assertFalse(s['days'].get('2027-01-15', {}).get('ai_calls'))

    def test_skips_holding_cooldown_attempted_and_fresh_review(self):
        self.desk.enter(quote(), review(), NOW)
        with self.store.transaction() as s:
            s['cooldowns']['B' * 32] = NOW + 100
            s['reviews']['C' * 32] = {'status': 'reserved'}
        m = Market({k: quote(mint=k) for k in (MINT, 'B' * 32, 'C' * 32)})
        out = self.prep(m)
        self.assertEqual(out['queued'], [])
        self.assertEqual(out['skipped'], {'holding': 1, 'cooldown': 1, 'already_research_attempted': 1})

    def test_stale_and_failing_entries_removed(self):
        self.prep(Market({MINT: quote()}))
        out = self.prep(Market({MINT: quote(insiders=1)}), NOW + 10)
        self.assertIn(MINT, out['removed'])
        self.prep(Market({MINT: quote()}))
        out = self.prep(Market({}, discover=[]), NOW + 901)
        self.assertIn(MINT, out['removed'])
        self.assertEqual(list(self.queue.glob('*.json')), [])

    def test_exits_run_before_discovery_and_when_halted(self):
        self.desk.enter(quote(), review(), NOW)
        with self.store.transaction() as s:
            s['halted'] = True
        m = Market({MINT: quote(price=.5)}, discover=[])
        out = self.prep(m)
        self.assertEqual(m.calls[:2], ['quote:' + MINT, 'discover'])
        self.assertEqual(out['exits_checked'], 1)
        self.assertFalse(self.store.read()['positions'])

    def test_exit_only_never_discovers_and_survives_provider_failure(self):
        self.desk.enter(quote(), review(), NOW)
        class Broken(Market):
            def quote(self, *a): raise DataError('down')
        m = Broken({}, discover=[])
        self.assertEqual(run.exit_monitor(self.desk, m, lambda: NOW + 5), 1)
        s = self.store.read()
        self.assertEqual(s['positions'][MINT]['status'], 'stale_exit_unavailable')
        self.assertEqual(s['heartbeat'], NOW + 5)
        self.assertNotIn('discover', m.calls)


class BridgeTests(Base):
    def setUp(self):
        super().setUp()
        self.prep(Market({MINT: quote()}))

    def test_write_valid_review_atomic_0600(self):
        p = self.put_review()
        self.assertEqual(oct(p.stat().st_mode & 0o777), '0o600')
        self.assertEqual(json.loads(p.read_text())['mint'], MINT)
        self.assertEqual([x.name for x in self.reviews.iterdir()], [MINT + '.json'])

    def test_rejections(self):
        cases = [dict(verdict='BUY', social_ok=False), dict(sources=['https://evil.com/a/status/1']),
                 dict(checked_at=NOW - 901), dict(checked_at=NOW + 5), dict(confidence=1.5), dict(sources=[])]
        for c in cases:
            with self.subTest(c=c), self.assertRaises(DataError):
                self.put_review(**c)
        for raw in ['{"mint": 1, "mint": 2}', json.dumps({**review(), 'extra': 1}), '{"confidence": NaN}', 'x' * 16001]:
            with self.subTest(raw=raw[:30]), self.assertRaises(DataError):
                bridge.write_review(raw, self.reviews, self.queue, NOW)
        with self.assertRaisesRegex(DataError, 'queue'):
            bridge.write_review(json.dumps(review(mint=MINT2)), self.reviews, self.queue, NOW)

    def test_symlink_target_refused(self):
        (self.reviews / (MINT + '.json')).symlink_to(Path(self.tmp.name) / 'elsewhere')
        with self.assertRaises(DataError):
            self.put_review()

    def test_cli_and_prune(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = bridge.main(['write-review', '--review-dir', str(self.reviews), '--queue-dir', str(self.queue)],
                             stdin=io.StringIO(json.dumps(review())), now=NOW)
        self.assertEqual(rc, 0)
        with redirect_stdout(io.StringIO()) as o:
            bridge.main(['list-queue', '--queue-dir', str(self.queue)], now=NOW)
        self.assertEqual(json.loads(o.getvalue())[0]['mint'], MINT)
        with redirect_stdout(io.StringIO()) as o:
            bridge.main(['prune', '--review-dir', str(self.reviews), '--queue-dir', str(self.queue)], now=NOW + 901)
        self.assertEqual(json.loads(o.getvalue())['reviews'], [MINT])


class ConsumeTests(Base):
    def setUp(self):
        super().setUp()
        self.prep(Market({MINT: quote()}))
        self.bot = BotReviews(self.reviews)

    def consume(self, snaps, now=NOW):
        return run.consume(self.desk, Market(snaps, discover=[]), self.bot, self.queue, lambda: now)

    def test_missing_review_does_not_reserve_or_burn(self):
        out = self.consume({MINT: quote()})
        self.assertEqual(out['waiting'], [MINT])
        s = self.store.read()
        self.assertEqual((s['reviews'], s['cash']), ({}, 1000))
        self.assertTrue((self.queue / (MINT + '.json')).exists())

    def test_buy_review_enters_after_recheck(self):
        self.put_review()
        m = Market({MINT: quote()}, discover=[])
        out = run.consume(self.desk, m, self.bot, self.queue, lambda: NOW)
        self.assertEqual(out['entered'], [MINT])
        self.assertEqual(m.calls.count('snapshot:' + MINT), 2)  # before review and before entry
        self.assertEqual(self.store.read()['cash'], 975)
        self.assertFalse((self.queue / (MINT + '.json')).exists())

    def test_skip_low_conf_and_failed_recheck_never_enter(self):
        for changes, snap in [(dict(verdict='SKIP', social_ok=False), quote()), (dict(confidence=.5), quote()),
                              ({}, quote(rugged=True))]:
            with self.subTest(changes=changes):
                self.tearDown(); self.setUp()
                self.put_review(**changes)
                out = self.consume({MINT: snap})
                self.assertEqual(out['entered'], [])
                self.assertEqual(self.store.read()['cash'], 1000)

    def test_invalid_review_file_skips_and_counts_attempt(self):
        (self.reviews / (MINT + '.json')).write_text('{"mint": "%s"}' % MINT)
        out = self.consume({MINT: quote()})
        self.assertIn('invalid_review_skip', out['skipped'][MINT])
        self.assertEqual(self.store.read()['cash'], 1000)
        self.assertIn(MINT, self.store.read()['reviews'])

    def test_stale_review_at_consume_time_skips(self):
        self.put_review()
        out = self.consume({MINT: quote(observed_at=NOW + 901, rug_observed_at=NOW + 901)}, NOW + 901)
        self.assertEqual(out['entered'], [])

    def test_legacy_cycle_does_not_burn_without_file(self):
        run.cycle(self.desk, Market({MINT: quote()}), self.bot, lambda: NOW)
        self.assertEqual(self.store.read()['reviews'], {})


class ConcurrencyTests(Base):
    def test_second_invocation_skips_when_locked(self):
        with open(self.db + '.worker.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for flag in ('--exit-only', '--prepare-queue', '--consume'):
                out = io.StringIO()
                with redirect_stdout(out), patch.object(run, 'MarketData', side_effect=AssertionError('must not run')):
                    run.main([flag, '--db', self.db, '--review-dir', str(self.reviews)])
                self.assertEqual(json.loads(out.getvalue()), {'status': 'skipped: locked'})
            with self.assertRaises(SystemExit):
                run.main(['--once', '--db', self.db])

    def test_same_mint_cannot_be_entered_twice_across_connections(self):
        results, barrier = [], threading.Barrier(2)
        def go():
            st = Store(self.db)
            try:
                barrier.wait(); results.append(Desk(st).enter(quote(), review(), NOW))
            finally:
                st.close()
        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(results), [False, True])
        s = self.store.read()
        self.assertEqual((s['cash'], len(s['trades'])), (975, 1))

    def test_reservation_is_once_per_mint(self):
        other = Desk(Store(self.db))
        try:
            self.assertTrue(self.desk.reserve_review(MINT, NOW, True))
            self.assertFalse(other.reserve_review(MINT, NOW, True))
        finally:
            other.store.close()


class DashboardAuthTests(unittest.TestCase):
    TOKEN = 'synthetic-test-token_abc'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = HTTPServer(('127.0.0.1', 0), functools.partial(Handler, db=self.tmp.name + '/a.sqlite3', token=self.TOKEN))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def code(self, path, headers=None, method='GET'):
        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        try:
            r = urllib.request.build_opener(NoRedir).open(urllib.request.Request(self.url + path, headers=headers or {}, method=method))
            return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers

    def test_requires_token(self):
        self.assertEqual(self.code('/api/report')[0], 401)
        self.assertEqual(self.code('/')[0], 401)
        self.assertEqual(self.code('/api/report', {'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.code('/api/report', {'Cookie': 'paper_dash=wrong'})[0], 401)
        self.assertEqual(self.code('/login?token=wrong')[0], 401)

    def test_bearer_cookie_login_and_still_read_only(self):
        self.assertEqual(self.code('/api/report', {'Authorization': 'Bearer ' + self.TOKEN})[0], 200)
        status, h = self.code('/login?token=' + self.TOKEN)
        self.assertEqual((status, h['Location']), (303, '/'))
        cookie = h['Set-Cookie']
        self.assertIn('HttpOnly', cookie); self.assertIn('SameSite=Strict', cookie)
        self.assertEqual(self.code('/', {'Cookie': cookie.split(';')[0]})[0], 200)
        self.assertEqual(self.code('/api/report', {'Authorization': 'Bearer ' + self.TOKEN}, 'POST')[0], 501)

    def test_env_token_used_and_unset_means_open_local(self):
        self.tearDown()
        with patch.dict(os.environ, {'SOLANA_DASH_TOKEN': 'env-token'}):
            self.tmp = tempfile.TemporaryDirectory()
            self.server = HTTPServer(('127.0.0.1', 0), functools.partial(Handler, db=self.tmp.name + '/a.sqlite3'))
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
            self.url = 'http://127.0.0.1:%d' % self.server.server_port
            self.assertEqual(self.code('/api/report')[0], 401)
            self.assertEqual(self.code('/api/report', {'Authorization': 'Bearer env-token'})[0], 200)


if __name__ == '__main__':
    unittest.main()
