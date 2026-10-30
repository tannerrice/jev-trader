#!/usr/bin/env python3
"""Run the full offline suite and keep its complete record.

Streams unittest -v output to the console unchanged AND saves, per run, under
<logs>/<UTC timestamp>/: output.txt (full stdout+stderr), summary.json (exit code,
counts, failing test names, tracebacks). <logs>/latest-summary.json and
latest-summary.txt point at the newest run. Default logs dir: data/test-runs/
(gitignored); override with --logs-dir or SOLANA_TEST_LOG_DIR. Exit code = suite's.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEP = '=' * 70


def parse(output):
    m = re.search(r'^Ran (\d+) tests? in ([0-9.]+)s', output, re.M)
    tail = re.search(r'^(OK|FAILED)(?: \((.*)\))?\s*$', output, re.M)
    counts = dict(ran=int(m.group(1)) if m else None, status=tail.group(1) if tail else None)
    if tail and tail.group(2):
        for part in tail.group(2).split(','):
            k, _, v = part.strip().partition('=')
            if v.isdigit():
                counts[k] = int(v)
    failing = []
    for block in output.split(SEP)[1:]:
        head = re.match(r'\s*(FAIL|ERROR|UNEXPECTED SUCCESS): (.+)', block)
        if head:
            body = block.split('-' * 70, 1)
            failing.append(dict(kind=head.group(1), test=head.group(2).strip(),
                                traceback=body[1].strip() if len(body) > 1 else block.strip()))
    return counts, failing


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--logs-dir', default=os.environ.get('SOLANA_TEST_LOG_DIR', str(HERE / 'data' / 'test-runs')))
    ap.add_argument('--pattern', default='test_*.py')
    a = ap.parse_args(argv)
    logs = Path(a.logs_dir)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run_dir = logs / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, '-m', 'unittest', 'discover', '-s', str(HERE), '-t', str(HERE), '-p', a.pattern, '-v']
    started = time.time()
    lines = []
    with open(run_dir / 'output.txt', 'w') as out:
        proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            sys.stdout.write(line); sys.stdout.flush()
            out.write(line); out.flush()
            lines.append(line)
        code = proc.wait()
    output = ''.join(lines)
    counts, failing = parse(output)
    summary = dict(started_utc=datetime.fromtimestamp(started, timezone.utc).isoformat(),
                   duration_s=round(time.time() - started, 2), exit_code=code, command=cmd,
                   output=str(run_dir / 'output.txt'), **counts, failing=failing)
    for p in (run_dir / 'summary.json', logs / 'latest-summary.json'):
        p.write_text(json.dumps(summary, indent=1) + '\n')
    txt = ['run %s exit_code=%s ran=%s status=%s' % (stamp, code, counts.get('ran'), counts.get('status')),
           'output: %s' % (run_dir / 'output.txt')]
    for f in failing:
        txt += ['', '%s: %s' % (f['kind'], f['test']), f['traceback']]
    (logs / 'latest-summary.txt').write_text('\n'.join(txt) + '\n')
    print('[run_tests] exit_code=%s log=%s' % (code, run_dir), file=sys.stderr)
    return code


if __name__ == '__main__':
    sys.exit(main())
