"""读取框架 Agent 团队中的成员 Agent 及原生状态，供运行控制及用户界面使用。"""

import json
from typing import TypedDict

from agentscope.app._service import SessionStatus
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import SessionRecord, StorageBase
from agentscope.event import RequireUserConfirmEvent
from agentscope.message import ToolCallState
from starlette.datastructures import State


class TeamMemberStatus(TypedDict):
    """成员 Agent 的会话与状态。"""

    agent_id: str
    session_id: str
    name: str
    status: SessionStatus


class BackgroundTool(TypedDict):
    """框架后台任务注册表中的工具信息。"""

    tool_name: str
    agent_id: str
    started_at: float


class TeamSummary(TypedDict):
    """团队运行状态和待确认调用。"""

    members: list[TeamMemberStatus]
    running: bool
    background_tasks: dict[str, BackgroundTool]
    confirmations: list[RequireUserConfirmEvent]


class TeamService:
    """使用原生 Agent 团队及成员 Agent 会话组织任务范围，Agent 间通信由框架负责。"""

    def __init__(self, runtime: State) -> None:
        """绑定框架存储及会话状态服务。"""
        self.runtime = runtime
        self.storage: StorageBase = runtime.storage

    async def scope(self, user_id: str, root: SessionRecord) -> list[SessionRecord]:
        """返回负责人 Agent 及当前成员 Agent 的会话，忽略已经删除的成员 Agent。"""
        root = await self.storage.get_session(user_id, root.agent_id, root.id) or root
        rows = [root]
        if root.team_id:
            team = await self.storage.get_team(user_id, root.team_id)
            if team is not None and team.session_id == root.id:
                for member in team.data.members:
                    row = await self.storage.get_session(
                        user_id, member.agent_id, member.session_id
                    )
                    if row is not None:
                        rows.append(row)
        return rows

    async def summary(self, user_id: str, root: SessionRecord) -> TeamSummary:
        """汇总 Agent 团队的运行和等待状态，返回原生工具确认事件对应的待确认调用。"""
        members: list[TeamMemberStatus] = []
        confirmations: list[RequireUserConfirmEvent] = []
        background_tasks: dict[str, BackgroundTool] = {}
        running = False
        for row in await self.scope(user_id, root):
            status = await self.runtime.session_service.get_session_status(
                user_id, row.agent_id, row.id
            )
            task = self.runtime.chat_run_registry.get(row.id)
            tools = await self.runtime.message_bus.registry_getall(
                MessageBusKeys.bg_tasks(row.id)
            )
            background_tasks.update(
                {task_id: json.loads(metadata) for task_id, metadata in tools.items()}
            )
            running |= (
                status == SessionStatus.RUNNING
                or (task is not None and not task.done())
                or bool(tools)
            )
            agent = await self.storage.get_agent(user_id, row.agent_id)
            if row.id != root.id:
                members.append(
                    {
                        "agent_id": row.agent_id,
                        "session_id": row.id,
                        "name": agent.data.name if agent else row.agent_id,
                        "status": SessionStatus.RUNNING if tools else status,
                    }
                )
            if status == SessionStatus.AWAITING_PERMISSION and agent is not None:
                confirmations.append(
                    RequireUserConfirmEvent(
                        reply_id=row.state.reply_id,
                        tool_calls=[
                            call
                            for call in row.state.get_awaiting_tool_calls(
                                agent.data.name
                            )
                            if call.state == ToolCallState.ASKING
                        ],
                    )
                )
        return {
            "members": members,
            "running": running,
            "background_tasks": background_tasks,
            "confirmations": confirmations,
        }
