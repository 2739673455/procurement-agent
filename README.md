# Buying AI · ERPNext 采购助手

在 ERPNext 的 Buying 页面中嵌入对话助手，让用户结合当前单据、未保存的表单内容和附件进行查询与分析。采购业务由 ERPNext 提供。

## 项目组成

| 目录             | 职责                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------- |
| `frappe_app/`    | Frappe App，内部名称为 `buying_ai`。提供助手界面、ERPNext 登录身份衔接、页面上下文和请求转发。 |
| `assistant/`     | 独立的 Agent 服务，运行在宿主机。负责身份核实、会话管理、模型与工具调用。                      |
| `frappe_docker/` | 官方 Docker 仓库子模块，提供 Frappe / ERPNext 开发环境基础。                                   |

前端使用 React + TypeScript，源码位于 `frappe_app/public/frontend/`；Bench 负责构建和监听，`public/dist/` 是不提交 Git 的构建产物。Python 包通过可编辑安装映射为 `buying_ai`，无需在源码目录中再嵌套同名包。

Assistant 使用 Python 3.13、uv、FastAPI 和 AgentScope 2。框架的 `ChatService` 负责 Agent 装配、推理循环、工具调用、消息与状态保存；`SessionService` 负责运行状态、取消和删除联动；`ChatRunRegistry` 与消息总线负责后台任务、会话互斥和事件分发。`AsyncSQLAlchemyStorage` 使用 `assistant/conf/app_config.yaml` 的 `postgresql` 连接，直接维护框架的会话、消息和状态表，启动时创建缺失的数据表。

应用层负责 ERPNext 登录核验、站点／用户身份映射、物料工具、页面及附件输入和聊天展示协议。每轮通过框架工具工厂绑定当前用户的 ERPNext 客户端，登录凭据不写入状态或消息总线。模型通过框架凭据工厂装配，数据库仅保存配置引用；模型密钥从环境变量读取，支持通过 `assistant/conf/.env` 配置。模型参数在 `assistant/conf/app_config.yaml` 配置，调用协议为 Chat Completions。

消息总线使用框架的 `InMemoryMessageBus`，服务以单 worker 启动。关闭页面只结束订阅，后台任务继续执行；服务重启后可加载已保存的对话继续提问，不会自动续跑中断任务。采购 Agent 使用框架装配的完整工具集，并加入物料查询工具；大结果可由框架卸载到工作目录。工具自动转为后台任务的中间件不启用，工具结果在当前运行中等待；定时任务调度器不启用，框架管理路由不开放。

执行环境使用 AgentScope 的 `DockerWorkspaceManager`，按用户分配容器。同一站点用户的会话共享容器；不同站点或用户使用不同容器。Bash 和内置文件工具在容器内执行；对应的 `assistant/data/workspaces/<workspace_id>/` 挂载为 `/workspace`，不提交 Git。每个会话首次执行时创建 `/workspace/sessions/<session_id>/work/`，作为 Bash 和文件搜索的默认目录，并向模型提供该路径。Agent 可通过绝对路径或相对路径访问同一容器内的其他目录；文件工具对绝对路径的要求遵循框架接口。同一用户的并发会话使用各自的工具后端和默认目录，不改变共享容器的工作目录。

框架负责镜像构建与缓存、容器创建和空闲回收，应用退出时释放容器；挂载的工作文件保留，后续执行可重新创建容器并读取文件。删除会话时，框架清理该会话的目录及内部状态，同一用户的其他会话文件保留。`workspace` 配置指定基础镜像、Node.js 版本、额外 Python 包和回收时间。ERPNext 查询工具在 Assistant 服务中执行，数据库密码、模型密钥和 ERPNext 登录凭据不传入工作空间容器。

## 功能

- Buying 页面右下角的常驻入口和对话面板，样式跟随 ERPNext 主题。
- 点击会话标题展开浮层菜单，支持新建、切换、删除会话；点击外部或按 Esc 收起。
- 流式回复、发送／停止切换、错误提示和重试。关闭面板只断开订阅，重新打开可读取历史并恢复订阅。
- 可选携带当前页面和未保存表单内容；服务端检查单据访问权限并过滤字段。
- `query_items` 工具通过 ERPNext 原生接口查询当前用户有权访问的物料，结果可以跳转到 Item 页面。
- Frappe 同源接口转发普通响应和事件流；Assistant 向 ERPNext 核实登录身份，会话按站点和用户归属隔离。
- 日志、请求追踪和统一异常处理。
- 附件上传、文本提取、PDF 解析和图片输入。

业务接口优先复用 ERPNext 原生 API。Frappe App 按需补充页面集成和业务能力，不重复包装全部业务接口。Agent 层负责模型和工具装配，服务层负责会话及事件转换，客户端层负责外部连接。

会话命名与 AgentScope 的 `session` 一致。Assistant 的单入口为 `POST /sessions`，通过 `action` 指定操作，`session_id` 标识会话；Frappe 代理入口为 `/api/method/buying_ai.api.sessions`。ERPNext 登录身份通过 `sid` 传递。

## 附件与沙箱

**Bash 和内置文件工具使用 Docker 工作空间。附件链路使用 Frappe 文件存储与读取权限；按上传者隔离的附件存储及基于附件 ID 的工具读取链路尚未实现。**

工作空间容器仅挂载对应用户的工作空间，不挂载项目、Frappe 站点或 Docker socket。会话目录用于组织文件，同一用户的会话可以访问彼此的文件。容器使用 Docker 默认网络，网络访问策略和资源配额尚未配置；文件权限确认由框架处理，操作确认界面尚未实现。

### 附件链路

浏览器调用 Frappe 自带的上传接口，把文件保存为站点私有附件。文件本体位于宿主机挂载目录：

```text
frappe_docker/development/frappe-bench/
sites/development.localhost/private/files/
```

文件名、上传者等元数据保存在 MariaDB 的 `File` 记录中。发送消息时，`frappe_app/context.py` 检查读取权限、读取文件并以 Base64 传给 Assistant；Assistant 提前提取文本、解析 PDF 或组织图片输入。

这个实现检查的是 Frappe 读取权限，不等于“只能访问自己上传的附件”。它也没有给 Agent 提供按需读取、解析附件的工具链路。

### 目标附件链路

目标方案由 Assistant 管理附件上传、存储与访问：

1. 用户上传附件，Assistant 通过已核实的登录身份确定上传者及所属会话。
2. 原始文件保存到按用户隔离的持久附件存储；本次会话授权的文件进入对应沙箱工作目录。
3. 消息只携带附件 ID、文件名、类型等元信息，并告诉 Agent 当前有哪些可用附件。
4. Agent 按需通过工具读取、解析或处理附件。
5. 需要保留的处理结果通过受控接口保存到附件存储，并关联用户和会话，供用户下载或继续使用。

附件的持久存储与沙箱工作目录分开。原文件和处理结果的存储周期独立于沙箱生命周期；上传失败、结果保存、会话删除和文件清理需有明确的生命周期管理。

### 用户权限与执行隔离

目标是每个用户使用独立沙箱，并绑定经过认证的所属用户：

- 用户身份来自服务端认证上下文，不使用模型或浏览器自报的用户名作为授权依据。
- 附件上传、关联、读取、下载、修改和删除，都校验当前用户与会话归属。
- 沙箱仅获得所属用户获准使用的文件，不挂载 Frappe 完整站点目录、宿主机项目目录或其他用户文件；同一用户的会话可共享工作空间内的文件。
- Bash 和其他文件工具都在同一沙箱权限边界内执行，不提供可绕过沙箱的宿主机执行入口。
- ERPNext 会话凭证、数据库密码和模型密钥留在可信服务端，不暴露给沙箱内执行的命令。
- 沙箱网络访问需要控制，避免绕过工具接口访问内部服务；解析缓存和临时文件也按用户、会话隔离。

执行隔离使用框架的 Docker 工作空间。附件持久存储、文件授权与容器资源限制和网络策略需要结合上述目标补充。

### 待实现功能

- 实现 Assistant 附件上传接口、附件元数据管理和基于附件 ID 的消息协议。
- 增加 Assistant 附件存储和用户／会话授权。
- 将获准使用的附件放入会话工作空间，并接入结果保存和下载接口。
- 验证跨用户、跨会话访问被拒绝，Bash 无法访问沙箱外文件，重连和清理不会丢失应保留的数据。

Markdown 渲染、工具执行卡片和操作确认等交互尚未实现。

## 本地开发

在仓库根目录通过统一的 `frappe_app/docker/compose.yaml` 启动 ERPNext 和 Assistant 所需的 PostgreSQL：

```bash
git submodule update --init --recursive
docker compose -f frappe_app/docker/compose.yaml up -d
docker compose -f frappe_app/docker/compose.yaml logs -f frappe
```

访问 `http://development.localhost:8000`。首次初始化的开发账号为 `Administrator`，密码为 `admin`；已有站点不会重置密码。

`frappe_app/docker/start.sh` 按功能组织初始化、依赖安装、配置、站点准备和服务启动。Bench 与站点不存在时创建；依赖文件或工具版本变化时重新安装依赖；每次启动执行类型检查、前端构建和缓存刷新。无需手动进入容器初始化。

容器仅挂载 `frappe_app/`、`frappe_docker/development/` 和只读的 `frappe_app/docker/`。本机修改前端源码后，Bench 自动重新构建，刷新浏览器查看；修改构建入口时需要重启监听。`assistant/` 不挂载到 Frappe 容器。

配置并启动 Assistant：

1. 首次将 `assistant/conf/.env.example` 复制为 `assistant/conf/.env`，填写所用模型密钥和数据库密码；数据库凭据需与 `frappe_app/docker/compose.yaml` 中的 `postgres` 服务一致。
2. 在 `assistant/conf/app_config.yaml` 中选择模型，确认 ERPNext、数据库、服务地址和 `workspace` 配置。Assistant 运行账户需要访问本机 Docker 服务；首次使用工作空间时，框架会拉取基础镜像并构建工具镜像，需要能访问镜像仓库和包源。
3. 在仓库根目录执行：

```bash
uv sync --directory assistant --locked
uv run --directory assistant python main.py
```

运行回归测试和静态检查（测试使用模拟模型、隔离 SQLite 和本地测试工作空间，不调用真实模型或 ERPNext；真实 Docker 测试默认跳过）：

```bash
uv run --directory assistant python -m pytest -q
uv run --directory assistant ruff check app main.py tests
uv run --directory assistant pyright
npm --prefix frappe_app run typecheck
```

运行真实 Docker 集成测试，验证用户容器隔离、会话默认目录、跨会话文件访问、文件恢复、会话目录清理和容器释放：

```bash
RUN_DOCKER_TESTS=1 uv run --directory assistant python -m pytest -q tests/test_workspaces.py
```

Frappe 通过 Compose 中的 `BUYING_AI_AGENT_URL` 访问宿主机 Assistant。Assistant 默认端口为 `8100`，通过配置的 ERPNext 地址核实用户并查询业务数据。真实模型能力需要配置后联调；助手服务不可用不影响 ERPNext 本身使用。

数据库卷、站点文件、依赖目录、构建产物和 `.env` 不随 Git 同步。多设备开发通过源码和锁文件保持一致，每台设备独立准备运行环境；停止 Compose 时不加 `-v` 可保留命名数据卷。
