"""采购拟单工具：原生映射与保存草稿是两个明确的确认动作。"""

from typing import Any

from agentscope.tool import FunctionTool

from app.clients.erpnext import drafts as api
from app.clients.erpnext.client import ERPNext
from app.clients.erpnext.procurement import DocumentName
from app.runtime.context import RunContext
from app.tools.common import erp_tool


def create_prepare_document_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def prepare_procurement_document(
        conversion: api.Conversion,
        source_name: DocumentName,
        supplier: DocumentName | None = None,
    ) -> dict[str, Any]:
        """按原生关联生成未保存文档，不代表已创建订单/收货/发票。

        request_to_rfq/request_to_order：采购需求转询价/订单；rfq_to_quotation 必填询价中的 supplier；
        quotation_to_order：报价转订单；order_to_receipt/order_to_invoice：订单转收货/发票；
        receipt_to_invoice/receipt_to_return：收货转发票/退货。来源须已提交。
        保留映射结果中的来源单号和子表来源行；审核后用 save_procurement_draft 保存业务字段。
        """
        return await api.prepare_document(erp, conversion, source_name, supplier)

    return erp_tool(prepare_procurement_document, read_only=False)


def create_save_draft_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def save_procurement_draft(
        doctype: api.DraftDocType,
        fields: dict[str, Any],
        name: DocumentName | None = None,
        expected_modified: str | None = None,
    ) -> dict[str, Any]:
        """新建或修改采购草稿，不提交。写入前核对公司、供应商、数量、金额、交期。

        不传 name 为新建；更新必须携带最新详情中的 modified 作为 expected_modified。
        fields 只包含业务字段；不要传 doctype/docstatus/status/workflow_state/flags/owner 等元数据。
        items 等子表按完整列表保存，更新行保留行 name 和来源字段，避免意外删行。
        缺少必填项先查询元数据/详情并询问用户，不编造。超时后先查状态，不自动重试新建。
        """
        return await api.save_draft(erp, doctype, fields, name, expected_modified)

    return erp_tool(save_procurement_draft, read_only=False)
