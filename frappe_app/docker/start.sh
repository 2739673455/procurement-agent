#!/usr/bin/env bash
set -Eeuo pipefail

# 基础配置：路径和应用、站点名称。
development_dir=/workspace/frappe_docker/development
source_dir=/workspace/frappe_app
app=buying_ai
site=development.localhost

# 命令失败时停止启动并打印行号，具体原因查看此前日志。
trap 'echo "ERPNext 启动失败，出错位置：第 $LINENO 行。请查看前面的日志。" >&2' ERR

# 一、首次初始化 Bench，之后进入统一的工作目录。
initialize_bench() {
    cd "$development_dir"
    if [[ ! -d frappe-bench ]]; then
        # 使用 Frappe 16，并按 apps-example.json 获取 ERPNext 等应用。
        # Redis 由独立容器提供，因此跳过本地 Redis 配置生成。
        # 重定向标准输入，防止自动启动流程等待交互输入。
        bench init --skip-redis-config-generation --frappe-branch version-16 \
            --apps_path=/workspace/frappe_docker/development/apps-example.json frappe-bench </dev/null
    fi
    cd frappe-bench
    # 准备 Bench 识别和运行所需的进程目录。
    mkdir -p config/pids
    # 镜像中的解释器路径变化时，重建 Bench 环境并安装 ERPNext 依赖。
    if ! env/bin/python --version >/dev/null 2>&1; then
        UV_VENV_CLEAR=1 bench setup env --python "$(pyenv which python)"
        uv pip install --python env/bin/python -e apps/erpnext
    fi
}

# 二、准备 App 源码链接，供 Bench 查找前端资源。
link_app() {
    # 仓库源码平铺；仅在 Bench 工作目录生成 Frappe 构建需要的包目录。
    mkdir -p "apps/$app"
    ln -sfnT "$source_dir" "apps/$app/$app"
}

# 三、按需安装依赖，签名未变化时复用已有安装。
install_dependencies() {
    # 依赖签名保存在对应安装目录中；依赖变更或安装目录被删除后自动重装。
    # 仅安装成功后写入签名，失败后的下次启动会重新尝试。
    local node_signature node_stamp python_signature python_stamp
    node_signature=$(cat "$source_dir/package.json" "$source_dir/package-lock.json"; node --version; npm --version)
    node_signature=$(printf '%s' "$node_signature" | sha256sum | cut -d ' ' -f 1)
    node_stamp="$source_dir/node_modules/.buying-ai-dependencies"
    if [[ ! -f "$node_stamp" ]] || [[ $(cat "$node_stamp") != "$node_signature" ]]; then
        echo "正在安装前端依赖……"
        npm ci --prefix "$source_dir" --no-audit --no-fund
        printf '%s' "$node_signature" > "$node_stamp"
    fi
    python_signature=$(cat "$source_dir/pyproject.toml"; env/bin/python --version)
    python_signature=$(printf '%s' "$python_signature" | sha256sum | cut -d ' ' -f 1)
    python_stamp=env/.buying-ai-dependencies
    if [[ ! -f "$python_stamp" ]] || [[ $(cat "$python_stamp") != "$python_signature" ]]; then
        echo "正在安装 App Python 包……"
        uv pip install --python env/bin/python --no-deps -e "$source_dir"
        printf '%s' "$python_signature" > "$python_stamp"
    fi
}

# 四、登记 Bench 可用应用；站点安装在后续步骤执行。
register_app() {
    env/bin/python - <<'PY'
from pathlib import Path
p = Path('sites/apps.txt')
apps = p.read_text().splitlines()
if 'buying_ai' not in apps:
    p.write_text('\n'.join([*apps, 'buying_ai']) + '\n')
PY
}

# 五、配置数据库、Redis 和开发模式。
configure_bench() {
    bench set-config -g db_host mariadb
    bench set-config -g redis_cache redis://redis-cache:6379
    bench set-config -g redis_queue redis://redis-queue:6379
    bench set-config -g redis_socketio redis://redis-queue:6379
    bench set-config -gp developer_mode 1
}

# 六、首次创建站点并安装 ERPNext。
initialize_site() {
    if [[ ! -d "sites/$site" ]]; then
        bench new-site "$site" --db-type mariadb --db-host mariadb \
            --db-root-username root --db-root-password 123 \
            --mariadb-user-host-login-scope='%' --admin-password admin \
            --install-app erpnext </dev/null
    fi
}

# 七、安装 App、检查类型并构建资源，更新默认站点和缓存。
prepare_app() {
    npm run --prefix "$source_dir" typecheck
    bench --site "$site" install-app "$app"
    bench build --app "$app"
    bench --site "$site" clear-cache
    bench use "$site"
}

# 启动流程：App 的链接和 Python 安装必须先于加载应用的 Bench 命令。
initialize_bench
link_app
install_dependencies
register_app
configure_bench
initialize_site
prepare_app

echo "正在启动 ERPNext，访问地址：http://development.localhost:8000"
exec bench start
