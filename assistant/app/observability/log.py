from __future__ import annotations

import json
import sys
import traceback
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from app.config.app_config import cfg
from app.observability import context

if TYPE_CHECKING:
    from loguru import Record

LOG_DIR = Path(__file__).parents[2] / "logs"
_JSON_LINE_KEY = "_json_line"


def _build_log_payload(record: Record) -> dict[str, Any]:
    """构造结构化日志载荷。"""
    name = record.get("name") or ""
    function = record.get("function") or ""
    line = record.get("line") or ""
    location = f"{name}:{function}:{line}" if name or function or line else ""

    payload = {
        "time": record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "level": record["level"].name,
        "location": location,
        "method": context.method_ctx.get(),
        "path": context.path_ctx.get(),
        "user_id": context.user_id_ctx.get(),
        "message": record["message"],
        "request_id": context.request_id_ctx.get(),
        "trace_id": context.trace_id_ctx.get(),
        "client_ip": context.client_ip_ctx.get(),
    }
    payload.update(
        {
            key: value
            for key, value in record["extra"].items()
            if key != _JSON_LINE_KEY and key not in payload
        }
    )

    exc_info = record.get("exception")
    if exc_info and exc_info.value is not None:
        payload["exc_type"] = type(exc_info.value).__name__
        payload["exception"] = "\n".join(
            f"{frame.filename}:{frame.lineno} in {frame.name}"
            for frame in traceback.extract_tb(exc_info.traceback)
        )

    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != ""
    }


def _json_formatter(record: Record) -> str:
    """序列化单行 JSON 且不追加 Loguru 异常文本。"""
    record["extra"][_JSON_LINE_KEY] = json.dumps(
        _build_log_payload(record),
        ensure_ascii=False,
        default=str,
    )
    return f"{{extra[{_JSON_LINE_KEY}]}}\n"


def _console_formatter(record: Record) -> str:
    """控制台同样输出结构化日志，避免打印包含凭据的原始异常。"""
    return _json_formatter(record)


@cache
def setup_logger() -> None:
    """初始化日志配置。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.configure(
        handlers=[
            {
                "sink": sys.stdout,
                "level": cfg.log.level,
                "format": _console_formatter,
                "colorize": True,
                "backtrace": False,
                "diagnose": False,
                "catch": True,
                "enqueue": True,
            },
            {
                "sink": str(LOG_DIR / "{time:YYYY-MM-DD}.jsonl"),
                "level": cfg.log.level,
                "format": _json_formatter,
                "rotation": cfg.log.rotation,
                "encoding": "utf-8",
                "backtrace": False,
                "diagnose": False,
                "catch": True,
                "enqueue": True,
            },
        ],
    )
