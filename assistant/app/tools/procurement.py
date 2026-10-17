"""采购只读工具；角色权限和 ERPNext 当前用户权限共同限定访问范围。"""

import json
from typing import Any
from urllib.parse import quote

from agentscope.tool import FunctionTool

from app.clients.erpnext import procurement as api
from app.clients.erpnext.client import ERPNext
from app.runtime.context import RunContext
from app.tools.common import erp_tool


def create_list_documents_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def list_procurement_documents(
        doctype: api.ProcurementDocType,
        fields: list[str] | None = None,
        filters: api.Filters | None = None,
        or_filters: api.Filters | None = None,
        order_by: str | None = None,
        limit_start: api.PageStart = 0,
        limit_page_length: api.PageSize = 20,
    ) -> dict[str, Any]:
        """分页查询采购相关记录。filters 为 AND，or_filters 为 OR，保留原生条件。

        fields 默认只返回 name。返回原生 data，不含总数；满页须继续查询。
        采购需求应筛选 material_request_type=Purchase；历史交易应区分 docstatus。
        子表明细请使用 get_procurement_document，不把单据列表当作完整明细。
        """
        return await api.list_documents(
            erp,
            doctype,
            fields=fields,
            filters=filters,
            or_filters=or_filters,
            order_by=order_by,
            limit_start=limit_start,
            limit_page_length=limit_page_length,
        )

    return erp_tool(list_procurement_documents)


def create_get_document_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def get_procurement_document(
        doctype: api.ProcurementDocType,
        name: api.DocumentName,
    ) -> dict[str, Any]:
        """读取已保存单据及子表明细，返回 data；包含 modified 供后续变更核对。

        可读取物料单位、供应商、价格、单据来源行及状态。不能代表页面未保存内容。
        """
        return await erp.get(api.resource_path(doctype, name))

    return erp_tool(get_procurement_document)


def create_get_schema_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def get_procurement_schema(
        doctype: api.ProcurementSchemaType,
    ) -> dict[str, Any]:
        """读取 v16 DocType 元数据，包括字段类型、必填项和链接；不代表写权限。"""
        return await erp.get("/api/v2/doctype/" + quote(doctype, safe="") + "/meta")

    return erp_tool(get_procurement_schema)


def create_run_report_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def run_procurement_report(
        report_name: api.ProcurementReport,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        """执行采购报表，返回原生 message（columns/result 等）。

        filters 使用原生字段，如 company/from_date/to_date/item_code/supplier。
        参数和必填项依具体报表；没有结果或仍在生成的预生成报表不能当成零值。
        比价须区分币种、税费、库存单位、报价有效期及草稿/已提交状态。
        """
        return await erp.get(
            "/api/method/frappe.desk.query_report.run",
            {
                "report_name": report_name,
                "filters": json.dumps(filters, ensure_ascii=False),
            },
        )

    return erp_tool(run_procurement_report)


def create_stock_tool(context: RunContext) -> FunctionTool:
    erp: ERPNext = context.clients["erpnext"]

    async def get_procurement_stock(
        item_code: api.DocumentName,
        warehouse: api.DocumentName,
    ) -> dict[str, Any]:
        """查询指定物料和仓库的 Bin：现存、预留、已订购、已申请及预计数量。

        数量按库存单位解释；空 data 表示无可见记录，不能直接推断零库存或缺口。
        采购数量还需结合需求、交期和在途，不能只由 actual_qty 推算。
        """
        return await api.list_documents(
            erp,
            "Bin",
            fields=[
                "item_code",
                "warehouse",
                "actual_qty",
                "reserved_qty",
                "ordered_qty",
                "indented_qty",
                "projected_qty",
            ],
            filters={"item_code": item_code, "warehouse": warehouse},
            limit_page_length=1,
        )

    return erp_tool(get_procurement_stock)
