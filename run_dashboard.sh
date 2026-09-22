#!/usr/bin/env bash
# run_dashboard.sh
#
# Refreshes the dashboard and opens it in your default browser.
# Run this manually whenever you want, or put it on a cron schedule
# (see README.md) for a weekly auto-refresh.

set -euo pipefail
cd "$(dirname "$0")"

# Load a local .env file if present (KEY=value per line)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

python3 refresh_dashboard.py --summarize --out dashboard.html

# Open it in the default browser (best-effort across platforms)
if command -v open >/dev/null 2>&1; then
  open dashboard.html          # macOS
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open dashboard.html      # Linux
else
  echo "Built dashboard.html — open it manually in a browser."
fi
