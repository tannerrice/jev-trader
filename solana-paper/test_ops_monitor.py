"""Regression tests for the exit-monitor restart investigation (Oct 30 2026).

Confirmed cause: the box is periodically not scheduled (bash `sleep 60` overran by up to ~520s
with the loop alive and exit=0). On wake the old supervisor saw monitor status 'stale' and
killed a healthy loop. These tests pin the new rules. Temp accounts only.
"""
import contextlib, fcntl, io, json, os, sys, tempfile, threading, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent/'ops'))
import supervisor
import run
from desk import Desk, Store


class DecideTests(unittest.TestCase):
    def test_stale_monitor_with_live_loop_never_restarts(self):
        for age in (0, 5, 60, supervisor.HUNG_AFTER):
            action, _ = supervisor.decide(True, age, 'stale')
            self.assertEqual(action, 'inline_check')

    def test_dead_loop_restarts(self):
        self.assertEqual(supervisor.decide(False, 3, 'ok')[0], 'restart')

    def test_hung_loop_restarts(self):
        self.assertEqual(supervisor.decide(True, supervisor.HUNG_AFTER + 1, 'ok')[0], 'restart')
        self.assertEqual(supervisor.decide(True, None, 'ok')[0], 'restart')

    def test_healthy(self):
        self.assertEqual(supervisor.decide(True, 4, 'ok')[0], 'ok')

    def test_hung_threshold_exceeds_longest_legit_lock_wait(self):
        # loop lock wait (30s) + run timeout (50s) + slice (5s) must stay below HUNG_AFTER
        self.assertGreater(supervisor.HUNG_AFTER, 30 + 50 + 5)

    def test_pid_check_rejects_unrelated_process(self):
        self.assertFalse(supervisor.loop_cmd_ok(os.getpid()))
        self.assertFalse(supervisor.loop_cmd_ok(2**22 + 7))


class LockDelayTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, 'test-account.sqlite3')

    def tearDown(self):
        self.dir.cleanup()

    def test_record_monitor_lock_delay_kept_separate_and_gap_still_missed(self):
        store = Store(self.db); desk = Desk(store)
        run.record_monitor(desk, 1000.0)
        run.record_monitor(desk, 1200.0, lock_wait=12.3)
        st = run.monitor_status(store.read(), 1201.0); store.close()
        self.assertEqual(st['missed_count'], 1)          # gap is not suppressed
        self.assertEqual(st['lock_delayed_count'], 1)
        self.assertEqual(st['lock_delayed'][0]['wait_seconds'], 12.3)

    def test_short_wait_not_recorded_as_delay(self):
        store = Store(self.db); desk = Desk(store)
        run.record_monitor(desk, 1000.0, lock_wait=0.2)
        self.assertEqual(run.monitor_status(store.read(), 1001.0)['lock_delayed_count'], 0); store.close()

    def test_exit_only_waits_for_lock_instead_of_skipping(self):
        Store(self.db).close()
        lock = open(self.db + '.worker.lock', 'w'); fcntl.flock(lock, fcntl.LOCK_EX)
        threading.Timer(1.5, lambda: (fcntl.flock(lock, fcntl.LOCK_UN), lock.close())).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run.main(['--exit-only', '--lock-wait', '10', '--db', self.db])
        res = json.loads(out.getvalue())
        self.assertEqual(res['mode'], 'exit-only')
        self.assertGreaterEqual(res['lock_wait_seconds'], 1.0)
        store = Store(self.db)
        self.assertEqual(run.monitor_status(store.read(), time.time())['lock_delayed_count'], 1); store.close()

    def test_exit_only_without_wait_still_skips_and_records(self):
        Store(self.db).close()
        with open(self.db + '.worker.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run.main(['--exit-only', '--db', self.db])
        self.assertEqual(json.loads(out.getvalue())['status'], 'skipped: locked')
        store = Store(self.db)
        self.assertIsNotNone(store.read()['monitor']['last_skipped_locked_at']); store.close()


if __name__ == '__main__':
    unittest.main()
