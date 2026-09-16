# Buying AI · ERPNext 采购助手

在 ERPNext 的 Buying 页面中嵌入对话助手，让用户结合当前单据、未保存的表单内容和附件进行查询与分析。业务系统继续使用 ERPNext，不另建采购系统。

## 项目组成

| 目录 | 职责 |
| --- | --- |
| `frappe_app/` | Frappe App，内部名称为 `buying_ai`。提供助手界面、ERPNext 登录身份衔接、页面上下文和请求转发。 |
| `assistant/` | 独立的 Agent 服务，运行在宿主机。负责身份核实、会话管理、模型与工具调用。 |
| `frappe_docker/` | 官方 Docker 仓库子模块，提供 Frappe / ERPNext 开发环境基础。 |

前端使用 React + TypeScript，源码位于 `frappe_app/public/frontend/`；Bench 负责构建和监听，`public/dist/` 是不提交 Git 的构建产物。Python 包通过可编辑安装映射为 `buying_ai`，无需在源码目录中再嵌套同名包。

Assistant 使用 Python 3.13、uv、FastAPI 和 Deep Agents。会话目录通过 SQLAlchemy 存储，LangGraph Checkpoint 使用独立 PostgreSQL；该数据库属于 Assistant，不属于 ERPNext 业务系统。模型参数在 `assistant/conf/app_config.yaml` 配置，密钥和密码放在本地 `.env`。

## 当前已经实现

- Buying 页面右下角的常驻入口和对话面板，样式跟随 ERPNext 主题。
- 点击会话标题展开浮层菜单，支持新建、切换、删除会话；点击外部或按 Esc 收起。
- 流式回复、发送／停止切换、错误提示和重试。关闭面板只断开订阅，重新打开可读取历史并恢复订阅。
- 可选携带当前页面和未保存表单内容；服务端检查单据访问权限并过滤字段。
- `query_items` 工具通过 ERPNext 原生接口查询当前用户有权访问的物料，结果可以跳转到 Item 页面。
- Frappe 同源接口转发普通响应和事件流；Assistant 向 ERPNext 核实登录身份，会话按站点和用户归属隔离。
- 日志、请求追踪和统一异常处理。
- 附件上传及基础处理，但当前附件实现需要按下面的方案重构。

业务接口优先复用 ERPNext 原生 API。Frappe App 按需补充页面集成和业务能力，不重复包装全部业务接口。Agent 层负责模型和工具装配，服务层负责会话及事件转换，客户端层负责外部连接。

## 附件与沙箱：下一步重构方向

**下面是讨论确定的方向，尚未实现。当前没有可执行 Bash 的隔离沙箱，也未实现附件严格按上传者隔离。**

### 当前附件链路

浏览器调用 Frappe 自带的上传接口，把文件保存为站点私有附件。文件本体位于宿主机挂载目录：

```text
frappe_docker/development/frappe-bench/
sites/development.localhost/private/files/
```

文件名、上传者等元数据保存在 MariaDB 的 `File` 记录中。发送消息时，`frappe_app/context.py` 检查读取权限、读取文件并以 Base64 传给 Assistant；Assistant 提前提取文本、解析 PDF 或组织图片输入。

这个实现检查的是 Frappe 读取权限，不等于“只能访问自己上传的附件”。它也没有给 Agent 提供按需读取、解析附件的工具链路。

### 目标附件链路

附件不再上传到 Frappe 的文件系统，改由 Assistant 管理：

1. 用户上传附件，Assistant 通过已核实的登录身份确定上传者及所属会话。
2. 原始文件保存到按用户隔离的持久附件存储；本次会话授权的文件进入对应沙箱工作目录。
3. 消息只携带附件 ID、文件名、类型等元信息，并告诉 Agent 当前有哪些可用附件。
4. Agent 按需通过工具读取、解析或处理附件，不再在每次发送消息前读取全部附件并注入模型。
5. 需要保留的处理结果通过受控接口保存到附件存储，并关联用户和会话，供用户下载或继续使用。

附件的持久存储与沙箱工作目录分开。沙箱销毁后，需要保留的原文件和结果仍应可用；上传失败、结果保存、会话删除和文件清理的生命周期需在重构时一起明确。

### 用户权限与执行隔离

目标是每个会话使用独立沙箱，并绑定经过认证的所属用户：

- 用户身份来自服务端认证上下文，不使用模型或浏览器自报的用户名作为授权依据。
- 附件上传、关联、读取、下载、修改和删除，都校验当前用户与会话归属。
- 沙箱仅获得本次会话获准使用的文件，不挂载 Frappe 完整站点目录、宿主机项目目录或其他用户文件。
- Bash 和其他文件工具都在同一沙箱权限边界内执行，不提供可绕过沙箱的宿主机执行入口。
- ERPNext 会话凭证、数据库密码和模型密钥留在可信服务端，不暴露给沙箱内执行的命令。
- 沙箱网络访问需要控制，避免绕过工具接口访问内部服务；解析缓存和临时文件也按用户、会话隔离。

只按用户分文件夹或在提示词里约束 Agent，不能实现 Bash 执行隔离。沙箱运行时、持久存储实现、资源限制和网络策略尚待选型；不能把框架自带的虚拟文件工具当作已完成的操作系统沙箱。

### 后续实施范围

- 重构上传接口、附件元数据和消息协议。
- 增加 Assistant 附件存储和用户／会话授权。
- 接入沙箱创建、恢复、销毁及任务执行。
- 提供按需读取、解析文件的工具，以及结果保存和下载接口。
- 验证跨用户、跨会话访问被拒绝，Bash 无法访问沙箱外文件，重连和清理不会丢失应保留的数据。

Markdown 渲染、工具执行卡片和操作确认等交互也计划后续扩展，目前未实现。

## 本地开发

在仓库根目录启动 ERPNext：

```bash
git submodule update --init --recursive
docker compose -f frappe_app/docker/compose.yaml up -d
docker compose -f frappe_app/docker/compose.yaml logs -f frappe
```

访问 `http://development.localhost:8000`。首次初始化的开发账号为 `Administrator`，密码为 `admin`；已有站点不会重置密码。

`frappe_app/docker/start.sh` 按功能组织初始化、依赖安装、配置、站点准备和服务启动。Bench 与站点不存在时创建；依赖文件或工具版本变化时重新安装依赖；每次启动仍执行类型检查、前端构建和缓存刷新。无需手动进入容器初始化。

容器仅挂载 `frappe_app/`、`frappe_docker/development/` 和只读的 `frappe_app/docker/`。本机修改前端源码后，Bench 自动重新构建，刷新浏览器查看；修改构建入口时需要重启监听。`assistant/` 不挂载到 Frappe 容器。

配置并启动 Assistant：

1. 首次将 `assistant/conf/.env.example` 复制为 `assistant/conf/.env`，填写所用模型密钥和数据库密码；数据库凭据需与 `assistant/docker-compose.yml` 一致。
2. 在 `assistant/conf/app_config.yaml` 中选择模型，确认 ERPNext、数据库和服务地址。
3. 在仓库根目录执行：

```bash
docker compose -f assistant/docker-compose.yml up -d
uv sync --directory assistant --locked
uv run --directory assistant python main.py
```

Frappe 通过 Compose 中的 `BUYING_AI_AGENT_URL` 访问宿主机 Assistant。Assistant 默认端口为 `8100`，通过配置的 ERPNext 地址核实用户并查询业务数据。真实模型能力需要配置后联调；助手服务不可用不影响 ERPNext 本身使用。

数据库卷、站点文件、依赖目录、构建产物和 `.env` 不随 Git 同步。多设备开发通过源码和锁文件保持一致，每台设备独立准备运行环境；停止 Compose 时不加 `-v` 可保留命名数据卷。
