#!/usr/bin/env bash
set -euo pipefail
LOCKFILE="/tmp/coin2win_monthly_billing.lock"
PERIOD="$(date -u +%Y-%m)"
ADMIN_KEY="a94a0cc63dbf98983618422133cdcabd635cf043dc624909200ede0ee60b8c6c"
exec 9>"$LOCKFILE"
flock -n 9 || { echo "billing runner already running"; exit 0; }
curl -s -X POST http://127.0.0.1:8000/admin/billing/run-global \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: ${ADMIN_KEY}" \
  -d "{\"period_key\":\"$PERIOD\"}"
