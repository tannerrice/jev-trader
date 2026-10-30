# Solana paper desk – operations (paper only)

No wallets, no real trades, no paid AI (`SOLANA_ENABLE_PAID_AI=no`, `XAI_API_KEY` unset in every launcher).
Thresholds live in `config.py`/`run.py` constants and are not changed by anything here.

## Architecture
The box has no service manager, so two pieces work together:

| piece | how it runs | what it does |
|---|---|---|
| `exit_loop.sh` | background (`setsid nohup`), writes `$OPS_DIR/loop.pid` | about every 60s: `timeout 50 run.py --exit-only --lock-wait 30`. Logs `exit=<rc> dur=<s>`, stderr and `loop_start/loop_stop` to `exit_loop.log`. Sleeps in 5s slices and touches `loop.alive` on each one. If a slice overruns by more than 25s (the box was not scheduled), it logs `suspend_gap=<s>` and checks again at once. |
| `ensure.sh` → `supervisor.py` | the 5-minute routine | gathers facts (loop pid from pidfile, verified against `/proc/<pid>/cmdline`, `loop.alive` age and `run.py --monitor-status`), decides, acts and appends one JSON line to `supervisor_decisions.log`. Also restarts the localhost dashboard if it is down. |

`OPS_DIR` defaults to `/workspace/desk-ops`; it holds logs, pidfile, heartbeat and `dash.token` and is never committed. `DESK_DIR` defaults to `/workspace/jev-trader/solana-paper`. The live `/workspace/desk-ops/{ensure,exit_loop,obs_report}.sh` are symlinks to this folder.

## Restart decision rules (`supervisor.decide`)
1. Loop process gone → **restart**.
2. `loop.alive` is older than `HUNG_AFTER` = 180s. That is more than the 30s lock wait, the 50s run timeout and the 5s slice combined. The supervisor checks again after 12s, because a loop that has just resumed touches the file within 5s. If it is still too old → **restart** (hung).
3. Loop alive but monitor status is `stale` → **inline_check**. The supervisor runs one protective `run.py --exit-only --lock-wait 30` itself and writes an `ALERT` line to `ensure.log`. It does not kill a live loop.
4. Otherwise **ok**.

A restart sends SIGTERM to the loop shell only. Then it waits up to 60s for any `run.py --exit-only` child to finish. It never signals `run.py`. Even if a run.py process were killed, the ledger is a single-row SQLite WAL state with `synchronous=FULL`, and each update is one `BEGIN IMMEDIATE … COMMIT` transaction, so a kill rolls back cleanly.

Alerts are not suppressed: every non-ok decision writes an `ALERT` line. Missed intervals (gap > 90s) are still counted in the ledger's `monitor.missed`. Lock-delayed checks (wait ≥ 1s) are recorded separately in `monitor.lock_delayed`.

## Supervisor singleton lock
`supervisor.py` holds an exclusive `fcntl.flock` on `$OPS_DIR/supervisor.lock` for the whole inspect → decide → act → log sequence. The kernel releases it if the process dies. A concurrent invocation waits up to `SUPERVISOR_LOCK_WAIT` (default 120s) and then re-inspects from scratch, so it sees any loop the first invocation launched. If the lock is still busy after the wait, it logs `skipped: another supervisor active` to `supervisor_decisions.log` plus an `ALERT` line to `ensure.log` and does nothing. Waiting-then-re-inspecting was chosen over exit-at-once so a routine wake still gets a fresh decision. After launching a loop, the supervisor keeps the lock until the new pid is in `loop.pid` and verified via `/proc` (up to 15s; otherwise an `ALERT`), so a second invocation can never launch a second loop. Tested by `test_ops_supervisor2.py` (6 concurrent invocations → exactly one loop).

## Inline-check failure handling
The inline protective check (`run.py --exit-only --lock-wait 30`, timeout `INLINE_CHECK_TIMEOUT` default 60s) never raises out of the supervisor. A `TimeoutExpired` is recorded as action `inline_check_timeout`. An `OSError` or other exec error, a non-zero exit or unparsable (non-JSON) output is recorded as `inline_check_failed`. Each record carries `rc`, `error` and the stderr/stdout tail (truncated to 300 chars), and writes an `ALERT` line to `ensure.log`. The supervisor still writes its decision line and exits 0, so the routine reports it.

## Host suspension limitation
This environment is **not continuously available**. The host sometimes stops scheduling the box for tens of seconds up to several minutes. During a suspension **no process runs, including protective exits**, so a stop-loss or take-profit cannot execute until the box resumes. Measured:
- Before the supervisor fix, gaps of up to about 8 minutes (the cause of 45 of 48 false restarts).
- During the 2026-10-30 13:41–14:45 EDT observation: pauses of about 35–93s at roughly 13:47, 13:48, 14:08, 14:09 and 14:28. Missed monitor intervals rose from 80 to 82. The longest gap between successful checks was 113s (14:26:51–14:28:44). The supervisor made 0 restarts and raised 0 alerts in that window.

Nothing on the box can prevent this. The loop detects a gap afterwards (`suspend_gap`) and checks at once, and gaps are counted in `monitor.missed`. Do not describe this desk as providing continuous protective monitoring.

## Inspecting status
- `python3 run.py --monitor-status`: last success, age, `ok`/`stale` (120s), missed and lock-delayed intervals.
- `python3 run.py --report`: the account (read-only).
- `ops/obs_report.sh SINCE_ISO [UNTIL_ISO]`: restarts, alerts, decisions, longest gap between loop checks, lock-delayed, skipped and suspend gaps, loop starts.
- `tail $OPS_DIR/{exit_loop.log,ensure.log,supervisor_decisions.log}`.

## Pausing and resuming entries
New simulated entries come only from the routine running `run.py --prepare-queue` and then `--consume --review-dir bot-reviews`. To pause, the routine runs only `ensure.sh`, which handles protective exits. To resume, add those two steps back, after review. Exit monitoring runs either way, including while the account is halted.

## Known limitations
- Host suspension: see the section above. Protective exits cannot run while the box is suspended.
- The 5-minute routine is the only external clock, so worst-case exit latency is bounded by the platform's scheduling of the box, not by the 60s loop.
- `--prepare-queue` holds the worker lock for about 4–6s (measured). Its worst case is about 60s of screening budget plus up to a 20s HTTP timeout. A concurrent loop check waits up to 30s for the lock and then records `skipped: locked`.
