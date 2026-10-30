"""FIXTURE/TEST end-to-end flow on a temporary TEST account.

queue -> bridge write-review -> consume -> simulated entry -> exits.
All market data below is synthetic FIXTURE data; nothing touches the network.
Isolation: each test gets an explicitly injected temp account and runs inside a
testguard.LedgerGuard that raises on any open of a protected ledger dir (the real
data/ dir by default) and proves afterwards that only the temp account was used.
(The old before/after hash of the live ledger failed whenever the live exit loop
wrote to it; see test_isolation.py.)
"""
import json
import unittest
from pathlib import Path
import bridge
import run
from desk import Desk, Store
from grok import BotReviews
from test_screening import CLEAN
import testguard

FIXTURE_MINT = 'FixtureTestMint1111111111111111111111111111'[:43]
T0 = 1_800_000_000

# Protected dirs for the guard; test_isolation.py swaps in a stand-in "live" copy.
PROTECTED = [testguard.REAL_DATA_DIR]


def fixture_quote(now, price=1.0, **changes):
    return {**CLEAN, 'mint': FIXTURE_MINT, 'symbol': 'FIXTURE', 'pair': 'FIXTURE_POOL', 'price': price,
            'observed_at': now, 'rug_observed_at': now, 'unmeasured_pools': [], **changes}


class FixtureMarket:
    """Serves a FIXTURE price sequence; one step per exit check."""
    def __init__(self, clock, prices):
        self.clock, self.prices = clock, list(prices)
    def discover(self):
        return [FIXTURE_MINT]
    def snapshot(self, mint):
        return fixture_quote(self.clock())
    def quote(self, mint, pair=None):
        return fixture_quote(self.clock(), self.prices.pop(0) if self.prices else 1.0)


class FlowFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = testguard.temp_root()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.guard = testguard.LedgerGuard(t, protected=PROTECTED)
        self.db = self.guard.account_path('TEST_account.sqlite3')   # explicit injection; never a default
        self.queue, self.reviews = t / 'TEST_queue', t / 'TEST_reviews'
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        self.store = Store(self.db); self.desk = Desk(self.store)
        self.now = T0

    def tearDown(self):
        written = self.store.read()
        self.store.close()
        self.guard.__exit__(None, None, None)
        # Safety assertions: only the injected temp account was used, and it was the one written.
        self.guard.assert_isolated(self, self.db)
        self.assertTrue(written['trades'], 'temp account was not the account written')
        self.assertTrue(all(testguard.inside(p, self.tmp.name) for p in (self.queue, self.reviews)))

    def clock(self):
        return self.now

    def enter(self, prices):
        market = FixtureMarket(self.clock, prices)
        out = run.prepare_queue(self.desk, market, self.queue, self.reviews, self.clock)
        self.assertEqual(out['queued'], [FIXTURE_MINT])
        self.now += 60
        review = dict(mint=FIXTURE_MINT, verdict='BUY', confidence=0.9, social_ok=True, checked_at=self.now,
                      reason='FIXTURE/TEST review - not real research', sources=['https://x.com/fixture/status/1'])
        bridge.write_review(json.dumps(review), self.reviews, self.queue, self.now)
        self.now += 30
        out = run.consume(self.desk, market, BotReviews(self.reviews), self.queue, self.clock)
        self.assertEqual(out['entered'], [FIXTURE_MINT])
        s = self.store.read()
        self.assertEqual((s['cash'], len(s['positions'])), (975.0, 1))
        return market

    def step(self, market, seconds=60):
        self.now += seconds
        run.exit_monitor(self.desk, market, self.clock)
        return self.store.read()

    def test_FIXTURE_stop_loss_path(self):
        market = self.enter([0.9, 0.6])
        s = self.step(market)
        self.assertIn(FIXTURE_MINT, s['positions'])
        s = self.step(market)
        self.assertNotIn(FIXTURE_MINT, s['positions'])
        self.assertEqual(s['trades'][-1]['reason'], 'stop_loss')
        self.assertLess(s['cash'], 1000)
        self.assertGreater(s['cooldowns'][FIXTURE_MINT], self.now)
        self.assertEqual(s['monitor']['missed_count'], 0)
        # Re-queue is impossible: holding gone, but cooldown + one research attempt.
        out = run.prepare_queue(self.desk, market, self.queue, self.reviews, self.clock)
        self.assertEqual(out['queued'], [])

    def test_FIXTURE_take_profit_and_trailing_path(self):
        market = self.enter([2.2, 3.3, 2.0])
        s = self.step(market)
        self.assertEqual(s['trades'][-1]['reason'], 'take_profit_1')
        s = self.step(market)
        self.assertEqual(s['trades'][-1]['reason'], 'take_profit_2')
        s = self.step(market)
        self.assertEqual(s['trades'][-1]['reason'], 'trailing_stop')
        self.assertNotIn(FIXTURE_MINT, s['positions'])
        self.assertGreater(s['cash'], 1000)
        self.assertEqual([t['side'] for t in s['trades']], ['BUY', 'SELL', 'SELL', 'SELL'])


if __name__ == '__main__':
    unittest.main()
