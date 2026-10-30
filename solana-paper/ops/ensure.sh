#!/bin/bash
# Idempotent supervisor, run by the 5-minute routine. See OPERATIONS.md.
OPS_DIR=${OPS_DIR:-/workspace/desk-ops}; DESK_DIR=${DESK_DIR:-/workspace/jev-trader/solana-paper}
export OPS_DIR DESK_DIR
HERE=$(dirname "$(readlink -f "$0")")
python3 "$HERE/supervisor.py"
cd "$DESK_DIR" || exit 1
if ! ss -ltn | grep -q '127.0.0.1:8787'; then
  [ -f "$OPS_DIR/dash.token" ] && export SOLANA_DASH_TOKEN=$(cat "$OPS_DIR/dash.token")
  SOLANA_ENABLE_PAID_AI=no setsid nohup python3 server.py >> "$OPS_DIR/dashboard.log" 2>&1 < /dev/null &
  echo "$(date -Is) restarted dashboard" >> "$OPS_DIR/ensure.log"
fi
