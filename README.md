# Buying AI · ERPNext 采购助手

在 ERPNext 的 Buying 页面中嵌入对话助手，让用户结合当前单据、未保存的表单内容和附件进行查询与分析。采购业务由 ERPNext 提供。

## 项目组成

| 目录             | 职责                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------- |
| `frappe_app/`    | Frappe App，内部名称为 `buying_ai`。提供助手界面、ERPNext 登录身份衔接、页面上下文和请求转发。 |
| `assistant/`     | 独立的 Agent 服务，运行在宿主机。负责身份核实、会话管理、模型与工具调用。                      |
| `frappe_docker/` | 官方 Docker 仓库子模块，提供 Frappe / ERPNext 开发环境基础。                                   |

## 技术架构

| 层次       | 技术与职责                                                                        |
| ---------- | --------------------------------------------------------------------------------- |
| 前端       | React + TypeScript，源码位于 `frappe_app/public/frontend/`，由 Bench 构建和监听。 |
| Assistant  | FastAPI，负责 ERPNext 身份核验、业务工具和输入处理。                              |
| Agent 运行 | AgentScope 2，负责推理、工具调用、Agent 间通信和运行资源管理。                        |
| 持久化     | AgentScope `AsyncSQLAlchemyStorage` 将会话、消息和状态保存到 PostgreSQL。         |
| 事件分发   | 原生 SSE 与 `InMemoryMessageBus`，Assistant 使用单 worker。                       |
| 执行环境   | `DockerWorkspaceManager` 按用户管理 Docker 容器。                                 |

### 消息与模型

- 聊天历史和发送响应使用 AgentScope 原生 `Msg`、`ChatTriggerResponse`。
- 前端使用官方 TypeScript SDK 的 `appendEvent()` 累积消息；每个选中会话保持一条 SSE 订阅。
- 关闭页面只结束订阅，后台任务继续执行。服务重启后可手动恢复已保存的上下文。
- 模型由 AgentScope 原生凭据工厂选择，连接参数在 `assistant/conf/agents.yaml` 的 `models` 中配置。DeepSeek 和 OpenAI 使用 Chat Completions。
- 模型密钥从环境变量或 `assistant/conf/.env` 读取；数据库仅保存模型配置引用。
- ERPNext 客户端按认证用户绑定，登录凭据只保留在服务端内存。

模型配置使用原生字段：

- `credential.type` 选择提供商：`deepseek_credential` 对应 `DeepSeekChatModel`，`openai_credential` 对应 `OpenAIChatModel`。其他提供商使用框架对应的凭据类型，并安装所需 SDK。
- `client_kwargs` 配置提供商 SDK 的连接选项。
- `params` 使用所选模型的 `Parameters` 字段，例如 DeepSeek 的 `thinking_enable`、`reasoning_effort` 和 `max_tokens`。

### 工作空间

| 项目     | 行为                                                              |
| -------- | ----------------------------------------------------------------- |
| 容器归属 | 同一站点、同一用户的会话共享容器，不同用户或站点相互隔离。        |
| 持久目录 | `assistant/data/workspaces/<workspace_id>/` 挂载为 `/workspace`。 |
| 会话目录 | `/workspace/sessions/<session_id>/`，作为当前会话工具的默认目录。 |
| 文件访问 | Agent 可以访问同一用户容器内其他会话的目录。                      |
| 容器回收 | 框架负责创建和空闲回收；应用退出时释放容器，挂载文件保留。        |
| 会话删除 | 清理该会话目录和内部状态，保留同一用户其他会话的文件。            |

`workspace` 配置控制基础镜像、Node.js 版本、额外 Python 包和回收时间。Bash 与内置文件工具在容器内运行；ERPNext 查询工具在 Assistant 服务中运行。

Bash 的相对路径以会话目录为起点；Read、Write、Edit 使用绝对路径，当前会话目录由提示词提供。Glob、Grep 默认搜索当前会话目录。

工具执行超过 10 秒时，框架将其转入后台，结果返回后自动继续推理。后台工具计入任务运行状态；中断或取消会停止工具并等待收尾。工具大结果由框架卸载到工作目录。

恢复运行会加载保存的上下文；服务重启后，未完成的后台工具不会自动恢复执行。

## 功能

- Buying 页面右下角的常驻入口和对话面板，样式跟随 ERPNext 主题。
- 点击会话标题展开浮层菜单，支持新建、切换、删除会话；点击外部或按 Esc 收起。
- 流式回复、开始／中断／恢复／取消、原生工具权限确认、成员 Agent 状态和错误提示。关闭面板只断开订阅，重新打开可读取历史并恢复订阅。
- 可选携带当前页面和未保存表单内容；服务端检查单据访问权限并过滤字段。
- `query_items` 工具通过 ERPNext 原生接口查询当前用户有权访问的物料，结果可以跳转到 Item 页面。
- Frappe 同源接口转发普通响应和事件流；Assistant 向 ERPNext 核实登录身份，会话按站点和用户归属隔离。
- 日志、请求追踪和统一异常处理。
- 附件上传到会话工作空间，工具按需读取文件，支持图片输入。

业务接口优先复用 ERPNext 原生 API。Frappe App 按需补充页面集成和业务能力，不重复包装全部业务接口。Agent 层负责模型和工具装配，服务层负责会话业务及原生事件订阅，客户端层负责外部连接。

会话命名与 AgentScope 的 `session` 一致。Assistant 的单入口为 `POST /sessions`，通过 `action` 指定操作，`session_id` 标识会话；Frappe 代理入口为 `/api/method/buying_ai.api.sessions`。ERPNext 登录身份通过 `sid` 传递。

## Agent 模板与扩展

通用运行模块不依赖采购角色；角色配置直接引用工具工厂的 Python 路径，采购助手 Agent 及其协作 Agent 使用同一运行基础。

```text
assistant/
├── app/config/              # app、agents、mcp 配置定义、加载与校验
├── app/runtime/              # 角色目录、任务上下文、模型资源、工作空间及框架装配
├── app/tools/                # 业务工具和工具工厂
├── app/services/
│   ├── agents.py            # 用户角色与模型配置登记
│   ├── sessions.py          # 认证用户的会话操作
│   ├── runs.py              # 开始、中断、恢复、取消及工具确认
│   └── teams.py             # 原生 Agent 团队范围及状态汇总
├── conf/agents.yaml         # 模型定义、默认角色与各角色的能力引用
├── conf/mcp.yaml            # AgentScope 原生 MCPClient 配置
└── resources/
    ├── prompts/             # procurement.md、item_researcher.md 等角色提示词
    └── skills/              # 完整 Skill 目录，含 SKILL.md 和辅助文件
```

`agents.yaml` 在顶层 `models` 定义可用模型，每个 Agent 的 `model` 必须引用其中一个配置名。各角色声明名称、说明、提示词文件、工作空间工具、业务工具工厂、MCP 服务、Skill、可邀请的协作 Agent 和运行策略。`context` 与 `react` 直接使用 AgentScope 的 `ContextConfig` 和 `ReActConfig`。启动时校验全部能力引用及 Skill 元数据，所有应用配置须显式提供。

当前默认入口为 `procurement`，负责采购查询与附件分析；`item_researcher` 负责物料查询。团队中的负责人和成员均为 Agent。

负责人 Agent 按任务需要调用原生 `TeamCreate`、`AgentInvite`、`TeamSay` 和 `TeamDelete`；成员 Agent 调用 `TeamSay` 报告。角色的 `members` 限制可邀请的 Agent，动态创建角色不开放。简单任务无需组建 Agent 团队。

框架 `AgentInvite` 工具从成员 Agent 自己的会话读取模型和工作空间，因此每个可邀请的 Agent 角色拥有一个原生配置参考会话。参考会话不运行任务，也不进入用户会话列表；成员 Agent 使用独立的原生团队会话，解散 Agent 团队只删除这些会话，预定义角色继续保留。

新增 Agent 的步骤：

1. 在 `resources/prompts/` 编写提示词，在 `app/tools/` 编写业务工具。工厂接收 `RunContext`，从 `clients` 获取经过认证的业务客户端，返回一个框架原生工具。
2. 在 `conf/agents.yaml` 声明角色及所需能力，`tool_factories` 使用 `Python模块:函数名`，如 `app.tools.items:create_items_tool`；将可协作 Agent 的配置名填入负责人 Agent 的 `members`。
3. 需要 MCP 时在 `conf/mcp.yaml` 配置原生连接，在角色的 `mcps` 引用服务名。支持 STDIO 和 HTTP，工具启用范围、连接及执行超时均使用原生 MCPClient 字段。
4. 需要 Skill 时在 `resources/skills/<名称>/` 放置 `SKILL.md` 及辅助文件。frontmatter 的 `name` 必须与目录名一致，并提供 `description`；在角色的 `skills` 引用名称。框架将完整目录复制到该用户角色的 Skill 分区并提供加载工具。

HTTP MCP 示例；框架客户端的 `name` 自动使用外层服务配置名：

```yaml
servers:
  documents:
    is_stateful: false
    mcp_config:
      type: http_mcp
      url: https://example.com/mcp
      headers:
        Authorization: ${oc.env:DOCUMENTS_MCP_AUTHORIZATION}
    enable_tools: [search]
    execution_timeout: 30
```

`servers: {}` 表示没有外部 MCP 服务。连接配置和资源持久化由框架工作空间管理，MCP 的静态凭据会保存在相应连接配置中；ERPNext 登录客户端只保留在可信服务端内存，不作为静态 MCP 凭据写入工作空间。

## 任务运行语义

| action                       | 行为                                                                   |
| ---------------------------- | ---------------------------------------------------------------------- |
| `create`                     | 使用配置中的默认入口角色创建会话。                                     |
| `send`                       | 用新用户输入开始运行，并重新绑定认证客户端。                           |
| `interrupt`                  | 中断负责人 Agent 及当前成员 Agent，等待框架保存上下文。               |
| `resume`                     | 读取已保存上下文，通过原生空输入继续推理，不补发用户消息。             |
| `cancel`                     | 结束当前任务并解散 Agent 团队，保留负责人 Agent 对话历史；继续使用会话须发送新消息。 |
| `confirm`                    | 提交原生 `UserConfirmResultEvent`，恢复对应 Agent 的工具调用。          |
| `messages`                   | 读取原生消息、Agent 团队状态、待确认调用及可恢复条件。                  |
| `upload`                     | 将文件写入当前会话工作空间，返回文件名和路径。                         |
| `subscribe`                  | 订阅原生 SSE 事件长连接。                                              |
| `list` / `rename` / `delete` | 管理用户会话；删除同时停止整个任务并清理其数据。                       |

恢复采用保存上下文后继续推理的语义，不恢复任意 Python 指令位置，也不重新运行已经完成的成员 Agent。框架负责工具调用与结果配对、权限等待、后台任务、原生 Agent 间通信、事件发布和 SQL 持久化。

应用将运行意图保存在框架 `AgentState.middle_context` 的 `app.run_control` 中，取值为 `active`、`interrupted` 或 `cancelled`。这项状态用于阻止停止后的 Agent 团队消息自动唤醒模型，团队提示保留为原生上下文供恢复使用；没有额外的运行表或自定义聊天事件格式。

发送、恢复和确认都经过 ERPNext 身份核验。成员 Agent 通过原生团队归属读取负责人 Agent 会话绑定的客户端，沿用发起任务的用户身份访问 ERPNext；`runtime.context_ttl_seconds` 限制登录上下文使用时长，失效或服务重启后须由认证请求重新绑定。模型客户端在请求触发及框架独立唤醒任务结束后释放，应用关闭时等待清理完成。多进程消息总线及跨进程认证上下文提供器不在当前单 worker 实现范围内。

## 附件与沙箱

用户上传的附件直接保存到所属用户的 Docker 工作空间，不创建 Frappe `File` 记录。

### 上传与读取

1. 浏览器通过 Frappe 同源接口上传文件，Assistant 核验 ERPNext 登录身份及会话归属。
2. 文件保存到 `/workspace/sessions/<session_id>/attachments/<文件名>`；同一会话内同名上传直接覆盖。
3. 发送消息时提交文件名，服务端在当前会话的附件目录查找文件，将名称、类型和完整沙箱路径提供给 Agent。
4. 文本、PDF 等文件由 Agent 使用工具按需读取和解析；沙箱默认安装 `pypdf`。支持图片输入的模型同时接收原生图片内容块。

### 存储与权限

- 文件通过 `assistant/data/workspaces/<workspace_id>/` 持久化，容器回收后仍保留。
- 上传与消息附件引用检查当前用户和会话归属；同一用户的其他会话可以通过沙箱工具访问文件。
- 删除会话会清理该会话的附件；从待发送列表移除附件只取消本次引用，文件保留到会话删除。
- 容器仅挂载当前用户工作空间，不挂载项目、Frappe 站点或 Docker socket。ERPNext 凭证、数据库密码及模型密钥保留在服务端。
- 容器使用 Docker 默认网络，尚未配置网络访问策略和资源配额。

处理结果可保存在工作空间中；面向用户的文件下载接口尚未实现。Markdown 富文本和完整工具执行卡片不在当前面板功能范围内。

## 本地开发

在仓库根目录通过统一的 `frappe_app/docker/compose.yaml` 启动 ERPNext 和 Assistant 所需的 PostgreSQL：

```bash
git submodule update --init --recursive
docker compose -f frappe_app/docker/compose.yaml up -d
```

访问 `http://development.localhost:8000`。首次初始化的开发账号为 `Administrator`，密码为 `admin`；已有站点不会重置密码。

`frappe_app/docker/start.sh` 自动初始化 Bench 和站点、安装依赖并构建前端。修改前端源码后，Bench 自动重新构建，刷新浏览器即可查看。

配置并启动 Assistant：

1. 首次将 `assistant/conf/.env.example` 复制为 `assistant/conf/.env`，填写所用模型密钥和数据库密码；数据库凭据需与 `frappe_app/docker/compose.yaml` 中的 `postgres` 服务一致。
2. 在 `assistant/conf/app.yaml` 中确认 ERPNext、数据库、服务地址、`runtime` 和 `workspace` 配置；在 `agents.yaml` 定义模型并配置各 Agent 的模型和能力，按需要填写 `mcp.yaml` 和 Skill 目录。Assistant 运行账户需要访问本机 Docker 服务；首次使用工作空间时，框架会拉取基础镜像并构建工具镜像，需要能访问镜像仓库和包源。
3. 在仓库根目录执行：

```bash
uv sync --directory assistant --locked
uv run --directory assistant python main.py
```

Frappe 通过 Compose 中的 `BUYING_AI_AGENT_URL` 访问宿主机 Assistant。Assistant 默认端口为 `8100`，通过配置的 ERPNext 地址核实用户并查询业务数据。真实模型能力需要配置后联调；助手服务不可用不影响 ERPNext 本身使用。
