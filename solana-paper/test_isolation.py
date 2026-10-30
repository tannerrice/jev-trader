"""Regression tests for fixture-test ledger isolation (no network, no real ledger).

1. The fixture flow tests pass while a concurrent writer continuously modifies a
   stand-in 'live' ledger (a temp COPY, never data/account.sqlite3), which used to
   fail the old before/after hash with 'real ledger must be untouched'.
2. The guard fails loudly when a test account points at the protected/live path.
"""
import hashlib
import sqlite3
import threading
import unittest
from pathlib import Path

import test_flow_fixture
import testguard
from desk import Store


def live_digest(d):
    h = hashlib.sha256()
    for p in sorted(Path(d).glob('account.sqlite3*')):
        h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = testguard.temp_root('ISOLATION_TEST_')
        self.addCleanup(self.tmp.cleanup)
        self.live = Path(self.tmp.name) / 'standin_live_data'
        self.live.mkdir()
        self.live_db = self.live / 'account.sqlite3'
        Store(str(self.live_db)).close()   # stand-in live ledger (temp copy role)

    def test_fixture_tests_pass_with_concurrent_live_writer(self):
        stop, writes = threading.Event(), [0]
        # Connect before any guard is active (the guard would rightly block it).
        db = sqlite3.connect(str(self.live_db), timeout=30, isolation_level=None, check_same_thread=False)
        def writer():
            while not stop.is_set():
                db.execute("UPDATE account SET state=json_set(state,'$.heartbeat',?) WHERE id=1", (writes[0],))
                writes[0] += 1
            db.close()
        before = live_digest(self.live)
        old = test_flow_fixture.PROTECTED
        test_flow_fixture.PROTECTED = [testguard.REAL_DATA_DIR, self.live]
        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            result = unittest.TestResult()
            for _ in range(3):
                unittest.defaultTestLoader.loadTestsFromTestCase(test_flow_fixture.FlowFixtureTests).run(result)
        finally:
            stop.set(); t.join(10); db.close()
            test_flow_fixture.PROTECTED = old
        self.assertGreater(writes[0], 10, 'writer was not active')
        self.assertNotEqual(live_digest(self.live), before, 'stand-in live ledger was really modified')
        self.assertEqual((result.testsRun, result.failures, result.errors), (6, [], []))

    def test_guard_rejects_account_in_live_dir(self):
        g = testguard.LedgerGuard(self.tmp.name + '_x', protected=[self.live])
        with self.assertRaisesRegex(AssertionError, 'protected'):
            g.check_account(self.live_db)
        with self.assertRaisesRegex(AssertionError, 'not under the test temp dir'):
            g.check_account('/tmp/elsewhere.sqlite3')

    def test_guard_blocks_open_of_live_path_and_fails_isolation(self):
        root = Path(self.tmp.name) / 'case'
        root.mkdir()
        g = testguard.LedgerGuard(root, protected=[self.live])
        with g:
            with self.assertRaises(PermissionError):
                Store(str(self.live_db))
            with self.assertRaises(PermissionError):
                open(self.live_db, 'rb')
        self.assertEqual(len(g.violations), 2)
        probe = unittest.TestCase()
        with self.assertRaises(AssertionError):
            g.assert_isolated(probe, root / 'TEST_account.sqlite3')

    def test_fixture_pointed_at_live_path_fails(self):
        """A fixture test whose injected account is the live path must not run."""
        old = test_flow_fixture.PROTECTED
        test_flow_fixture.PROTECTED = [self.live]
        orig = testguard.LedgerGuard.account_path
        testguard.LedgerGuard.account_path = lambda g, name='x': str(g.check_account(self.live_db))
        try:
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromName('test_FIXTURE_stop_loss_path',
                                                         test_flow_fixture.FlowFixtureTests).run(result)
        finally:
            testguard.LedgerGuard.account_path = orig
            test_flow_fixture.PROTECTED = old
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.errors) + len(result.failures), 1)
        self.assertIn('protected', (result.errors + result.failures)[0][1])

    def test_guard_refuses_overlapping_temp_root(self):
        with self.assertRaises(AssertionError):
            testguard.LedgerGuard(self.live / 'sub', protected=[self.live])
        with self.assertRaises(AssertionError):
            testguard.LedgerGuard(testguard.REAL_DATA_DIR)

    def test_real_ledger_never_opened_by_fixture_flow(self):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_flow_fixture.FlowFixtureTests)
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual((result.testsRun, result.failures, result.errors), (2, [], []))


if __name__ == '__main__':
    unittest.main()
