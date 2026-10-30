"""Regression tests for supervisor singleton lock and inline-check failure handling.
Uses temp OPS_DIR/DESK_DIR with stub run.py and stub exit_loop.sh; never touches the real ledger or live loop."""
import json, os, signal, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / 'ops'))
import supervisor

SUP = str(Path(__file__).parent / 'ops' / 'supervisor.py')
STUB_RUN = 'import json,sys\nprint(json.dumps({"status":"ok","age_seconds":1}))\n'
STUB_LOOP = '#!/bin/bash\necho $$ > "$OPS_DIR/loop.pid"\nwhile true; do date +%s > "$OPS_DIR/loop.alive"; sleep 1; done\n'


def loops_in(ops):
    out = []
    for p in Path('/proc').iterdir():
        if p.name.isdigit():
            try:
                env = (p / 'environ').read_bytes()
                cmd = (p / 'cmdline').read_bytes()
            except OSError:
                continue
            if b'exit_loop.sh' in cmd and ('OPS_DIR=%s' % ops).encode() in env:
                out.append(int(p.name))
    return out


class Env:
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.ops, self.desk, stub = root / 'ops', root / 'desk', root / 'stub'
        for d in (self.ops, self.desk, stub):
            d.mkdir()
        (self.desk / 'run.py').write_text(STUB_RUN)
        self.loop = stub / 'exit_loop.sh'
        self.loop.write_text(STUB_LOOP); self.loop.chmod(0o755)
        return self

    def __exit__(self, *a):
        for pid in loops_in(self.ops):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        self.td.cleanup()


class SingletonTests(unittest.TestCase):
    def test_concurrent_supervisors_launch_exactly_one_loop(self):
        with Env() as e:
            env = dict(os.environ, OPS_DIR=str(e.ops), DESK_DIR=str(e.desk), EXIT_LOOP_SCRIPT=str(e.loop),
                       SUPERVISOR_LOCK_WAIT='60')
            procs = [subprocess.Popen([sys.executable, SUP], env=env, stdout=subprocess.PIPE, text=True) for _ in range(6)]
            outs = [p.communicate(timeout=120)[0] for p in procs]
            self.assertTrue(all(p.returncode == 0 for p in procs))
            recs = [json.loads(o.strip().splitlines()[-1]) for o in outs]
            self.assertEqual(sum(r['action'] == 'restart' for r in recs), 1, recs)
            self.assertEqual(sum(r['action'] == 'ok' for r in recs), 5, recs)
            time.sleep(1)
            loops = loops_in(e.ops)
            self.assertEqual(len(loops), 1, loops)
            self.assertEqual(int((e.ops / 'loop.pid').read_text()), loops[0])
            lines = (e.ops / 'supervisor_decisions.log').read_text().splitlines()
            self.assertEqual(len(lines), 6)

    def test_busy_lock_skips_and_logs(self):
        import fcntl
        with Env() as e:
            f = open(e.ops / 'supervisor.lock', 'a+'); fcntl.flock(f, fcntl.LOCK_EX)
            try:
                with mock.patch.object(supervisor, 'SUPERVISOR_LOCK_WAIT', 0.5):
                    rec = supervisor.main(str(e.ops), str(e.desk))
            finally:
                f.close()
            self.assertEqual(rec['action'], 'skipped')
            self.assertIn('another supervisor active', (e.ops / 'supervisor_decisions.log').read_text())
            self.assertIn('ALERT', (e.ops / 'ensure.log').read_text())
            self.assertEqual(loops_in(e.ops), [])

    def test_lock_released_when_holder_dies(self):
        with Env() as e:
            holder = subprocess.Popen([sys.executable, '-c',
                'import fcntl,time,sys; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX); print("x",flush=True); time.sleep(60)',
                str(e.ops / 'supervisor.lock')], stdout=subprocess.PIPE)
            holder.stdout.readline(); holder.kill(); holder.wait()
            f, _ = supervisor.acquire(e.ops, 2)
            self.assertIsNotNone(f); f.close()


class InlineCheckFailureTests(unittest.TestCase):
    def run_with(self, side_effect):
        with Env() as e:
            (e.desk / 'run.py').write_text('import json\nprint(json.dumps({"status":"stale"}))\n')
            (e.ops / 'loop.pid').write_text(str(os.getpid()))
            (e.ops / 'loop.alive').write_text(str(int(time.time())))
            real = subprocess.run
            def fake(cmd, *a, **k):
                if '--exit-only' in cmd:
                    return side_effect(cmd, *a, **k)
                return real(cmd, *a, **k)
            with mock.patch.object(supervisor, 'loop_cmd_ok', return_value=True), \
                 mock.patch.object(supervisor.subprocess, 'run', side_effect=fake):
                rec = supervisor.main(str(e.ops), str(e.desk))
            dec = [json.loads(l) for l in (e.ops / 'supervisor_decisions.log').read_text().splitlines()]
            alerts = (e.ops / 'ensure.log').read_text()
            return rec, dec, alerts

    def test_timeout_recorded(self):
        def se(cmd, *a, **k):
            raise subprocess.TimeoutExpired(cmd, 60, stderr=b'waiting for lock')
        rec, dec, alerts = self.run_with(se)
        self.assertEqual(rec['action'], 'inline_check_timeout')
        self.assertEqual(dec[-1]['action'], 'inline_check_timeout')
        self.assertIn('waiting for lock', dec[-1]['inline_check']['err'])
        self.assertIn('ALERT inline_check_timeout', alerts)

    def test_oserror_recorded(self):
        def se(cmd, *a, **k):
            raise OSError('exec failed')
        rec, dec, alerts = self.run_with(se)
        self.assertEqual(dec[-1]['action'], 'inline_check_failed')
        self.assertIn('exec failed', dec[-1]['inline_check']['error'])
        self.assertIn('ALERT inline_check_failed', alerts)

    def test_nonzero_exit_recorded(self):
        def se(cmd, *a, **k):
            return subprocess.CompletedProcess(cmd, 3, stdout='', stderr='boom')
        rec, dec, alerts = self.run_with(se)
        self.assertEqual(dec[-1]['action'], 'inline_check_failed')
        self.assertEqual(dec[-1]['inline_check']['rc'], 3)
        self.assertIn('ALERT inline_check_failed', alerts)

    def test_unparsable_output_recorded(self):
        def se(cmd, *a, **k):
            return subprocess.CompletedProcess(cmd, 0, stdout='not json', stderr='')
        rec, dec, alerts = self.run_with(se)
        self.assertEqual(dec[-1]['action'], 'inline_check_failed')
        self.assertIn('unparsable', dec[-1]['inline_check']['error'])

    def test_successful_inline_check_still_alerts(self):
        def se(cmd, *a, **k):
            return subprocess.CompletedProcess(cmd, 0, stdout='{"status":"ok"}\n', stderr='')
        rec, dec, alerts = self.run_with(se)
        self.assertEqual(dec[-1]['action'], 'inline_check')
        self.assertIn('ALERT inline_check', alerts)


if __name__ == '__main__':
    unittest.main()
