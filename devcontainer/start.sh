#!/usr/bin/env bash
set -Eeuo pipefail

# Run by Compose as frappe, with the whole repository mounted at /workspace.
cd /workspace/frappe_docker/development
site=development.localhost
marker=.procurement-initializing
trap 'echo "ERPNext startup failed at line $LINENO. See preceding logs; existing data has not been deleted." >&2' ERR

if [[ -e "$marker" ]]; then
    echo "A previous initialization did not finish. Inspect/repair the Bench and site before removing $PWD/$marker and retrying. No automatic reset will be performed." >&2
    exit 1
fi

if [[ ! -d frappe-bench ]]; then
    touch "$marker"
    bench init --skip-redis-config-generation --frappe-branch version-16 \
        --apps_path=/workspace/frappe_docker/development/apps-example.json frappe-bench </dev/null
    rm "$marker"
fi

cd frappe-bench
if [[ ! -f Procfile || ! -x env/bin/python || ! -d apps/frappe || ! -d apps/erpnext ]]; then
    echo "Existing Bench is incomplete. Repair it before restarting; no files were deleted." >&2
    exit 1
fi

bench set-config -g db_host mariadb
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
bench set-config -gp developer_mode 1

if [[ ! -d "sites/$site" ]]; then
    touch "../$marker"
    bench new-site "$site" --db-type mariadb --db-host mariadb \
        --db-root-username root --db-root-password 123 \
        --mariadb-user-host-login-scope='%' --admin-password admin \
        --install-app erpnext </dev/null
    rm "../$marker"
fi

# Connect to the actual database, not just check for a site_config.json file.
# A missing database or partially installed site must fail, never be recreated.
bench --site "$site" list-apps --format json | env/bin/python -c '
import json, sys
apps = json.load(sys.stdin)
if not {"frappe", "erpnext"}.issubset(apps.get("development.localhost", [])):
    sys.exit("Site is missing Frappe or ERPNext. Repair installation before restarting.")
'
bench use "$site"
echo "Starting ERPNext: http://development.localhost:8000"
exec bench start
