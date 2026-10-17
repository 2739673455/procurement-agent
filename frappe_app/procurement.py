"""采购执行入口：在同一事务中锁定版本、检查权限并调用原生业务方法。"""

import frappe
from frappe.model.workflow import apply_workflow, get_transitions, get_workflow_name
from frappe.utils import get_datetime

DOCUMENT_TYPES = {
    "Material Request",
    "Request for Quotation",
    "Supplier Quotation",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Invoice",
    "Quality Inspection",
}


def _require_doctype(doctype: str) -> None:
    if doctype not in DOCUMENT_TYPES:
        frappe.throw("此对象不在采购操作范围内。", frappe.PermissionError)


@frappe.whitelist(methods=["GET"])
def get_actions(doctype: str, name: str) -> dict:
    """返回当前用户可见单据的工作流动作；没有工作流时返回空列表。"""
    _require_doctype(doctype)
    doc = frappe.get_doc(doctype, name)
    doc.check_permission("read")
    workflow = get_workflow_name(doctype)
    return {
        "workflow": workflow,
        "modified": str(doc.modified),
        "transitions": get_transitions(doc) if workflow else [],
    }


@frappe.whitelist(methods=["POST"])
def execute(
    doctype: str,
    name: str,
    expected_modified: str,
    operation: str,
    action: str | None = None,
    recipients: list[str] | None = None,
) -> dict:
    """仅执行已登记动作；不接受任意方法路径、状态字段或模型构造的文档。"""
    _require_doctype(doctype)
    if operation not in {
        "submit",
        "cancel",
        "workflow",
        "hold",
        "close",
        "reopen",
        "send_rfq",
    }:
        frappe.throw("未知采购动作。", frappe.PermissionError)
    # 从持久化文档执行，锁持有到 Frappe 请求事务结束，版本校验后不会被其他写入抢占。
    doc = frappe.get_doc(doctype, name, for_update=True)
    doc.check_permission("read")
    if not expected_modified or get_datetime(doc.modified) != get_datetime(
        expected_modified
    ):
        frappe.throw("单据已变化，请重新读取并确认。", frappe.TimestampMismatchError)
    if doctype == "Material Request" and doc.material_request_type != "Purchase":
        frappe.throw("只支持采购类型的物料需求。")
    workflow = get_workflow_name(doctype)
    if operation in {"submit", "cancel"}:
        if workflow:
            frappe.throw("此单据启用工作流，请使用可执行的工作流动作。")
        doc.check_permission(operation)
        required_status = 0 if operation == "submit" else 1
        if doc.docstatus != required_status:
            frappe.throw("当前单据状态不允许此操作。")
        if operation == "submit":
            # RFQ 的 on_submit 可自动发邮件。发送须使用单独的确认动作。
            if doctype == "Request for Quotation" and any(
                row.send_email for row in doc.suppliers
            ):
                frappe.throw(
                    "请先在询价草稿的所有 suppliers 行关闭 send_email 自动发送，再单独确认发送询价。"
                )
            doc.submit()
        else:
            doc.cancel()
    elif operation == "workflow":
        if not workflow or not action:
            frappe.throw("需要有效的工作流动作。")
        if (
            doctype == "Request for Quotation"
            and doc.docstatus == 0
            and any(row.send_email for row in doc.suppliers)
        ):
            frappe.throw(
                "请先关闭询价单所有 suppliers 行的 send_email 自动发送，再执行工作流。"
            )
        apply_workflow(doc, action)
    elif operation in {"hold", "close", "reopen"}:
        if doctype != "Purchase Order" or doc.docstatus != 1:
            frappe.throw("只能操作已提交采购订单。")
        doc.check_permission("submit")
        if operation == "reopen" and doc.status not in {"Closed", "On Hold"}:
            frappe.throw("只有已关闭或暂停的订单可以恢复。")
        from erpnext.buying.doctype.purchase_order.purchase_order import update_status

        update_status(
            {"hold": "On Hold", "close": "Closed", "reopen": "Submitted"}[operation],
            name,
        )
    else:
        if doctype != "Request for Quotation" or doc.docstatus != 1:
            frappe.throw("只能发送已提交询价单。")
        doc.check_permission("write")
        requested = set(recipients or [])
        available = {row.email_id for row in doc.suppliers if row.email_id}
        if (
            not requested
            or requested - available
            or len(requested) != len(recipients or [])
        ):
            frappe.throw("收件人必须是询价单中已有的不重复地址，请重新读取并确认。")
        selected = [row for row in doc.suppliers if row.email_id in requested]
        if any(row.email_sent for row in selected):
            frappe.throw("询价已发送，请先核对通信记录，禁止自动重复发送。")
        # send_email 是原生允许提交后修改的字段；只发送本次确认的已存在地址。
        for row in doc.suppliers:
            row.send_email = int(row.email_id in requested)
        doc.save()
        from erpnext.buying.doctype.request_for_quotation.request_for_quotation import (
            send_supplier_emails,
        )

        send_supplier_emails(name)
    doc.reload()
    doc.apply_fieldlevel_read_permissions()
    # send_rfq 表示请求已交给 ERP 邮件流程，不保证外部邮件最终投递成功。
    return {
        "operation": operation,
        "document": doc.as_dict(),
        "email_queued": operation == "send_rfq",
    }
