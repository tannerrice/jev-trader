"""RPC creator holdings (A), unmeasured-pool ambiguity (B), exit-monitor log (D). Offline FIXTURE tests."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import run
from desk import Desk, Store
from providers import (DataError, MarketData, normalize_pair, normalize_rug, rpc_creator_pct,
                       rpc_call, TOKEN_PROGRAM, TOKEN_2022_PROGRAM)

FIX = Path(__file__).parent / 'fixtures'
RPC = json.loads((FIX / 'rpc_creator_holdings_FIXTURE.json').read_text())


def fake_rpc(case, **override):
    c = copy.deepcopy(RPC[case])
    c.update(override)
    def call(method, params):
        v = c[method]
        if isinstance(v, Exception):
            raise v
        return v
    return c, call


class RpcCreatorTests(unittest.TestCase):
    def test_token2022_account_summed_against_supply(self):
        c, call = fake_rpc('token2022_with_account')
        self.assertEqual(c['getAccountInfo']['result']['value']['owner'], TOKEN_2022_PROGRAM)
        self.assertAlmostEqual(rpc_creator_pct(c['mint'], c['creator'], call), 4439455904405 * 100 / 10**15)

    def test_spl_token_zero_balance_and_empty_list_are_verified_zero(self):
        c, call = fake_rpc('spl_token_zero_balance_account')
        self.assertEqual(c['getAccountInfo']['result']['value']['owner'], TOKEN_PROGRAM)
        self.assertEqual(rpc_creator_pct(c['mint'], c['creator'], call), 0)
        c, call = fake_rpc('token2022_no_accounts')
        self.assertEqual(c['getTokenAccountsByOwner']['result']['value'], [])
        self.assertEqual(rpc_creator_pct(c['mint'], c['creator'], call), 0)

    def test_multiple_accounts_summed(self):
        c, call = fake_rpc('token2022_with_account')
        acc = c['getTokenAccountsByOwner']['result']['value']
        second = copy.deepcopy(acc[0]); second['pubkey'] = 'B' * 43
        acc.append(second)
        self.assertAlmostEqual(rpc_creator_pct(c['mint'], c['creator'], call), 2 * 4439455904405 * 100 / 10**15)
        acc.append(copy.deepcopy(acc[0]))  # duplicate pubkey
        with self.assertRaises(DataError):
            rpc_creator_pct(c['mint'], c['creator'], call)

    def mutate_fail(self, mutate, case='token2022_with_account'):
        c, call = fake_rpc(case)
        mutate(c)
        with self.assertRaises(DataError):
            rpc_creator_pct(c['mint'], c['creator'], call)

    def test_fail_closed_cases(self):
        info = lambda c: c['getAccountInfo']['result']['value']['data']['parsed']['info']
        acct0 = lambda c: c['getTokenAccountsByOwner']['result']['value'][0]['account']['data']['parsed']['info']
        cases = {
            'zero_supply': lambda c: (info(c).update(supply='0'), c['getTokenSupply']['result']['value'].update(amount='0')),
            'supply_mismatch': lambda c: c['getTokenSupply']['result']['value'].update(amount='1'),
            'decimals_mismatch_supply': lambda c: c['getTokenSupply']['result']['value'].update(decimals=9),
            'decimals_mismatch_account': lambda c: acct0(c)['tokenAmount'].update(decimals=9),
            'wrong_owner_program': lambda c: c['getAccountInfo']['result']['value'].update(owner='1' * 32),
            'account_other_mint': lambda c: acct0(c).update(mint='A' * 32),
            'account_other_owner': lambda c: acct0(c).update(owner='A' * 32),
            'float_amount': lambda c: acct0(c)['tokenAmount'].update(amount=1.5),
            'confidential': lambda c: info(c)['extensions'].append({'extension': 'confidentialTransferMint'}),
            'rpc_error': lambda c: c.update(getTokenAccountsByOwner=RPC['rpc_error_example']),
            'timeout': lambda c: c.update(getTokenSupply=DataError('provider request failed')),
            'slot_spread': lambda c: c['getTokenAccountsByOwner']['result']['context'].update(slot=10**12),
            'null_mint_account': lambda c: c['getAccountInfo']['result'].update(value=None),
            'accounts_not_list': lambda c: c['getTokenAccountsByOwner']['result'].update(value=None),
        }
        for name, m in cases.items():
            with self.subTest(name):
                self.mutate_fail(m)

    def test_creator_must_be_explicit(self):
        c, call = fake_rpc('token2022_with_account')
        for creator in (None, '', 'not-a-key'):
            with self.assertRaises(DataError):
                rpc_creator_pct(c['mint'], creator, call)

    def test_rpc_call_allows_read_methods_only(self):
        call = rpc_call(post=lambda *a, **k: self.fail('must not post'))
        with self.assertRaises(DataError):
            call('sendTransaction', [])

    def test_default_off_and_feeds_dev_pct_only_when_enabled(self):
        from test_desk import rug, MINT, NOW
        d = rug(); d['topHolders'] = [h for h in d['topHolders'] if h['owner'] != 'dev']
        d['creator'] = RPC['token2022_with_account']['creator']
        with self.assertRaisesRegex(DataError, 'creator percentage unverified'):
            normalize_rug(d, MINT, 'pool', NOW)
        self.assertIsNone(MarketData().rpc)
        r = normalize_rug(d, MINT, 'pool', NOW, creator_pct=lambda m, c: 0.44)
        self.assertEqual((r['dev_pct'], r['dev_pct_source']), (0.44, 'rpc_creator_holdings'))
        d['creator'] = None
        with self.assertRaises(DataError):
            normalize_rug(d, MINT, 'pool', NOW, creator_pct=lambda m, c: 0.0)
        d['creator'] = RPC['token2022_with_account']['creator']
        def boom(m, c): raise DataError('RPC error')
        with self.assertRaises(DataError):
            normalize_rug(d, MINT, 'pool', NOW, creator_pct=boom)


class PoolAmbiguityTests(unittest.TestCase):
    def setUp(self):
        self.f = json.loads((FIX / 'pool_rugcheck_deepest_unmeasured_FIXTURE.json').read_text())
        self.mint = self.f['rug']['mint']
        self.now = max(p['pairCreatedAt'] for p in self.f['pairs']) / 1000 + 3600

    def screen(self, pairs, rug):
        q = normalize_pair(pairs, self.mint, self.now)
        # Synthetic creator source so the pool rule is what is under test.
        return normalize_rug(rug, self.mint, q['pair'], self.now, q['unmeasured_pools'], lambda m, c: 1.0)

    def test_rugcheck_deepest_is_unmeasured_pool_rejects(self):
        with self.assertRaises(DataError):
            self.screen(self.f['pairs'], self.f['rug'])

    def test_unmeasured_absent_from_rugcheck_rejects_as_ambiguous(self):
        rug = copy.deepcopy(self.f['rug'])
        rug['markets'] = [m for m in rug['markets'] if m['marketType'] != 'pump_fun']
        with self.assertRaisesRegex(DataError, 'ambiguous unmeasured pool'):
            self.screen(self.f['pairs'], rug)

    def test_measured_small_vs_unmeasured_equal_depth_rejects(self):
        rug = copy.deepcopy(self.f['rug'])
        meas = next(m for m in rug['markets'] if m['marketType'] != 'pump_fun')
        curve = next(m for m in rug['markets'] if m['marketType'] == 'pump_fun')
        curve['lp'].update(baseUSD=meas['lp']['baseUSD'], quoteUSD=meas['lp']['quoteUSD'])
        with self.assertRaises(DataError):
            self.screen(self.f['pairs'], rug)

    def test_allowed_only_when_rugcheck_shows_unmeasured_pool_shallower(self):
        # FIXTURE modification: curve depth set below the measured pool in Rugcheck.
        rug = copy.deepcopy(self.f['rug'])
        curve = next(m for m in rug['markets'] if m['marketType'] == 'pump_fun')
        curve['lp'].update(baseUSD=1.0, quoteUSD=1.0)
        r = self.screen(self.f['pairs'], rug)
        meas = next(m for m in rug['markets'] if m['marketType'] != 'pump_fun')
        self.assertEqual(r['lp_locked_pct'], meas['lp']['lpLockedPct'])  # LP lock of the quoted pool

    def test_all_unmeasured_rejects(self):
        pairs = copy.deepcopy(self.f['pairs'])
        for p in pairs: p.pop('liquidity', None)
        with self.assertRaises(DataError):
            normalize_pair(pairs, self.mint, self.now)

    def test_duplicate_rugcheck_market_rejects(self):
        rug = copy.deepcopy(self.f['rug'])
        curve = next(m for m in rug['markets'] if m['marketType'] == 'pump_fun')
        curve['lp'].update(baseUSD=1.0, quoteUSD=1.0)
        rug['markets'].append(copy.deepcopy(curve))
        with self.assertRaises(DataError):
            self.screen(self.f['pairs'], rug)


class MonitorLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = self.tmp.name + '/TEST_account.sqlite3'
        self.store = Store(self.db); self.desk = Desk(self.store)
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_gaps_over_90s_recorded_and_capped(self):
        class M:
            def quote(self, *a): raise AssertionError
        t = 1_800_000_000
        for dt in (0, 60, 120, 400, 460):
            run.exit_monitor(self.desk, M(), lambda: t + dt)
        m = self.store.read()['monitor']
        self.assertEqual(m['missed_count'], 1)
        self.assertEqual(m['missed'][0], dict(start=t + 120, end=t + 400, gap_seconds=280))
        st = run.monitor_status(self.store.read(), t + 470)
        self.assertEqual((st['status'], st['age_seconds']), ('ok', 10))
        self.assertTrue(st['last_success_at_et'].endswith(('-04:00', '-05:00')))
        self.assertEqual(run.monitor_status(self.store.read(), t + 581)['status'], 'stale')
        for i in range(250):
            run.record_monitor(self.desk, t + 1000 + i * 100)
        m = self.store.read()['monitor']
        self.assertEqual((len(m['missed']), m['missed_count']), (200, 251))
        r = self.desk.report(t + 30000)
        self.assertEqual(r['monitor_missed_count'], 251)
        self.assertEqual(r['monitor_last_success'], m['last_success_at'])

    def test_skipped_locked_recorded_and_status_cli(self):
        import fcntl
        with open(self.db + '.worker.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with redirect_stdout(io.StringIO()):
                run.main(['--exit-only', '--db', self.db])
        self.assertIsNotNone(self.store.read()['monitor']['last_skipped_locked_at'])
        with redirect_stdout(io.StringIO()) as out:
            run.main(['--monitor-status', '--db', self.db])
        self.assertEqual(json.loads(out.getvalue())['status'], 'stale')


if __name__ == '__main__':
    unittest.main()
