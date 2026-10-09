"""会话请求结构，由 FastAPI 解析和校验。"""

from typing import Any, Literal
from uuid import UUID

from agentscope.event import UserConfirmResultEvent
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class PageContext(BaseModel):
    """用户页面和表单快照；包含路由、保存状态及可读取的业务字段。"""

    model_config = ConfigDict(extra="forbid")
    route: list[str] = Field(default_factory=list)
    doctype: str | None = None
    name: str | None = None
    is_new: bool = False
    is_dirty: bool = False
    doc: dict[str, Any] | None = None


class Attachment(BaseModel):
    """传给 Assistant 的附件载荷，data 为原始文件内容的 Base64 编码。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    media_type: str
    data: str = Field(repr=False)


class Command(BaseModel):
    """单入口会话请求；sid 是 ERPNext 登录凭据，session_id 是业务会话 ID。"""

    model_config = ConfigDict(extra="forbid")
    sid: SecretStr
    action: Literal[
        "agents",
        "list",
        "create",
        "messages",
        "rename",
        "delete",
        "send",
        "subscribe",
        "interrupt",
        "resume",
        "cancel",
        "confirm",
    ]
    session_id: UUID | None = None  # agents、list 和 create 不需要会话 ID。
    agent_key: str | None = None  # 创建会话时选择角色；空值使用默认角色。
    confirmation: UserConfirmResultEvent | None = None  # 框架原生工具确认结果。
    message: str = ""
    title: str = Field(default="", max_length=64)
    page_context: PageContext | None = None
    attachments: list[Attachment] = Field(default_factory=list)
