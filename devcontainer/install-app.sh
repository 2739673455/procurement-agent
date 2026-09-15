#!/usr/bin/env bash
set -Eeuo pipefail
cd /workspace/frappe_docker/development/frappe-bench
app=procurement_assistant
source_dir=/workspace/frappe_app
# 仓库源码平铺；仅在 Bench 工作目录生成 Frappe 构建需要的包目录。
if [[ -L apps/$app ]]; then
    if [[ $(readlink -f "apps/$app") != "$source_dir" ]]; then
        echo "apps/$app points to another app; refusing to overwrite." >&2
        exit 1
    fi
    unlink "apps/$app"
fi
mkdir -p "apps/$app"
for entry in "$app" pyproject.toml; do
    target="$source_dir"
    [[ "$entry" == pyproject.toml ]] && target="$source_dir/pyproject.toml"
    if [[ ! -e apps/$app/$entry && ! -L apps/$app/$entry ]]; then
        ln -s "$target" "apps/$app/$entry"
    fi
    if [[ $(readlink -f "apps/$app/$entry") != "$target" ]]; then
        echo "apps/$app/$entry has an unexpected target; refusing to overwrite." >&2
        exit 1
    fi
done
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
