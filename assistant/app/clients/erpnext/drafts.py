"""原生采购转单和仅草稿写入；保留服务器计算及乐观并发校验。"""

from typing import Any, Literal

from app.errors.agent import AgentError

from .client import ERPNext
from .procurement import resource_path

type DraftDocType = Literal[
    "Material Request",
    "Request for Quotation",
    "Supplier Quotation",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Invoice",
    "Quality Inspection",
]
type Conversion = Literal[
    "request_to_rfq",
    "request_to_order",
    "rfq_to_quotation",
    "quotation_to_order",
    "order_to_receipt",
    "order_to_invoice",
    "receipt_to_invoice",
    "receipt_to_return",
]
# 这些方法仅映射文档，不调用有自动保存/提交副作用的批量方法。
CONVERSIONS: dict[Conversion, tuple[str, str]] = {
    "request_to_rfq": (
        "Material Request",
        "erpnext.stock.doctype.material_request.material_request.make_request_for_quotation",
    ),
    "request_to_order": (
        "Material Request",
        "erpnext.stock.doctype.material_request.material_request.make_purchase_order",
    ),
    "rfq_to_quotation": (
        "Request for Quotation",
        "erpnext.buying.doctype.request_for_quotation.request_for_quotation.make_supplier_quotation_from_rfq",
    ),
    "quotation_to_order": (
        "Supplier Quotation",
        "erpnext.buying.doctype.supplier_quotation.supplier_quotation.make_purchase_order",
    ),
    "order_to_receipt": (
        "Purchase Order",
        "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
    ),
    "order_to_invoice": (
        "Purchase Order",
        "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
    ),
    "receipt_to_invoice": (
        "Purchase Receipt",
        "erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice",
    ),
    "receipt_to_return": (
        "Purchase Receipt",
        "erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_return",
    ),
}


def validate_fields(fields: dict[str, Any]) -> None:
    """禁止模型通过字段绕过生命周期、权限或服务器内部标记。"""
    protected = {
        "doctype",
        "docstatus",
        "status",
        "workflow_state",
        "flags",
        "owner",
        "creation",
        "modified",
        "modified_by",
        "parent",
        "parenttype",
        "parentfield",
    }

    def check(value: Any, *, root: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in protected
                    or key.startswith(("_", "ignore_"))
                    or (root and key == "name")
                ):
                    raise AgentError("草稿字段包含受保护的状态、身份或内部字段。")
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(fields, root=True)


async def prepare_document(
    erp: ERPNext,
    conversion: Conversion,
    source_name: str,
    supplier: str | None,
) -> dict[str, Any]:
    source_type, method = CONVERSIONS[conversion]
    source = (await erp.get(resource_path(source_type, source_name)))["data"]
    if source.get("docstatus") != 1:
        raise AgentError("转单来源必须是已提交单据。")
    if (
        source_type == "Material Request"
        and source.get("material_request_type") != "Purchase"
    ):
        raise AgentError("只支持采购类型的物料需求。")
    params: dict[str, Any] = {"source_name": source_name}
    if conversion == "rfq_to_quotation":
        if not supplier or supplier not in {
            row.get("supplier") for row in source.get("suppliers", [])
        }:
            raise AgentError("请选择此询价单中的供应商。")
        params["for_supplier"] = supplier
    elif supplier is not None:
        raise AgentError("supplier 仅用于询价单转报价；其他转单请在草稿中设置供应商。")
    result = await erp.post("/api/method/" + method, params)
    return {
        "saved": False,
        "document": result.get("message"),
        "source_modified": source.get("modified"),
    }


async def save_draft(
    erp: ERPNext,
    doctype: DraftDocType,
    fields: dict[str, Any],
    name: str | None = None,
    expected_modified: str | None = None,
) -> dict[str, Any]:
    validate_fields(fields)
    if not fields:
        raise AgentError("草稿字段不能为空。")
    if name is None:
        if expected_modified is not None:
            raise AgentError("新建草稿不接受 expected_modified。")
        document = {"doctype": doctype, **fields, "docstatus": 0}
        method = "insert"
    else:
        if not expected_modified:
            raise AgentError("更新前须读取单据并提供 expected_modified。")
        document = (await erp.get(resource_path(doctype, name)))["data"]
        if document.get("docstatus") != 0:
            raise AgentError("只能通过此工具修改草稿。")
        if document.get("modified") != expected_modified:
            raise AgentError("单据已被修改，请重新读取并确认变更。", 409)
        document.update(fields)
        # 保留读取时的 modified；frappe.client.save 在写入时再次检查并发版本。
        method = "save"
    if (
        doctype == "Material Request"
        and document.get("material_request_type") != "Purchase"
    ):
        raise AgentError("物料需求必须设置 material_request_type=Purchase。")
    return await erp.post("/api/method/frappe.client." + method, {"doc": document})
