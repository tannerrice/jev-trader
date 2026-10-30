"""Supervisor for the paper exit loop. Stdlib only; never touches thresholds or exit logic.

Restart rules (decide()):
  * restart only if the loop process is gone, or its loop.alive heartbeat (touched every 5s)
    is older than HUNG_AFTER seconds even after a re-check -> the loop is hung.
  * a stale *monitor* status alone never restarts a live loop: it triggers an inline
    protective check (run.py --exit-only --lock-wait) and is logged as an alert.
Every decision is appended as JSON to supervisor_decisions.log.
A restart sends SIGTERM to the loop shell only, never to a run.py child; it then waits for
any running run.py --exit-only child to finish before starting a new loop.
"""
import json, os, signal, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HUNG_AFTER = 180
RECHECK_SECONDS = 12


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

    pid, alive, age, st = facts()
    action, reason = decide(alive, age, st.get('status'))
    rechecked = False
    if action == 'restart' and alive:
        time.sleep(RECHECK_SECONDS)  # a just-resumed loop touches loop.alive within 5s
        pid, alive, age, st = facts(); rechecked = True
        action, reason = decide(alive, age, st.get('status'))
    rec = dict(at=datetime.now().astimezone().isoformat(timespec='seconds'), action=action, reason=reason,
               loop_pid=pid, loop_alive=alive, heartbeat_age=None if age is None else round(age, 1),
               rechecked=rechecked, monitor=dict((k, st.get(k)) for k in ('status', 'age_seconds', 'missed_count', 'lock_delayed_count')),
               run_py_children=exit_children())
    if action == 'inline_check':
        r = subprocess.run([sys.executable, 'run.py', '--exit-only', '--lock-wait', '30'], cwd=desk, env=env,
                           capture_output=True, text=True, timeout=60)
        rec['inline_check'] = dict(rc=r.returncode, out=r.stdout.strip()[-300:], err=r.stderr.strip()[-300:])
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
        subprocess.Popen(['setsid', 'nohup', str(Path(__file__).with_name('exit_loop.sh'))], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env=dict(env, OPS_DIR=str(ops), DESK_DIR=str(desk)))
        with open(ops/'ensure.log', 'a') as f:
            f.write('%s restarted exit_loop: %s\n' % (rec['at'], reason))
    if action != 'ok':
        with open(ops/'ensure.log', 'a') as f:
            f.write('%s ALERT %s: %s\n' % (rec['at'], action, reason))
    with open(ops/'supervisor_decisions.log', 'a') as f:
        f.write(json.dumps(rec) + '\n')
    print(json.dumps(rec))


if __name__ == '__main__':
    main()
