#!/usr/bin/env bash
set -Eeuo pipefail
cd /workspace/frappe_docker/development/frappe-bench
app=procurement_assistant
source_dir=/workspace/frappe_app
if [[ ! -e apps/$app && ! -L apps/$app ]]; then
    ln -s "$source_dir" "apps/$app"
fi
if [[ $(readlink -f "apps/$app") != "$source_dir" ]]; then
    echo "apps/$app must point to $source_dir; refusing to overwrite another app." >&2
    exit 1
fi
# The editable package and assets live in the main repository, not ignored Bench data.
uv pip install --python env/bin/python --no-deps -e "$source_dir"
env/bin/python - <<'PY'
from pathlib import Path
p = Path('sites/apps.txt')
apps = p.read_text().splitlines()
if 'procurement_assistant' not in apps:
    p.write_text('\n'.join([*apps, 'procurement_assistant']) + '\n')
PY
bench --site development.localhost install-app "$app"
bench build --app "$app"
bench --site development.localhost clear-cache
