"""采购查询的原生接口与允许访问的业务对象。"""

import json
from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import Field

from .client import ERPNext

# 过滤条件沿用 Frappe 的列表或字典形式，操作符和查询值由接口解释。
type Filters = list[list[Any]] | dict[str, Any]

# 只开放采购所需对象；不开放 User、凭据或任意 DocType。
type ProcurementDocType = Literal[
    "Item",
    "Item Group",
    "UOM",
    "Supplier",
    "Supplier Group",
    "Contact",
    "Address",
    "Company",
    "Warehouse",
    "Bin",
    "Item Price",
    "Price List",
    "Currency",
    "Material Request",
    "Request for Quotation",
    "Supplier Quotation",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Invoice",
    "Quality Inspection",
    "Payment Entry",
    "Payment Terms Template",
    "Purchase Taxes and Charges Template",
    "Cost Center",
    "Project",
    "Supplier Scorecard",
    "Blanket Order",
]
type ProcurementSchemaType = (
    ProcurementDocType
    | Literal[
        "Item Default",
        "Item Supplier",
        "UOM Conversion Detail",
        "Material Request Item",
        "Request for Quotation Item",
        "Request for Quotation Supplier",
        "Supplier Quotation Item",
        "Purchase Order Item",
        "Purchase Receipt Item",
        "Purchase Invoice Item",
        "Purchase Taxes and Charges",
        "Payment Schedule",
        "Quality Inspection Reading",
    ]
)
type ProcurementReport = Literal[
    "Stock Balance",
    "Stock Projected Qty",
    "Supplier Quotation Comparison",
    "Purchase Order Analysis",
    "Purchase Analytics",
    "Received Items To Be Billed",
    "Accounts Payable",
]
type DocumentName = Annotated[str, Field(min_length=1, max_length=240)]
type PageSize = Annotated[int, Field(ge=1, le=200)]
type PageStart = Annotated[int, Field(ge=0)]


def resource_path(doctype: str, name: str | None = None) -> str:
    """分别编码对象和名称，避免名称中的 /、? 等改变请求目标。"""
    path = "/api/resource/" + quote(doctype, safe="")
    return path if name is None else path + "/" + quote(name, safe="")


async def list_documents(
    erp: ERPNext,
    doctype: ProcurementDocType,
    *,
    fields: list[str] | None = None,
    filters: Filters | None = None,
    or_filters: Filters | None = None,
    order_by: str | None = None,
    limit_start: int = 0,
    limit_page_length: int = 20,
) -> dict[str, Any]:
    """保留原生查询值、分页和响应，不将单页结果误报为全集。"""
    params: dict[str, str | int] = {
        "limit_start": limit_start,
        "limit_page_length": limit_page_length,
    }
    for key, value in (
        ("fields", fields),
        ("filters", filters),
        ("or_filters", or_filters),
    ):
        if value is not None:
            params[key] = json.dumps(value, ensure_ascii=False)
    if order_by is not None:
        params["order_by"] = order_by
    return await erp.get(resource_path(doctype), params)
