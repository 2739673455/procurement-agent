# Buying AI · ERPNext 采购助手

嵌入 ERPNext Buying 页面，结合当前单据、未保存的表单和附件进行查询与分析。请求通过 Frappe 同源接口转发到独立的 FastAPI 服务，由 AgentScope 执行模型、工具和团队协作。

| 目录             | 职责                                                            |
| ---------------- | --------------------------------------------------------------- |
| `frappe_app/`    | Frappe App `buying_ai`，包含 React 界面、页面上下文和请求转发。 |
| `assistant/`     | Agent 服务，负责身份核验、会话管理、模型和业务工具。            |
| `frappe_docker/` | 官方 Docker 仓库子模块，提供 ERPNext 开发环境。                 |

## 本地开发

需要 Docker 和 uv。Assistant 在宿主机运行，运行账户须能访问本机 Docker 服务。首次准备环境：

```bash
git submodule update --init --recursive
docker compose -f frappe_app/docker/compose.yaml up -d
cp assistant/conf/.env.example assistant/conf/.env
```

填写 `.env` 中的模型密钥和数据库密码；数据库凭据须与 Compose 的 `postgres` 服务一致。确认 `assistant/conf/app.yaml` 中的 ERPNext、数据库和服务地址后启动：

```bash
uv sync --directory assistant --locked
uv run --directory assistant python main.py
```

ERPNext：`http://development.localhost:8000`，首次初始化账号为 `Administrator` / `admin`。Assistant 默认端口为 `8100`，Frappe 通过 `BUYING_AI_AGENT_URL` 连接。Bench 自动构建并监听 `frappe_app/public/frontend/`；修改后刷新浏览器。

## runtime 文件依赖

`assistant/app/runtime/` 使用包内相对导入，配置与外部资源由宿主提供。下面的箭头表示**直接导入**，虚线表示 `__init__.py` 的公开导出。

入口与装配：

```mermaid
flowchart LR
    public["__init__.py<br/>公开导出"]
    bootstrap["bootstrap.py<br/>装配与生命周期"]
    runtime["runtime.py"]
    agents["agents.py"]
    runs["runs.py"]
    teams["teams.py"]
    catalog["catalog.py"]
    context["context.py"]
    models["models.py"]
    resources["resources.py"]
    workspaces["workspaces.py"]
    offloading["offloading.py"]
    config["config.py"]
    errors["errors.py"]

    public -.-> bootstrap & runtime & catalog & config & context & errors
    bootstrap --> runtime & agents & runs & teams
    bootstrap --> catalog & context & models
    bootstrap --> resources & workspaces & offloading
```

其余模块的依赖与职责：

```mermaid
flowchart TB
    runtime["runtime.py<br/>会话入口"]
    runs["runs.py<br/>运行控制"]
    agents["agents.py<br/>角色、凭据与模型参数登记"]
    resources["resources.py<br/>角色工具、MCP 与 Skill 装配"]
    catalog["catalog.py<br/>能力目录与工具工厂"]
    models["models.py<br/>运行设置与客户端释放"]
    context["context.py<br/>认证客户端绑定与有效期"]
    workspaces["workspaces.py<br/>会话工具工作目录"]
    teams["teams.py<br/>团队与后台任务状态"]
    config["config.py<br/>配置类型与校验"]
    errors["errors.py<br/>运行异常"]

    runtime --> agents & catalog & context & errors & runs & teams
    runs --> context & errors & teams
    agents --> catalog
    resources --> catalog & config
    catalog --> config & context & errors
    models --> config
    context --> errors
    workspaces --> errors
```

`config.py`、`errors.py`、`teams.py`、`offloading.py` 没有包内导入；`offloading.py` 负责前台工具被中断时的取消收尾。图中省略第三方库和按配置动态加载的业务工具。

迁移时整包复制，依赖 `agentscope[service,storage-sql]>=2.0.9,<2.1`、FastAPI 和 Pydantic；Docker 工作空间另需 `workspace-docker` 扩展。部分装配使用 AgentScope 内部 API。宿主提供 `AgentCatalog`、原生存储、消息总线、工作空间管理器、`InjectionConfig` 和上下文有效期，通过 `create_runtime()` 创建实例，以 `async with runtime` 管理生命周期。

用户标识由宿主认证后传入；发送、恢复、确认、中断、取消和删除须显式提供 `clients`，无业务客户端时传 `{}`。工具通过 `RunContext.clients` 访问客户端，宿主将 `RunError.code` 转为接口错误。

## 配置与扩展

| 位置                           | 用途                                            |
| ------------------------------ | ----------------------------------------------- |
| `assistant/conf/app.yaml`      | 服务、ERPNext、数据库、工作空间和上下文有效期。 |
| `assistant/conf/agents.yaml`   | 模型、默认入口、角色能力及允许邀请的成员。      |
| `assistant/conf/mcp.yaml`      | 原生 MCPClient 连接；角色通过 `mcps` 引用。     |
| `assistant/resources/prompts/` | 角色提示词。                                    |
| `assistant/resources/skills/`  | Skill 目录，包含 `SKILL.md` 及辅助文件。        |
| `assistant/app/tools/`         | 业务工具工厂。                                  |

模型配置中，`credential.type` 选择提供商，`params` 设置原生生成参数，无附加参数时填写 `{}`。加载时解析为原生凭据和参数对象；创建用户会话时，将凭据（含模型密钥）及序列化参数保存到框架存储。密钥从环境变量或 `.env` 读取，后续运行由框架从存储直接创建原生模型。

`context_size` 显式覆盖原生模型卡及构造器的上下文预算，`image_inputs` 控制图片输入。`client_kwargs` 通过 SDK 的 `with_options()` 应用超时、重试等连接选项；模型层不额外重试。原生凭据的 `name` 对应宿主模型配置名，用于在 Agent 装配时关联这些运行设置。YAML 配置在启动时加载，发送和恢复使用会话中保存的原生模型配置，并应用宿主运行设置。

新增角色：编写提示词和工具工厂，在 `agents.yaml` 声明能力并引用 `models` 中的模型配置名。`tool_factories` 使用 `Python模块:函数名`，工厂接收 `RunContext` 并返回原生工具。需要协作时在 `members` 声明可邀请角色；`context`、`react` 分别使用原生 `ContextConfig`、`ReActConfig`。

Skill 的 frontmatter 须包含 `name` 和 `description`，其中 `name` 与目录名一致。模型、角色、工具工厂、MCP 和 Skill 引用在启动时校验。

## 会话与运行

Assistant 入口为 `POST /sessions`，Frappe 代理入口为 `/api/method/buying_ai.api.sessions`。使用 `action` 指定操作、`session_id` 标识会话、`sid` 核验 ERPNext 登录身份，会话按站点和用户隔离。

| action                       | 行为                                                                   |
| ---------------------------- | ---------------------------------------------------------------------- |
| `create` / `list` / `rename` | 创建、列出和重命名用户会话。                                           |
| `send`                       | 发送新输入并绑定认证客户端。                                           |
| `interrupt`                  | 中断当前回复或结束工具确认／外部结果等待，保留后台工具和成员 Agent。   |
| `resume`                     | 界面的「继续执行」：加载已保存上下文，通过原生空输入继续当前会话推理。 |
| `cancel`                     | 取消当前会话的回复及后台工具，保留上下文和团队；不结束工具确认等待。   |
| `confirm`                    | 提交原生工具权限确认结果。                                             |
| `messages` / `subscribe`     | 读取完整消息与运行状态／订阅原生 SSE 事件。                            |
| `upload`                     | 上传附件到会话工作空间。                                               |
| `delete`                     | 停止并删除会话及其团队资源。                                           |

关闭页面只断开订阅，任务继续执行。中断或取消不会冻结团队，成员消息和后台结果仍可原生唤醒会话。服务重启后，符合恢复条件的会话可点击「继续执行」；未完成的后台工具和成员任务不会因此重启。

业务客户端仅保存在服务端内存，成员沿用负责人会话绑定的用户身份。上下文过期或服务重启后须通过认证请求重新绑定；当前使用单 worker。

工作空间按站点和用户隔离，同一用户共享容器。宿主文件保存在 `assistant/data/workspaces/<workspace_id>/`，会话目录为 `/workspace/sessions/<session_id>/`，附件位于其中的 `attachments/`。Agent 可访问同一用户其他会话的文件；删除会话清理该会话目录，容器回收保留挂载文件。

Bash 和内置文件工具在容器执行，ERPNext 工具在服务端执行并遵守用户权限。工具运行超过 10 秒后由框架转入后台，结果可自动唤醒推理。`image_inputs` 启用时直接传入图片内容，其他附件由工具按需读取。

## 采购工具

采购入口在 `assistant/conf/agents.yaml` 登记工具工厂。物料查询成员仍只有 `query_items`；新增工具仅开放给采购入口。所有请求携带当前 ERPNext 用户身份，不使用管理员凭据。

| 批次 | 工具 | 行为 |
| --- | --- | --- |
| 查询 | `query_items`、`list_procurement_documents`、`get_procurement_document`、`get_procurement_schema`、`get_procurement_stock`、`run_procurement_report` | 采购对象白名单内的列表、详情、v16 元数据、Bin 库存和原生报表；新列表工具每页最多 200 条。 |
| 拟单 | `prepare_procurement_document` | 采购需求→询价/订单、询价→报价、报价→订单、订单→收货/发票、收货→发票/退货；返回未保存文档，保留原生来源行关联。 |
| 拟单 | `save_procurement_draft` | 新建或更新采购需求、询价、报价、订单、收货、发票、质检草稿；不会提交。 |
| 执行 | `get_procurement_actions`、`apply_procurement_workflow` | 读取并执行当前用户的原生工作流动作。 |
| 执行 | `submit_procurement_document`、`cancel_procurement_document` | 提交或取消单据；启用工作流时拒绝直接执行。 |
| 执行 | `change_purchase_order_status` | 暂停、关闭或恢复已提交订单。 |
| 执行 | `send_procurement_rfq` | 向已提交询价单中的指定已有地址发送询价，拒绝已发送行的重复发送。 |

### 操作约定

- 查询返回 ERPNext 原生 JSON；分页没有自动补齐。库存按库存单位理解，不能把没有可见 Bin 记录解释为零库存。报表使用原生筛选字段，预生成报表未完成时不能宣称已获得完整结果。
- 所有拟单和执行工具设置原生 `ASK` 权限；发送、恢复或确认请求重新绑定当前业务客户端。确认不是 ERP 审批，ERPNext 仍校验权限和业务条件。
- 草稿 `fields` 仅包含业务字段，禁止状态、所有者和内部标记。更新需读取最新 `modified` 并作为 `expected_modified` 提交；子表用完整目标列表，保留现有行 `name` 和来源关联，未提供的顶层字段保持不变。
- 服务器以原生 `frappe.client.save` 的版本校验保存草稿。执行动作通过 `buying_ai.procurement.execute` 在同一事务中锁定单据并检查版本，再调用原生方法；不支持任意方法路径、绕过审批或级联删除。
- ERPNext v16 的询价单提交会直接检查供应商行并发信。拟单时须显式将所有 `suppliers[].send_email` 设为 `0`；普通提交和工作流工具会拒绝仍启用自动发送的询价草稿。单独发送时，按已确认的 `recipients` 更新原生允许提交后修改的 `send_email`，再执行原生发送方法。该流程可能建立供应商门户访问，需预先配置门户和出站邮件；`email_queued` 不保证邮件已送达。
- 写操作超时或未获得成功响应时返回“结果未知”，不自动重试；先查询单据或通信记录再决定后续动作。此版本不提供跨请求幂等键，不能盲目重放新建或发送调用。
- Payment Entry 和应付数据仅供查询；本版本不执行实际付款，也不凭模型推测实际收货、质检或财务事实。

### 部署与验证

需要同步更新 Assistant 和 Frappe App Python 代码，并重启相应服务以加载新增 `buying_ai.procurement` 入口；不需要新增 DocType。Frappe 代理只向服务端传递当前会话的 `sid` 和 CSRF token，Assistant 将两者保存在内存客户端中。缺少 CSRF token 时只读操作仍可用，写操作会明确拒绝；不要通过关闭 CSRF 校验解决。

本实现依据 ERPNext/Frappe `version-16` 原生 REST、单据映射和工作流方法。新增 HTTP 合约测试、真实 AgentScope 确认流程测试及 Frappe 入口替身测试可通过 `uv run --directory assistant python -m pytest -q` 运行。替身测试不替代已安装 ERPNext 站点上的权限、并发、工作流、库存/财务记账和邮件联调；上线前应在测试站点用采购员、审批人及无权限用户验证这些行为。
