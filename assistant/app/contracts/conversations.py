"""会话请求结构，由 FastAPI 解析和校验。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class PageContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: list[str] = Field(default_factory=list)
    doctype: str | None = None
    name: str | None = None
    is_new: bool = False
    is_dirty: bool = False
    doc: dict[str, Any] | None = None


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    media_type: str
    data: str = Field(repr=False)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sid: SecretStr
    action: Literal[
        "list", "create", "messages", "rename", "delete", "send", "subscribe", "stop"
    ]
    conversation_id: UUID | None = None
    message: str = ""
    title: str = Field(default="", max_length=64)
    page_context: PageContext | None = None
    attachments: list[Attachment] = Field(default_factory=list)
