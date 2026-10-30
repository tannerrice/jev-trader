"""Test-only ledger isolation guard (imported by tests; not a test module itself).

Fixture tests must use an explicitly injected temporary account. While a
LedgerGuard is active every sqlite3.connect / open / io.open / os.open call is
recorded; any path that resolves inside a protected directory (by default the
real data/ dir holding data/account.sqlite3) raises PermissionError immediately
and is recorded as a violation. This replaces hashing the live ledger, which a
concurrently running exit loop legitimately changes.
"""
import builtins
import io
import os
import sqlite3
import tempfile
from pathlib import Path

REAL_DATA_DIR = (Path(__file__).parent / 'data').resolve()
REAL_DB = REAL_DATA_DIR / 'account.sqlite3'


def _resolve(p):
    if isinstance(p, int):
        return None  # already-open file descriptor
    s = os.fsdecode(p)
    if s.startswith('file:'):
        s = s[5:].split('?', 1)[0]
    if s in ('', ':memory:'):
        return None
    return Path(s).expanduser().resolve()


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


class LedgerGuard:
    def __init__(self, temp_root, protected=(REAL_DATA_DIR,)):
        self.temp_root = Path(temp_root).resolve()
        self.protected = [Path(p).resolve() for p in protected]
        for p in self.protected:
            if inside(self.temp_root, p) or inside(p, self.temp_root):
                raise AssertionError('temp root overlaps a protected ledger dir: %s' % p)
        self.opened, self.violations, self._saved = [], [], None

    def account_path(self, name='TEST_account.sqlite3'):
        """Explicit temp account path; refuses anything outside temp_root or inside a protected dir."""
        return str(self.check_account(self.temp_root / name))

    def check_account(self, path):
        p = Path(path).resolve()
        if any(inside(p, d) for d in self.protected):
            raise AssertionError('test account resolves into a protected (live) ledger dir: %s' % p)
        if not inside(p, self.temp_root):
            raise AssertionError('test account is not under the test temp dir: %s' % p)
        return p

    def _note(self, kind, p):
        r = _resolve(p)
        if r is None:
            return
        self.opened.append((kind, r))
        if any(inside(r, d) for d in self.protected):
            self.violations.append((kind, r))
            raise PermissionError('LedgerGuard: test tried to %s protected path %s' % (kind, r))

    def __enter__(self):
        real_connect, real_open, real_io_open, real_os_open = sqlite3.connect, builtins.open, io.open, os.open
        self._saved = (real_connect, real_open, real_io_open, real_os_open)
        guard = self

        def connect(database, *a, **k):
            guard._note('sqlite3.connect', database)
            return real_connect(database, *a, **k)

        def g_open(file, *a, **k):
            guard._note('open', file)
            return real_open(file, *a, **k)

        def g_os_open(path, *a, **k):
            guard._note('os.open', path)
            return real_os_open(path, *a, **k)

        sqlite3.connect, builtins.open, io.open, os.open = connect, g_open, g_open, g_os_open
        return self

    def __exit__(self, *exc):
        sqlite3.connect, builtins.open, io.open, os.open = self._saved
        return False

    def assert_isolated(self, testcase, account_path):
        """Prove the test used only its temp account: no protected path touched, every
        recorded path under temp_root (or a read-only source/stdlib file), account written."""
        testcase.assertEqual(self.violations, [], 'protected ledger path was opened')
        acct = self.check_account(account_path)
        dbs = [p for k, p in self.opened if k == 'sqlite3.connect']
        testcase.assertTrue(dbs, 'test never opened its temp account')
        testcase.assertTrue(all(p == acct for p in dbs), 'a DB other than the injected temp account was opened: %s' % dbs)
        writes = [p for k, p in self.opened if k == 'os.open']
        testcase.assertTrue(all(inside(p, self.temp_root) for p in writes), 'file created outside temp dir: %s' % writes)


def temp_root(prefix='FIXTURE_TEST_'):
    return tempfile.TemporaryDirectory(prefix=prefix)
