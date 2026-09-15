"""将问题、表单快照和附件整理为模型输入，展示元数据与正文分开保存。"""

import base64
import binascii
import json
from io import BytesIO
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.config import app_config
from app.errors.agent import AgentError

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def user_message(text, page_context, attachments):
    blocks: list[str | dict[str, Any]] = [{"type": "text", "text": text}]
    if page_context is not None:
        blocks.append(
            {
                "type": "text",
                "text": "用户当前页面及表单快照（可能尚未保存）：\n"
                + json.dumps(page_context.model_dump(), ensure_ascii=False),
            }
        )
    for attachment in attachments:
        try:
            content = base64.b64decode(attachment.data, validate=True)
        except (ValueError, binascii.Error):
            raise AgentError(f"附件 {attachment.name} 内容无效。") from None
        if attachment.media_type in IMAGE_TYPES:
            model = app_config.cfg.lm_config.models[app_config.cfg.lm_config.active]
            if not model.profile.image_inputs:
                raise AgentError(
                    f"当前模型不支持图片输入，无法读取 {attachment.name}。请切换支持图片的模型。"
                )
            blocks.append(
                {
                    "type": "image",
                    "base64": attachment.data,
                    "mime_type": attachment.media_type,
                }
            )
            continue
        if Path(attachment.name).suffix.lower() == ".pdf":
            try:
                reader = PdfReader(BytesIO(content))
                extracted = "\n".join(
                    page.extract_text() or "" for page in reader.pages
                )
                extracted = (
                    extracted or "此 PDF 未提取到文本，可能为扫描件，需要 OCR 后读取。"
                )
            except (PyPdfError, ValueError, OSError):
                raise AgentError(
                    f"无法读取 PDF 附件 {attachment.name}，请检查文件是否损坏或加密。"
                ) from None
        elif (
            attachment.media_type.startswith("text/")
            or Path(attachment.name).suffix.lower() in TEXT_EXTENSIONS
        ):
            try:
                extracted = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise AgentError(
                    f"附件 {attachment.name} 不是 UTF-8 文本，请转换编码后重新上传。"
                ) from None
        else:
            extracted = (
                "附件已上传，但当前没有此文件类型的内容解析器，不能推断文件内容。"
            )
        blocks.append(
            {"type": "text", "text": f"用户附件 {attachment.name}：\n{extracted}"}
        )
    return HumanMessage(
        content=blocks,
        additional_kwargs={
            "display_text": text,
            "page_context": page_context.model_dump(exclude={"doc"})
            if page_context
            else None,
            "attachments": [
                attachment.model_dump(exclude={"data"}) for attachment in attachments
            ],
        },
    )
