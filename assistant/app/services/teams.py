"""读取框架团队成员与原生状态，供运行控制及用户界面使用。"""

from agentscope.app._service import SessionStatus
from agentscope.message import ToolCallState


class TeamService:
    """使用原生团队和成员会话组织任务范围，不自行实现成员消息通信。"""

    def __init__(self, runtime):
        """绑定框架存储及会话状态服务。"""
        self.runtime = runtime
        self.storage = runtime.storage

    async def scope(self, user_id, root):
        """返回负责人及当前团队的成员会话，忽略已经删除的成员。"""
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

    async def summary(self, user_id, root):
        """汇总团队运行和等待状态，返回原生工具确认事件对应的待确认调用。"""
        from agentscope.event import RequireUserConfirmEvent

        members = []
        confirmations = []
        running = False
        for row in await self.scope(user_id, root):
            status = await self.runtime.session_service.get_session_status(
                user_id, row.agent_id, row.id
            )
            task = self.runtime.chat_run_registry.get(row.id)
            running |= status == SessionStatus.RUNNING or (
                task is not None and not task.done()
            )
            agent = await self.storage.get_agent(user_id, row.agent_id)
            if row.id != root.id:
                members.append(
                    {
                        "agent_id": row.agent_id,
                        "session_id": row.id,
                        "name": agent.data.name if agent else row.agent_id,
                        "status": status,
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
        return {"members": members, "running": running, "confirmations": confirmations}
