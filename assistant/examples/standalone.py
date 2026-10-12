"""无 ERP 的独立宿主：从 assistant/ 运行 python -m examples.standalone。"""

import asyncio
import os
from pathlib import Path

from agentscope.agent import InjectionConfig
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import IsolationPolicy, LocalWorkspaceManager
from agentscope.message import TextBlock, UserMsg
from agentscope.tool import FunctionTool, ToolChunk

from app.runtime import (
    AgentCatalog,
    AgentDefinitions,
    RunContext,
    Runtime,
    create_runtime,
)


def create_greeting(context: RunContext) -> FunctionTool:
    """宿主工具工厂可以使用每次请求绑定的客户端或其他资源。"""

    async def greeting() -> ToolChunk:
        """读取宿主提供的问候语。"""
        return ToolChunk(content=[TextBlock(text=context.clients["greeting"])])

    return FunctionTool(greeting, is_read_only=True)


def build_runtime(directory: Path, api_key: str) -> Runtime:
    """仅装配通用运行包和框架资源，不加载采购配置或 ERP 客户端。"""
    directory.mkdir(parents=True, exist_ok=True)
    definitions = AgentDefinitions.model_validate(
        {
            "default": "helper",
            "models": {
                "chat": {
                    "credential": {"type": "openai_credential", "api_key": api_key},
                    "model": "gpt-4.1-mini",
                    "client_kwargs": {},
                    "params": {},
                    "image_inputs": False,
                    "context_size": 32768,
                }
            },
            "agents": {
                "helper": {
                    "name": "Helper",
                    "description": "A standalone assistant",
                    "prompt": "standalone_prompt.md",
                    "model": "chat",
                    "tool_factories": ["examples.standalone:create_greeting"],
                    "builtin_tools": [],
                    "mcps": [],
                    "skills": [],
                    "members": [],
                    "context": {},
                    "react": {},
                }
            },
        }
    )
    return create_runtime(
        AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{directory / 'sessions.db'}"),
        catalog=AgentCatalog(
            definitions=definitions,
            mcps={},
            base_dir=Path(__file__).parent,
            skills_dir=Path(__file__).parent / "skills",
        ),
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(
            str(directory / "workspaces"),
            isolation=IsolationPolicy.PER_USER,
        ),
        injection_config=InjectionConfig(timezone="UTC"),
        context_ttl_seconds=3600,
    )


async def main() -> None:
    """身份由宿主提供；只用公共 API 完成创建、执行和读取。"""
    async with build_runtime(
        Path("data/standalone").resolve(), os.environ["OPENAI_API_KEY"]
    ) as runtime:
        session = await runtime.create("demo-user", agent="helper", title="Greeting")
        await runtime.send(
            "demo-user",
            session.id,
            UserMsg(name="user", content="请调用 greeting 并向我问好。"),
            clients={"greeting": "你好，来自独立宿主。"},
        )
        await runtime.wait("demo-user", session.id)
        snapshot = await runtime.messages("demo-user", session.id)
        for message in snapshot["messages"]:
            print(message.get_text_content())


if __name__ == "__main__":
    asyncio.run(main())
