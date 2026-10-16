"""采购执行工具，所有有副作用的动作都经框架确认与服务器版本校验。"""

from typing import Any, Literal

from agentscope.tool import FunctionTool

from app.clients.erpnext.client import ERPNext
from app.clients.erpnext.drafts import DraftDocType
from app.clients.erpnext.procurement import DocumentName
from app.runtime.context import RunContext
from app.tools.common import erp_tool

EXECUTE_PATH = "/api/method/buying_ai.procurement.execute"


def create_workflow_actions_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def get_procurement_actions(
        doctype: DraftDocType, name: DocumentName
    ) -> dict[str, Any]:
        """查询当前用户的原生工作流动作及 modified；无工作流时 transitions 为空。"""
        return await erp.get(
            "/api/method/buying_ai.procurement.get_actions",
            {"doctype": doctype, "name": name},
        )

    return erp_tool(get_procurement_actions)


def create_submit_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def submit_procurement_document(
        doctype: DraftDocType,
        name: DocumentName,
        expected_modified: DocumentName,
    ) -> dict[str, Any]:
        """确认后提交已保存草稿，可能影响采购、库存或应付；有工作流则拒绝直接提交。

        先读取完整单据核对公司、供应商、金额、明细及 modified。RFQ 所有 suppliers 行必须关闭 send_email 自动发送。
        提交收货/发票不能代替实际验收或财务审批。不得自动提交刚生成的草稿。
        """
        return await erp.post(
            EXECUTE_PATH,
            {
                "doctype": doctype,
                "name": name,
                "expected_modified": expected_modified,
                "operation": "submit",
            },
        )

    return erp_tool(submit_procurement_document, read_only=False)


def create_cancel_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def cancel_procurement_document(
        doctype: DraftDocType,
        name: DocumentName,
        expected_modified: DocumentName,
    ) -> dict[str, Any]:
        """确认后取消已提交单据，可能撤销库存/财务影响；关联单据约束由 ERPNext 校验。

        必须核对最新单据及 modified。有工作流须走工作流动作；不级联取消、不删除。
        """
        return await erp.post(
            EXECUTE_PATH,
            {
                "doctype": doctype,
                "name": name,
                "expected_modified": expected_modified,
                "operation": "cancel",
            },
        )

    return erp_tool(cancel_procurement_document, read_only=False)


def create_apply_workflow_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def apply_procurement_workflow(
        doctype: DraftDocType,
        name: DocumentName,
        expected_modified: DocumentName,
        action: DocumentName,
    ) -> dict[str, Any]:
        """执行 get_procurement_actions 返回的原生动作，不能自行编造审批状态。

        动作可能提交或取消单据；确认前说明具体业务影响。角色、条件、自我审批限制仍由 Frappe 校验。
        """
        return await erp.post(
            EXECUTE_PATH,
            {
                "doctype": doctype,
                "name": name,
                "expected_modified": expected_modified,
                "operation": "workflow",
                "action": action,
            },
        )

    return erp_tool(apply_procurement_workflow, read_only=False)


def create_order_status_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def change_purchase_order_status(
        name: DocumentName,
        expected_modified: DocumentName,
        action: Literal["hold", "close", "reopen"],
    ) -> dict[str, Any]:
        """确认后暂停、关闭或恢复已提交采购订单；关闭不等于取消，也不代表已收货。"""
        return await erp.post(
            EXECUTE_PATH,
            {
                "doctype": "Purchase Order",
                "name": name,
                "expected_modified": expected_modified,
                "operation": action,
            },
        )

    return erp_tool(change_purchase_order_status, read_only=False)


def create_send_rfq_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def send_procurement_rfq(
        name: DocumentName,
        expected_modified: DocumentName,
        recipients: list[DocumentName],
    ) -> dict[str, Any]:
        """确认后向已提交询价单中指定的供应商地址发送询价邮件。

        先读取询价单，核对 suppliers 中 send_email/email_id、询价物料、正文和条款。
        recipients 必须是单据已有 email_id 的非空无重复子集；工具会更新对应行 send_email，不新增或替换地址。已发送拒绝重复发送。
        原生流程可能创建供应商门户访问；成功仅表示交给 ERP 邮件流程，不保证外部投递。
        """
        return await erp.post(
            EXECUTE_PATH,
            {
                "doctype": "Request for Quotation",
                "name": name,
                "expected_modified": expected_modified,
                "operation": "send_rfq",
                "recipients": recipients,
            },
        )

    return erp_tool(send_procurement_rfq, read_only=False)
