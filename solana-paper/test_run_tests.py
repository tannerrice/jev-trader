"""run_tests.py records exit status, counts, failing names and tracebacks (offline)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import run_tests

SAMPLE = """test_a (m.T.test_a) ... ok
test_b (m.T.test_b) ... FAIL

======================================================================
FAIL: test_b (m.T.test_b)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "m.py", line 9, in test_b
AssertionError: real ledger must be untouched

----------------------------------------------------------------------
Ran 2 tests in 0.010s

FAILED (failures=1)
"""


class RunTestsTests(unittest.TestCase):
    def test_parse_failures(self):
        counts, failing = run_tests.parse(SAMPLE)
        self.assertEqual(counts, dict(ran=2, status='FAILED', failures=1))
        self.assertEqual(failing[0]['test'], 'test_b (m.T.test_b)')
        self.assertIn('AssertionError: real ledger must be untouched', failing[0]['traceback'])

    def test_end_to_end_failing_run_is_saved(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'src'; src.mkdir(); logs = Path(d) / 'logs'
            (src / 'test_x.py').write_text('import unittest\nclass X(unittest.TestCase):\n'
                                           '    def test_ok(self): pass\n'
                                           '    def test_bad(self): self.assertEqual(1, 2, "boom")\n')
            runner = src / 'run_tests.py'
            runner.write_text(Path(run_tests.__file__).read_text())
            p = subprocess.run([sys.executable, str(runner), '--logs-dir', str(logs)],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 1)
            self.assertIn('FAIL: test_bad', p.stdout)          # output not hidden
            s = json.loads((logs / 'latest-summary.json').read_text())
            self.assertEqual((s['exit_code'], s['ran'], s['failures']), (1, 2, 1))
            self.assertIn('boom', s['failing'][0]['traceback'])
            self.assertIn('test_bad', (logs / 'latest-summary.txt').read_text())
            self.assertIn('FAIL: test_bad', Path(s['output']).read_text())


if __name__ == '__main__':
    unittest.main()
