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

## Inspecting status
- `python3 run.py --monitor-status`: last success, age, `ok`/`stale` (120s), missed and lock-delayed intervals.
- `python3 run.py --report`: the account (read-only).
- `ops/obs_report.sh SINCE_ISO [UNTIL_ISO]`: restarts, alerts, decisions, longest gap between loop checks, lock-delayed, skipped and suspend gaps, loop starts.
- `tail $OPS_DIR/{exit_loop.log,ensure.log,supervisor_decisions.log}`.

## Pausing and resuming entries
New simulated entries come only from the routine running `run.py --prepare-queue` and then `--consume --review-dir bot-reviews`. To pause, the routine runs only `ensure.sh`, which handles protective exits. To resume, add those two steps back, after review. Exit monitoring runs either way, including while the account is halted.

## Known limitations
- The host sometimes stops scheduling the box when no agent is active. During that time no process runs, protective exits included, and nothing on the box can prevent it. The loop detects the gap afterwards (`suspend_gap`) and checks at once. The routine wake also triggers an inline check.
- The 5-minute routine is the only external clock, so worst-case exit latency is bounded by the platform's scheduling of the box, not by the 60s loop.
- `--prepare-queue` holds the worker lock for about 4–6s (measured). Its worst case is about 60s of screening budget plus up to a 20s HTTP timeout. A concurrent loop check waits up to 30s for the lock and then records `skipped: locked`.
