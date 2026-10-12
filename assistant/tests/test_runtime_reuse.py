"""独立项目的运行模块复用与应用错误映射检查。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors.exc_handlers import register_exception_handlers
from app.runtime.errors import RunError


def test_runtime_in_an_independent_project(tmp_path: Path) -> None:
    """复制运行包并清除应用环境变量，实际执行原生模型、工具和运行控制。"""
    assistant = Path(__file__).resolve().parents[1]
    shutil.copytree(
        assistant / "app" / "runtime",
        tmp_path / "portable_runtime",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(assistant / "tests" / "fixtures" / "runtime_app.py", tmp_path)
    (tmp_path / "external_tools.py").write_text(
        "from agentscope.tool import FunctionTool, ToolChunk\n"
        "from agentscope.message import TextBlock\n"
        "from portable_runtime import RunContext\n"
        "def create_echo(context: RunContext) -> FunctionTool:\n"
        "    async def echo() -> ToolChunk:\n"
        '        """读取调用方绑定的资源。"""\n'
        '        return ToolChunk(content=[TextBlock(text=context.clients["value"])])\n'
        "    return FunctionTool(echo, is_read_only=True)\n"
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("ASSISTANT_", "DEEPSEEK_"))
    }
    result = subprocess.run(
        [sys.executable, "-I", str(tmp_path / "runtime_app.py")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_errors_are_mapped_by_the_application() -> None:
    """通用模块的运行冲突由应用输出为 HTTP 409。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/conflict")
    def conflict() -> None:
        """模拟调用运行控制时发生冲突。"""
        raise RunError("当前任务正在执行。", "conflict")

    with TestClient(app) as client:
        response = client.get("/conflict")
    assert response.status_code == 409
    assert response.json()["title"] == "当前任务正在执行。"
