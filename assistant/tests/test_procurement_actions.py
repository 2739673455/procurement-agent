"""采购执行的确认门槛、角色隔离与 Frappe 事务边界。"""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.permission import PermissionBehavior
from test_runs import (
    Model,
    command,
    configured_catalog,
    response,
    run_turn,
    service_at,
    use_models,
)

from app.clients.erpnext.client import ERPNext
from app.runtime.context import RunContext
from app.tools import actions, drafts


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """加载实际入口，用最小框架替身观察权限/锁/副作用顺序，不替代真实 ERP 联调。"""
    doc = SimpleNamespace(
        doctype="Purchase Order",
        name="PO-1",
        modified="2026-10-17 10:00:00",
        docstatus=0,
        status="Draft",
        material_request_type="Purchase",
        suppliers=[
            SimpleNamespace(email_id="supplier@example.com", send_email=0, email_sent=0)
        ],
        check_permission=Mock(),
        submit=Mock(),
        cancel=Mock(),
        save=Mock(),
        reload=Mock(),
        apply_fieldlevel_read_permissions=Mock(),
        as_dict=Mock(return_value={"name": "PO-1"}),
    )

    def fail(message: str, *args: Any) -> None:
        raise ValueError(message)

    frappe = SimpleNamespace(
        whitelist=lambda **_: lambda f: f,
        get_doc=Mock(return_value=doc),
        throw=fail,
        PermissionError=ValueError,
        TimestampMismatchError=ValueError,
    )
    workflow = SimpleNamespace(
        get_workflow_name=Mock(return_value=""),
        get_transitions=Mock(return_value=[{"action": "Approve"}]),
        apply_workflow=Mock(),
    )
    update_status, send = Mock(), Mock()
    for name, module in {
        "frappe": frappe,
        "frappe.model": SimpleNamespace(),
        "frappe.model.workflow": workflow,
        "frappe.utils": SimpleNamespace(get_datetime=datetime.fromisoformat),
        "erpnext.buying.doctype.purchase_order.purchase_order": SimpleNamespace(
            update_status=update_status
        ),
        "erpnext.buying.doctype.request_for_quotation.request_for_quotation": SimpleNamespace(
            send_supplier_emails=send
        ),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[2] / "frappe_app" / "procurement.py"
    spec = importlib.util.spec_from_file_location("procurement_gateway_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return SimpleNamespace(
        api=module,
        doc=doc,
        frappe=frappe,
        workflow=workflow,
        update_status=update_status,
        send=send,
    )


def test_gateway_rejects_invalid_scope_versions_and_permissions(server: Any) -> None:
    for values in (
        {"doctype": "User", "operation": "submit"},
        {"doctype": "Purchase Order", "operation": "delete"},
    ):
        with pytest.raises(ValueError):
            server.api.execute(
                name="PO-1", expected_modified=server.doc.modified, **values
            )
    server.frappe.get_doc.assert_not_called()
    with pytest.raises(ValueError, match="变化"):
        server.api.execute("Purchase Order", "PO-1", "2026-10-16 10:00:00", "submit")
    server.frappe.get_doc.assert_called_with("Purchase Order", "PO-1", for_update=True)
    server.doc.submit.assert_not_called()
    server.doc.check_permission.side_effect = ValueError("forbidden")
    with pytest.raises(ValueError, match="forbidden"):
        server.api.execute("Purchase Order", "PO-1", server.doc.modified, "submit")
    server.doc.submit.assert_not_called()


def test_gateway_lifecycle_workflow_and_order_status(server: Any) -> None:
    server.api.execute("Purchase Order", "PO-1", server.doc.modified, "submit")
    server.doc.submit.assert_called_once()
    server.doc.check_permission.assert_any_call("submit")
    server.doc.docstatus = 1
    server.api.execute("Purchase Order", "PO-1", server.doc.modified, "cancel")
    server.doc.cancel.assert_called_once()
    server.doc.status = "Closed"
    server.api.execute("Purchase Order", "PO-1", server.doc.modified, "reopen")
    server.update_status.assert_called_once_with("Submitted", "PO-1")
    server.workflow.get_workflow_name.return_value = "Purchasing Approval"
    for operation in ("submit", "cancel"):
        with pytest.raises(ValueError, match="工作流"):
            server.api.execute("Purchase Order", "PO-1", server.doc.modified, operation)
    server.api.execute(
        "Purchase Order", "PO-1", server.doc.modified, "workflow", action="Approve"
    )
    server.workflow.apply_workflow.assert_called_once_with(server.doc, "Approve")
    assert server.api.get_actions("Purchase Order", "PO-1")["transitions"] == [
        {"action": "Approve"}
    ]


def test_rfq_submit_does_not_send_and_explicit_send_checks_recipients(
    server: Any,
) -> None:
    server.doc.suppliers[0].send_email = 1
    with pytest.raises(ValueError, match="自动发送"):
        server.api.execute(
            "Request for Quotation", "RFQ-1", server.doc.modified, "submit"
        )
    server.doc.submit.assert_not_called()
    server.workflow.get_workflow_name.return_value = "Approval"
    with pytest.raises(ValueError, match="自动发送"):
        server.api.execute(
            "Request for Quotation",
            "RFQ-1",
            server.doc.modified,
            "workflow",
            action="Approve",
        )
    server.workflow.apply_workflow.assert_not_called()
    server.doc.docstatus = 1
    for recipients in ([], ["outsider@example.com"], ["supplier@example.com"] * 2):
        with pytest.raises(ValueError, match="收件人"):
            server.api.execute(
                "Request for Quotation",
                "RFQ-1",
                server.doc.modified,
                "send_rfq",
                recipients=recipients,
            )
    server.send.assert_not_called()
    result = server.api.execute(
        "Request for Quotation",
        "RFQ-1",
        server.doc.modified,
        "send_rfq",
        recipients=["supplier@example.com"],
    )
    assert result["email_queued"]
    server.send.assert_called_once_with("RFQ-1")
    server.doc.save.assert_called_once()
    server.doc.suppliers[0].email_sent = 1
    with pytest.raises(ValueError, match="重复"):
        server.api.execute(
            "Request for Quotation",
            "RFQ-1",
            server.doc.modified,
            "send_rfq",
            recipients=["supplier@example.com"],
        )
    assert server.send.call_count == 1


def test_all_write_tools_ask_and_researcher_has_no_write_tools() -> None:
    async def scenario() -> None:
        context = RunContext("user", "session", {"erpnext": Mock()})
        factories = [
            drafts.create_prepare_document_tool,
            drafts.create_save_draft_tool,
            actions.create_submit_tool,
            actions.create_cancel_tool,
            actions.create_apply_workflow_tool,
            actions.create_order_status_tool,
            actions.create_send_rfq_tool,
        ]
        for factory in factories:
            tool = factory(context)
            assert not tool.is_read_only
            assert (await tool.check_permissions({})).behavior == PermissionBehavior.ASK
        catalog = configured_catalog()
        assert all(
            tool.is_read_only
            for tool in catalog.create_tools(
                catalog.definitions.agents["item_researcher"], context
            )
        )

    asyncio.run(scenario())


def test_real_tool_confirmation_executes_persisted_draft_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        call = response()
        call.append_tool_call(
            name="save_procurement_draft",
            input=json.dumps(
                {
                    "doctype": "Purchase Order",
                    "fields": {"supplier": "approved-supplier"},
                }
            ),
            block_id="save-1",
        )
        use_models(monkeypatch, [Model([call]), Model([response("已保存草稿")])])
        erp = Mock(spec=ERPNext)
        erp.post = AsyncMock(return_value={"message": {"name": "PO-1", "docstatus": 0}})
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            await run_turn(service, identifier, message="创建采购草稿")
            erp.post.assert_not_awaited()
            snapshot = await command(service, "messages", identifier)
            confirmation = snapshot["confirmations"][0]
            forged = confirmation.tool_calls[0].model_copy(
                update={
                    "input": json.dumps(
                        {"doctype": "Purchase Order", "fields": {"supplier": "forged"}}
                    )
                }
            )
            from app.contracts.sessions import Command

            await service.execute(
                Command.model_validate(
                    {
                        "sid": "test",
                        "action": "confirm",
                        "session_id": identifier,
                        "confirmation": UserConfirmResultEvent(
                            reply_id=confirmation.reply_id,
                            confirm_results=[
                                ConfirmResult(confirmed=True, tool_call=forged)
                            ],
                        ),
                    }
                ),
                ("site", "owner"),
                cast(ERPNext, erp),
            )
            await service._runtime._app.state.chat_run_registry.get(identifier)
            erp.post.assert_awaited_once()
            assert erp.post.call_args.args[1]["doc"]["supplier"] == "approved-supplier"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("factory", "parameters", "operation"),
    [
        (actions.create_submit_tool, {"doctype": "Purchase Receipt"}, "submit"),
        (actions.create_cancel_tool, {"doctype": "Purchase Invoice"}, "cancel"),
        (
            actions.create_apply_workflow_tool,
            {"doctype": "Purchase Order", "action": "Approve"},
            "workflow",
        ),
        (actions.create_order_status_tool, {"action": "hold"}, "hold"),
        (actions.create_order_status_tool, {"action": "close"}, "close"),
        (actions.create_order_status_tool, {"action": "reopen"}, "reopen"),
        (
            actions.create_send_rfq_tool,
            {"recipients": ["supplier@example.com"]},
            "send_rfq",
        ),
    ],
)
def test_action_tool_posts_fixed_gateway(
    factory: Any, parameters: dict, operation: str
) -> None:
    async def scenario() -> None:
        erp = Mock(spec=ERPNext)
        erp.post = AsyncMock(return_value={"message": {"operation": operation}})
        tool = factory(RunContext("user", "session", {"erpnext": erp}))
        await tool(name="DOC-1", expected_modified="2026-10-17 10:00:00", **parameters)
        erp.post.assert_awaited_once()
        path, body = erp.post.call_args.args
        assert path == "/api/method/buying_ai.procurement.execute"
        assert body["operation"] == operation
        assert body["expected_modified"] == "2026-10-17 10:00:00"
        assert "sid" not in body and "csrf_token" not in body

    asyncio.run(scenario())


def test_frappe_proxy_forwards_server_session_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import os

    frappe = SimpleNamespace(
        whitelist=lambda **_: lambda f: f,
        session=SimpleNamespace(sid="server-session"),
        local=SimpleNamespace(response_headers={}),
        parse_json=lambda value: value,
    )
    monkeypatch.setitem(sys.modules, "frappe", frappe)
    monkeypatch.setitem(
        sys.modules,
        "frappe.sessions",
        SimpleNamespace(get_csrf_token=lambda: "server-csrf"),
    )
    monkeypatch.setitem(
        sys.modules,
        "buying_ai.context",
        SimpleNamespace(page_snapshot=lambda value: value),
    )
    # 本测试只验证普通 JSON 代理，流式 Response 由 Frappe 环境提供。
    monkeypatch.setitem(
        sys.modules,
        "werkzeug.wrappers",
        SimpleNamespace(Response=type("Response", (), {})),
    )
    monkeypatch.setenv("BUYING_AI_AGENT_URL", "https://assistant.test")
    path = Path(__file__).resolve().parents[2] / "frappe_app" / "api.py"
    spec = importlib.util.spec_from_file_location("frappe_proxy_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sent = []

    def upstream(request: Any, timeout: int) -> Any:
        sent.append(json.loads(request.data))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(module, "urlopen", upstream)
    assert module.sessions(action="send", message="hello") == {"ok": True}
    assert sent[0]["sid"] == "server-session"
    assert sent[0]["csrf_token"] == "server-csrf"
    assert os.environ["BUYING_AI_AGENT_URL"] == "https://assistant.test"
