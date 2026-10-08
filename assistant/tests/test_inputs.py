import asyncio
import base64

import pytest
from agentscope.formatter import DeepSeekChatFormatter

from app.config import app_config
from app.contracts.conversations import Attachment, PageContext
from app.errors.agent import AgentError
from app.services.inputs import user_message
from app.services.messages import public_messages


def test_page_snapshot_text_and_image_preserved_but_not_exposed(monkeypatch):
    model = app_config.cfg.lm_config.models[app_config.cfg.lm_config.active]
    monkeypatch.setattr(model.profile, "image_inputs", True)
    attachments = [
        Attachment(
            id="text",
            name="notes.txt",
            media_type="text/plain",
            data=base64.b64encode("数量 3".encode()).decode(),
        ),
        Attachment(
            id="image",
            name="photo.png",
            media_type="image/png",
            data=base64.b64encode(b"fake-png").decode(),
        ),
    ]
    message = user_message(
        "看看附件",
        PageContext(doctype="Item", doc={"item_code": "BOLT"}, is_dirty=True),
        attachments,
    )
    text = message.get_text_content()
    assert text is not None and "BOLT" in text and "数量 3" in text
    formatted = asyncio.run(
        DeepSeekChatFormatter(input_types=["text/plain", "image/*"]).format([message])
    )
    assert any(
        block.get("type") == "image_url"
        for item in formatted
        for block in item.get("content", [])
    )
    visible = public_messages([message])[0]
    assert visible["content"] == "看看附件"
    assert "doc" not in visible["page_context"]
    assert all("data" not in attachment for attachment in visible["attachments"])


def test_image_rejected_for_text_only_model(monkeypatch):
    model = app_config.cfg.lm_config.models[app_config.cfg.lm_config.active]
    monkeypatch.setattr(model.profile, "image_inputs", False)
    with pytest.raises(AgentError, match="不支持图片"):
        user_message(
            "看图",
            None,
            [
                Attachment(
                    id="image", name="photo.png", media_type="image/png", data="eA=="
                )
            ],
        )
