"""Observation report for a window: obs_report.py SINCE_ISO [UNTIL_ISO]. Read-only."""
import json, re, subprocess, sys, os
from datetime import datetime
OPS = os.environ.get('OPS_DIR', '/workspace/desk-ops'); DESK = os.environ.get('DESK_DIR', '/workspace/jev-trader/solana-paper')
since = datetime.fromisoformat(sys.argv[1]).astimezone()
until = datetime.fromisoformat(sys.argv[2]).astimezone() if len(sys.argv) > 2 else datetime.now().astimezone()
inwin = lambda t: since <= t <= until
ok, skipped, delayed, suspends, starts, prev = [], 0, 0, [], [], None
for line in open(f'{OPS}/exit_loop.log'):
    line = line.strip()
    m = re.match(r'(\S+) exit=(\d+)', line)
    if m:
        t = datetime.fromisoformat(m[1])
        if inwin(t):
            if prev and '"exit-only"' in prev and m[2] == '0':
                ok.append(t)
                try:
                    if json.loads(prev).get('lock_wait_seconds', 0) >= 1: delayed += 1
                except ValueError: pass
            elif prev and 'skipped: locked' in prev: skipped += 1
        prev = None; continue
    m = re.match(r'(\S+) (suspend_gap=\d+)s', line) or re.match(r'(\S+) (loop_start)', line)
    if m and inwin(datetime.fromisoformat(m[1])):
        (suspends if 'suspend' in m[2] else starts).append((m[1], m[2]))
    prev = line
restarts = [l for l in open(f'{OPS}/ensure.log') if 'restarted exit_loop' in l and inwin(datetime.fromisoformat(l.split()[0]))]
alerts = [l for l in open(f'{OPS}/ensure.log') if ' ALERT ' in l and inwin(datetime.fromisoformat(l.split()[0]))]
dec = []
if os.path.exists(f'{OPS}/supervisor_decisions.log'):
    dec = [json.loads(l) for l in open(f'{OPS}/supervisor_decisions.log') if inwin(datetime.fromisoformat(json.loads(l)['at']))]
gaps = sorted(((b - a).total_seconds(), a, b) for a, b in zip(ok, ok[1:]))
st = json.loads(subprocess.run([sys.executable, 'run.py', '--monitor-status'], cwd=DESK, capture_output=True, text=True).stdout)
missed_win = [g for g in st.get('missed_intervals', []) if since.timestamp() <= g['end'] <= until.timestamp()]
print(json.dumps(dict(window=[since.isoformat(), until.isoformat()], supervisor_restarts=len(restarts), alerts=len(alerts),
    decisions={a: sum(1 for d in dec if d['action'] == a) for a in ('ok', 'inline_check', 'restart')},
    loop_checks_ok=len(ok), longest_gap_between_loop_checks_s=gaps[-1][0] if gaps else None,
    longest_gap_at=[gaps[-1][1].isoformat(), gaps[-1][2].isoformat()] if gaps else None,
    lock_delayed_checks=delayed, skipped_locked=skipped, suspend_gaps=suspends, loop_starts=starts,
    monitor_missed_count_total=st.get('missed_count'), monitor_missed_in_window_last20=len(missed_win),
    monitor_status=st.get('status'), monitor_age_s=st.get('age_seconds')), indent=2))
