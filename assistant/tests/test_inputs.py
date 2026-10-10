import asyncio
import base64

from agentscope.formatter import DeepSeekChatFormatter

from app.contracts.sessions import AttachmentInfo, PageContext
from app.services.inputs import user_message


def test_attachment_paths_and_native_images_preserved() -> None:
    attachments: list[AttachmentInfo] = [
        {
            "name": "notes.txt",
            "media_type": "text/plain",
            "path": "/workspace/sessions/test/attachments/notes.txt",
        },
        {
            "name": "photo.png",
            "media_type": "image/png",
            "path": "/workspace/sessions/test/attachments/photo.png",
            "data": base64.b64encode(b"fake-png").decode(),
        },
    ]
    message = user_message(
        "看看附件",
        PageContext(doctype="Item", doc={"item_code": "BOLT"}, is_dirty=True),
        attachments,
        image_inputs=True,
    )
    text = message.get_text_content()
    assert text is not None and "BOLT" in text and attachments[0]["path"] in text
    formatted = asyncio.run(
        DeepSeekChatFormatter(input_types=["text/plain", "image/*"]).format([message])
    )
    assert any(
        block.get("type") == "image_url"
        for item in formatted
        for block in item.get("content", [])
    )
    assert message.metadata["display_text"] == "看看附件"
    assert "doc" not in message.metadata["page_context"]
    assert all("data" not in item for item in message.metadata["attachments"])
    text_only = user_message("查看", None, attachments, image_inputs=False)
    assert "不支持图片" in (text_only.get_text_content() or "")
    assert not any(block.type == "data" for block in text_only.content)
