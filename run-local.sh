#!/usr/bin/env bash
# Local alternative to GitHub Actions. Add to cron (every 12 h):
#   17 */12 * * * /full/path/to/quant-intern-finder/run-local.sh >> /tmp/quant-finder.log 2>&1
# Set NTFY_TOPIC / DISCORD_WEBHOOK_URL in your shell profile or a .env file next to this script.
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
python3 -m pip install -q -r requirements.txt
python3 finder.py "$@"
