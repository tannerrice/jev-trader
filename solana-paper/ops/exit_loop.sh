#!/bin/bash
# Paper-only protective exit monitor. One check about every 60s.
# - Waits up to LOCK_WAIT s for the worker lock (recorded as lock-delayed) instead of skipping at once.
# - Sleeps in 5s slices and touches loop.alive each slice, so the supervisor can tell a
#   live-but-suspended loop from a dead or hung one.
# - If a slice overruns by >20s (box/VM was not scheduled), logs suspend_gap and checks immediately.
DESK_DIR=${DESK_DIR:-/workspace/jev-trader/solana-paper}
OPS_DIR=${OPS_DIR:-/workspace/desk-ops}
INTERVAL=${INTERVAL:-60}; LOCK_WAIT=${LOCK_WAIT:-30}; RUN_TIMEOUT=${RUN_TIMEOUT:-50}
LOG=$OPS_DIR/exit_loop.log
cd "$DESK_DIR" || exit 1
export SOLANA_ENABLE_PAID_AI=no; unset XAI_API_KEY
echo $$ > "$OPS_DIR/loop.pid"
echo "$(date -Is) loop_start pid=$$" >> "$LOG"
trap 'echo "$(date -Is) loop_stop pid=$$ signal=TERM" >> "$LOG"; exit 0' TERM
while true; do
  date +%s > "$OPS_DIR/loop.alive"
  t0=$(date +%s)
  timeout "$RUN_TIMEOUT" python3 run.py --exit-only --lock-wait "$LOCK_WAIT" >> "$LOG" 2>&1 &
  wait $!; rc=$?
  echo "$(date -Is) exit=$rc dur=$(( $(date +%s) - t0 ))s" >> "$LOG"
  slept=0
  while [ "$slept" -lt "$INTERVAL" ]; do
    a=$(date +%s); sleep 5 & wait $!; b=$(date +%s)
    date +%s > "$OPS_DIR/loop.alive"
    if [ $(( b - a )) -gt 25 ]; then echo "$(date -Is) suspend_gap=$(( b - a ))s (sleep 5 overran; box not scheduled)" >> "$LOG"; break; fi
    slept=$(( slept + b - a ))
  done
done
