"""Supervisor for the paper exit loop. Stdlib only; never touches thresholds or exit logic.

Restart rules (decide()):
  * restart only if the loop process is gone, or its loop.alive heartbeat (touched every 5s)
    is older than HUNG_AFTER seconds even after a re-check -> the loop is hung.
  * a stale *monitor* status alone never restarts a live loop: it triggers an inline
    protective check (run.py --exit-only --lock-wait) and is logged as an alert.
Every decision is appended as JSON to supervisor_decisions.log.

Singleton: the whole inspect -> decide -> act -> log sequence runs under an exclusive
fcntl.flock on OPS_DIR/supervisor.lock (released by the kernel if the process dies).
A concurrent invocation waits up to SUPERVISOR_LOCK_WAIT seconds, then re-inspects from
scratch (so it sees the loop the first one launched); if the lock is still busy it logs
'skipped: another supervisor active' and does nothing. A launched loop must appear in
loop.pid (verified via /proc) before the lock is released.
Inline-check failures (timeout, OSError, non-zero exit, unparsable output) are recorded as
inline_check_timeout / inline_check_failed with an ALERT line; the supervisor still exits 0.
A restart sends SIGTERM to the loop shell only, never to a run.py child; it then waits for
any running run.py --exit-only child to finish before starting a new loop.
"""
import fcntl, json, os, signal, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HUNG_AFTER = 180
RECHECK_SECONDS = 12
SUPERVISOR_LOCK_WAIT = float(os.environ.get('SUPERVISOR_LOCK_WAIT', 120))
INLINE_TIMEOUT = float(os.environ.get('INLINE_CHECK_TIMEOUT', 60))
LAUNCH_CONFIRM_S = 15


def decide(pid_alive, alive_age, monitor_status):
    """Pure decision. Returns (action, reason). action in ok|inline_check|restart."""
    if not pid_alive:
        return 'restart', 'loop process not running'
    if alive_age is None or alive_age > HUNG_AFTER:
        return 'restart', 'loop heartbeat older than %ss (hung)' % HUNG_AFTER
    if monitor_status != 'ok':
        return 'inline_check', 'monitor status %s but loop alive (heartbeat %.0fs); likely suspended or lock-delayed' % (monitor_status, alive_age)
    return 'ok', 'loop alive and monitor ok'


def loop_cmd_ok(pid):
    try:
        cmd = Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
    except OSError:
        return False
    return any(c.endswith(b'exit_loop.sh') for c in cmd[:2])


def exit_children():
    out = []
    for p in Path('/proc').iterdir():
        if p.name.isdigit():
            try:
                cmd = (p/'cmdline').read_bytes().split(b'\0')
            except OSError:
                continue
            if b'run.py' in cmd and b'--exit-only' in cmd and not any(b'timeout' == c for c in cmd[:1]):
                out.append(int(p.name))
    return out


def inline_check(desk, env, timeout=None):
    """Run one protective exit check. Never raises; returns (action_override_or_None, record)."""
    timeout = INLINE_TIMEOUT if timeout is None else timeout
    cmd = [sys.executable, 'run.py', '--exit-only', '--lock-wait', '30']
    try:
        r = subprocess.run(cmd, cwd=desk, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode(errors='replace') if isinstance(e.stderr, bytes) else (e.stderr or '')
        return 'inline_check_timeout', dict(rc=None, error='timeout after %ss' % timeout, err=err.strip()[-300:])
    except Exception as e:  # OSError, etc.
        return 'inline_check_failed', dict(rc=None, error='%s: %s' % (type(e).__name__, e))
    rec = dict(rc=r.returncode, out=(r.stdout or '').strip()[-300:], err=(r.stderr or '').strip()[-300:])
    if r.returncode != 0:
        rec['error'] = 'non-zero exit %s' % r.returncode
        return 'inline_check_failed', rec
    try:
        json.loads((r.stdout or '').strip().splitlines()[-1])
    except (ValueError, IndexError):
        rec['error'] = 'unparsable output'
        return 'inline_check_failed', rec
    return None, rec


def acquire(ops, wait):
    f = open(ops/'supervisor.lock', 'a+')
    deadline = time.time() + wait
    waited = False
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f, waited
        except BlockingIOError:
            waited = True
            if time.time() >= deadline:
                f.close(); return None, waited
            time.sleep(0.2)


def main(ops=os.environ.get('OPS_DIR', '/workspace/desk-ops'), desk=os.environ.get('DESK_DIR', '/workspace/jev-trader/solana-paper')):
    ops, desk = Path(ops), Path(desk)
    env = dict(os.environ, SOLANA_ENABLE_PAID_AI='no'); env.pop('XAI_API_KEY', None)

    def facts():
        try:
            pid = int((ops/'loop.pid').read_text())
        except (OSError, ValueError):
            pid = None
        alive = pid is not None and loop_cmd_ok(pid)
        try:
            age = time.time() - int((ops/'loop.alive').read_text())
        except (OSError, ValueError):
            age = None
        try:
            st = json.loads(subprocess.run([sys.executable, 'run.py', '--monitor-status'], cwd=desk, env=env,
                                           capture_output=True, text=True, timeout=30).stdout)
        except Exception:
            st = {}
        return pid, alive, age, st

    lockf, lock_waited = acquire(ops, SUPERVISOR_LOCK_WAIT)
    if lockf is None:
        rec = dict(at=datetime.now().astimezone().isoformat(timespec='seconds'), action='skipped',
                   reason='skipped: another supervisor active (waited %ss)' % SUPERVISOR_LOCK_WAIT)
        with open(ops/'ensure.log', 'a') as f:
            f.write('%s ALERT skipped: another supervisor active\n' % rec['at'])
        with open(ops/'supervisor_decisions.log', 'a') as f:
            f.write(json.dumps(rec) + '\n')
        print(json.dumps(rec)); return rec
    try:
        return _locked(ops, desk, env, facts, lock_waited)
    finally:
        lockf.close()


def _locked(ops, desk, env, facts, lock_waited):
    pid, alive, age, st = facts()
    action, reason = decide(alive, age, st.get('status'))
    rechecked = False
    if action == 'restart' and alive:
        time.sleep(RECHECK_SECONDS)  # a just-resumed loop touches loop.alive within 5s
        pid, alive, age, st = facts(); rechecked = True
        action, reason = decide(alive, age, st.get('status'))
    rec = dict(at=datetime.now().astimezone().isoformat(timespec='seconds'), action=action, reason=reason,
               loop_pid=pid, loop_alive=alive, heartbeat_age=None if age is None else round(age, 1),
               rechecked=rechecked, waited_for_supervisor_lock=lock_waited, monitor=dict((k, st.get(k)) for k in ('status', 'age_seconds', 'missed_count', 'lock_delayed_count')),
               run_py_children=exit_children())
    if action == 'inline_check':
        override, rec['inline_check'] = inline_check(desk, env)
        if override:
            action = rec['action'] = override
            reason = rec['reason'] = '%s; inline check %s' % (reason, rec['inline_check'].get('error'))
    if action == 'restart':
        if alive:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not loop_cmd_ok(pid):
                    break
                time.sleep(0.5)
        waited = 0
        while exit_children() and waited < 60:  # never interrupt an account update
            time.sleep(1); waited += 1
        rec['waited_for_child_s'] = waited
        try:
            (ops/'loop.pid').unlink()
        except OSError:
            pass
        script = os.environ.get('EXIT_LOOP_SCRIPT') or str(Path(__file__).with_name('exit_loop.sh'))
        subprocess.Popen(['setsid', 'nohup', script], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env=dict(env, OPS_DIR=str(ops), DESK_DIR=str(desk)))
        new_pid, t0 = None, time.time()  # hold the lock until the new loop is visible
        while time.time() - t0 < LAUNCH_CONFIRM_S:
            try:
                new_pid = int((ops/'loop.pid').read_text())
                if loop_cmd_ok(new_pid):
                    break
            except (OSError, ValueError):
                pass
            new_pid = None; time.sleep(0.2)
        rec['new_loop_pid'] = new_pid
        if new_pid is None:
            with open(ops/'ensure.log', 'a') as f:
                f.write('%s ALERT restart: new loop not confirmed within %ss\n' % (rec['at'], LAUNCH_CONFIRM_S))
        with open(ops/'ensure.log', 'a') as f:
            f.write('%s restarted exit_loop: %s\n' % (rec['at'], reason))
    if action != 'ok':
        with open(ops/'ensure.log', 'a') as f:
            f.write('%s ALERT %s: %s\n' % (rec['at'], action, reason))
    with open(ops/'supervisor_decisions.log', 'a') as f:
        f.write(json.dumps(rec) + '\n')
    print(json.dumps(rec))
    return rec


if __name__ == '__main__':
    main()
