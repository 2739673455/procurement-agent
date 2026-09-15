"""请求身份验证及应用服务依赖。"""

import asyncio
from dataclasses import dataclass

from fastapi import Request

from app.clients.erpnext.client import ERPNext
from app.config import app_config
from app.contracts.conversations import Command
from app.observability import context
from app.services.conversations import ConversationService


@dataclass(frozen=True)
class AuthenticatedUser:
    owner: tuple[str, str]
    erp: ERPNext


async def authenticate(command: Command) -> AuthenticatedUser:
    erp = ERPNext(command.sid.get_secret_value())
    user = await asyncio.to_thread(erp.authenticate)
    context.user_id_ctx.set(user)
    return AuthenticatedUser((app_config.cfg.erpnext.site, user), erp)


def conversation_service(request: Request) -> ConversationService:
    return request.app.state.conversations
