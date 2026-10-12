"""在独立目录验证运行模块、原生模型工厂和客户端上下文。"""

import asyncio
import json
import sys
from collections import Counter
from contextlib import AsyncExitStack
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from agentscope.agent import InjectionConfig
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import IsolationPolicy, LocalWorkspaceManager
from agentscope.mcp import MCPClient
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel

sys.path.insert(0, str(Path.cwd()))
runtime = import_module("portable_runtime")


def stream(delta: dict[str, Any], reason: str) -> httpx.Response:
    """提供原生 Chat Completions 流响应。"""
    chunk = {
        "id": "completion",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
    }
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
    )


async def main() -> None:
    """并行运行两个模型配置相同名称的实例，再中断、恢复和取消。"""
    assert "app" not in sys.modules
    root = Path.cwd()
    (root / "prompt.md").write_text("你是一个工具助手。")
    skill_dir = root / "skills" / "greeting"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: greeting\ndescription: Greeting guide\n---\nRead references/style.txt"
    )
    (skill_dir / "references" / "style.txt").write_text("Be concise.")
    server = root / "mcp_server.py"
    server.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        'mcp = FastMCP("greeting")\n'
        '@mcp.tool(annotations={"readOnlyHint": True})\n'
        'def greet() -> str:\n    return "hello-from-mcp"\n'
        "mcp.run()\n"
    )
    calls: Counter[str] = Counter()
    entered = {name: asyncio.Event() for name in ("alpha", "beta")}
    clients = []
    storages: dict[str, AsyncSQLAlchemyStorage] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        """校验实例配置与工具结果，暂停第二次模型请求供中断。"""
        payload = json.loads(request.content)
        name = payload["model"]
        assert request.headers["authorization"] == f"Bearer {name}-secret"
        calls[name] += 1
        if calls[name] == 1:
            return stream(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "echo-1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": "{}"},
                        }
                    ]
                },
                "tool_calls",
            )
        results = [item for item in payload["messages"] if item["role"] == "tool"]
        assert results and f"{name}-resource" in str(results[0]["content"])
        if calls[name] == 2:
            mcp_name = next(
                tool["function"]["name"]
                for tool in payload["tools"]
                if "greet" in tool["function"]["name"]
            )
            return stream(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "greet-1",
                            "type": "function",
                            "function": {"name": mcp_name, "arguments": "{}"},
                        }
                    ]
                },
                "tool_calls",
            )
        assert "hello-from-mcp" in str(results)
        if calls[name] == 3:
            entered[name].set()
            await asyncio.Event().wait()
        return stream({"content": f"{name}-done"}, "stop")

    initialize = OpenAIChatModel.__init__

    def connect(self: OpenAIChatModel, **kwargs: Any) -> None:
        """为每个原生提供商客户端注入独立的模拟传输。"""
        kwargs["client_kwargs"] = {
            **kwargs.get("client_kwargs", {}),
            "http_client": httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        }
        initialize(self, **kwargs)
        clients.append(self.client)

    with patch.object(OpenAIChatModel, "__init__", connect):
        async with AsyncExitStack() as stack:
            instances = []
            for name in entered:
                definitions = runtime.AgentDefinitions.model_validate(
                    {
                        "default": "helper",
                        "models": {
                            "shared-key": {
                                "credential": {
                                    "type": "openai_credential",
                                    "api_key": f"{name}-secret",
                                    "base_url": "https://model.invalid",
                                },
                                "model": name,
                                "client_kwargs": {"max_retries": 0},
                                "image_inputs": False,
                                "context_size": 32768,
                                "params": {},
                            }
                        },
                        "agents": {
                            "helper": {
                                "name": "通用助手",
                                "description": "独立项目中的助手",
                                "prompt": "prompt.md",
                                "model": "shared-key",
                                "builtin_tools": [],
                                "tool_factories": ["external_tools:create_echo"],
                                "mcps": ["greeting"],
                                "skills": ["greeting"],
                                "members": [],
                                "context": {},
                                "react": {},
                            }
                        },
                    }
                )
                catalog = runtime.AgentCatalog(
                    definitions=definitions,
                    mcps={
                        "greeting": MCPClient.model_validate(
                            {
                                "name": "greeting",
                                "is_stateful": True,
                                "mcp_config": {
                                    "type": "stdio_mcp",
                                    "command": sys.executable,
                                    "args": [str(server)],
                                },
                            }
                        )
                    },
                    base_dir=root,
                    skills_dir=root / "skills",
                )
                storage = storages[name] = AsyncSQLAlchemyStorage(
                    f"sqlite+aiosqlite:///{root / name}.db"
                )
                app = runtime.create_runtime(
                    storage,
                    catalog=catalog,
                    message_bus=InMemoryMessageBus(),
                    workspace_manager=LocalWorkspaceManager(
                        str(root / name), isolation=IsolationPolicy.PER_USER
                    ),
                    injection_config=InjectionConfig(timezone="UTC"),
                    context_ttl_seconds=60,
                )
                await stack.enter_async_context(app)
                row = await app.create("opaque-user")
                instances.append((app, row, name))

            # 同一会话的并发开始在运行模块内串行检查。
            state, row, name = instances[0]
            results = await asyncio.gather(
                *(
                    state.send(
                        "opaque-user",
                        row.id,
                        UserMsg(name="user", content="查询"),
                        {"value": f"{name}-resource"},
                    )
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
            failures: list[Any] = [
                item for item in results if isinstance(item, runtime.RunError)
            ]
            assert len(failures) == 1 and failures[0].code == "conflict"
            state, row, name = instances[1]
            await state.send(
                "opaque-user",
                row.id,
                UserMsg(name="user", content="查询"),
                {"value": f"{name}-resource"},
            )
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in entered.values())), 10
            )
            for state, row, name in instances:
                await state.interrupt(
                    "opaque-user",
                    row.id,
                    {"value": f"{name}-resource"},
                )
                assert (await state.messages("opaque-user", row.id))["resumable"]
                await state.resume("opaque-user", row.id, {"value": f"{name}-resource"})
                await state.wait("opaque-user", row.id)
                messages = (await state.messages("opaque-user", row.id))["messages"]
                assert (
                    len([message for message in messages if message.role == "user"])
                    == 1
                )
                assert messages[-1].get_text_content() == f"{name}-done"
                await state.cancel("opaque-user", row.id)
                assert not (await state.messages("opaque-user", row.id))["resumable"]
                records = await storages[name].list_credentials("opaque-user")
                assert "secret" not in str([record.data for record in records])
                workspace = await state.workspace_manager.get_workspace(
                    "opaque-user", row.agent_id, row.id, row.config.workspace_id
                )
                skills = await workspace.list_skills(agent_id=row.agent_id)
                assert [skill.name for skill in skills] == ["greeting"]
                assert (
                    Path(skills[0].dir) / "references" / "style.txt"
                ).read_text() == "Be concise."
    assert clients and all(client.is_closed() for client in clients)
    assert "app" not in sys.modules


if __name__ == "__main__":
    asyncio.run(main())
