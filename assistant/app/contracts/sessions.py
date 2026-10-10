"""会话请求结构，由 FastAPI 解析和校验。"""

from typing import Any, Literal, NotRequired, TypedDict
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


class AttachmentUpload(BaseModel):
    """待写入会话工作空间的文件，data 为原始内容的 Base64 编码。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(repr=False)


class AttachmentInfo(TypedDict):
    """工作空间附件信息。"""

    name: str
    media_type: str
    path: str
    data: NotRequired[str]


class Command(BaseModel):
    """单入口会话请求；sid 是 ERPNext 登录凭据，session_id 是业务会话 ID。"""

    model_config = ConfigDict(extra="forbid")
    sid: SecretStr
    action: Literal[
        "list",
        "create",
        "messages",
        "rename",
        "delete",
        "send",
        "upload",
        "subscribe",
        "interrupt",
        "resume",
        "cancel",
        "confirm",
    ]
    session_id: UUID | None = None  # list 和 create 不需要会话 ID。
    confirmation: UserConfirmResultEvent | None = None  # 框架原生工具确认结果。
    message: str = ""
    title: str = Field(default="", max_length=64)
    page_context: PageContext | None = None
    attachments: list[str] = Field(default_factory=list)  # 当前会话中的附件文件名。
    upload: AttachmentUpload | None = None  # 上传操作的文件载荷。
