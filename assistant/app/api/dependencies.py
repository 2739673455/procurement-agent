"""请求身份验证及应用服务依赖。"""

import asyncio
from dataclasses import dataclass

from fastapi import Request

from app.clients.erpnext.client import ERPNext
from app.config import app_config
from app.contracts.sessions import Command
from app.observability import context
from app.services.sessions import SessionService


@dataclass(frozen=True)
class AuthenticatedUser:
    """认证结果：站点和用户构成会话归属，ERPNext 客户端绑定登录凭据。"""

    owner: tuple[str, str]
    erp: ERPNext


async def authenticate(command: Command) -> AuthenticatedUser:
    """用请求中的 sid 向 ERPNext 核实身份，并设置请求日志的用户信息。"""
    erp = ERPNext(command.sid.get_secret_value())
    user = await asyncio.to_thread(erp.authenticate)
    context.user_id_ctx.set(user)
    return AuthenticatedUser((app_config.cfg.erpnext.site, user), erp)


def session_service(request: Request) -> SessionService:
    """从应用生命周期初始化的组件中获取会话业务服务。"""
    return request.app.state.sessions
