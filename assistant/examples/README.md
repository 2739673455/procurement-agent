# 独立 Runtime 示例

从 `assistant/` 运行（无 ERP、Frappe、Docker 或 PostgreSQL 依赖）：

```bash
uv sync --locked
export OPENAI_API_KEY=你的模型密钥
uv run python -m examples.standalone
```

示例使用真实模型，会产生提供商调用费用。SQLite 和本地工作空间保存在
`assistant/data/standalone/`。本地工作空间直接访问宿主文件系统，不提供容器隔离。
无模型密钥的验证入口是 `uv run python -m pytest -q tests/test_runtime_api.py tests/test_runtime_reuse.py`；
后者在隔离目录中使用模拟模型响应、真实工具、STDIO MCP 和 Skill 文件。

```python
from agentscope.message import UserMsg
from examples.standalone import build_runtime

async with build_runtime(directory, api_key) as runtime:
    row = await runtime.create("authenticated-user", agent="helper", title="Hello")
    await runtime.send(
        "authenticated-user",
        row.id,
        UserMsg(name="user", content="请向我问好"),
        clients={"greeting": "你好"},
    )
    await runtime.wait("authenticated-user", row.id)
    snapshot = await runtime.messages("authenticated-user", row.id)
```

`create_runtime(...)` 返回 `Runtime`。宿主只需一次 `async with runtime`，不用取得
FastAPI 的 `app.state`，也不用配置用户、分配工作空间或手动保存 `SessionRecord`。
每个 Runtime 实例只允许进入一次；重启时创建新实例并提供同一持久化存储和工作空间路径。
关闭流程与已进入的会话操作串行，先完成短操作，再停止框架运行并释放资源。
采购宿主在自己的 FastAPI lifespan 中使用同一入口。

公开操作：

- `create(user_id, agent=..., title=...)` 返回原生 `SessionRecord`，省略 agent 时使用目录默认值。
- `get`、`list`、`messages`、`rename`、`delete` 提供会话管理；`messages` 返回类型化 `SessionSnapshot`，包含原生消息、团队成员、后台工具、权限确认、状态和可恢复条件。
- `send` 接收原生 `Msg`；`interrupt`、`resume`、`cancel` 操作整个任务；`confirm` 接收原生 `UserConfirmResultEvent`。
- `wait` 捕获调用时的主会话运行并在操作锁外等待；取消等待者不会取消运行，也不等待后续新轮次，不保证整个团队或后台工具结束；使用快照的 `running` 和 `background_tasks` 判断任务状态。
- `subscribe` 返回框架原生 SSE `StreamingResponse`，可从宿主 HTTP 路由直接返回。断开订阅不会取消运行；响应应在 Runtime 生命周期内消费；HTTP 断开会释放该订阅的 feeder，内存消息总线关闭也会结束现有订阅。直接迭代响应的宿主应在提前停止时关闭迭代器，或取消并等待消费任务。
- `catalog` 和 `workspace_manager` 提供宿主输入适配所需的类型化资源；例如采购宿主自行处理附件与图片消息。

所有会话操作按 `user_id` 检查归属，隐藏成员会话和角色参考会话；身份认证仍由宿主负责。
`RunError.code` 包含 `not_found`、`invalid_input`、`conflict`、`context_expired` 和 `unavailable`，
HTTP 映射由宿主决定。传入的 `clients` 仅保存在进程内，宿主负责关闭这些客户端；
Runtime 负责模型客户端、框架后台任务、存储与工作空间的生命周期。

## 接入能力

保留现有声明结构，不新增 Agent 实现基类：

- 工具：在角色的 `tool_factories` 声明可导入的 `模块:工厂`；工厂接收 `RunContext`，返回原生工具。示例的 `create_greeting` 演示请求资源绑定。
- MCP：在 `AgentCatalog(mcps=...)` 传入名称到原生 `MCPClient` 的映射，并在角色 `mcps` 引用名称。客户端 `name` 必须与映射键一致。可使用 HTTP 或 STDIO 连接。
- Skills：向 `AgentCatalog(skills_dir=...)` 提供目录，在角色 `skills` 引用其子目录。每个子目录含带 `name`、`description` 的 `SKILL.md`；`name` 与目录一致。运行时验证元数据并安装完整目录。
- 团队：在 `members` 声明可邀请的角色，运行时自动登记原生 AgentInvite 所需的参考会话。

本轮仍是单进程运行包，依赖 AgentScope 2.0.x 的原生服务及内部事件接口；尚未拆分发布为独立发行包。
其他项目可复用 `app/runtime` 包和声明资源，不需要采购宿主的认证、配置加载器或会话编排。
