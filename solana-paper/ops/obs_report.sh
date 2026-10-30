#!/bin/bash
# Usage: obs_report.sh SINCE_ISO [UNTIL_ISO]
exec python3 "$(dirname "$(readlink -f "$0")")/obs_report.py" "$@"
