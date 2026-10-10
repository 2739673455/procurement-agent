"""整理当前页面的表单快照。"""

from typing import Any

import frappe


def page_snapshot(value: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """校验单据访问权限并过滤表单字段；快照内容由用户提交。"""
    if not value:
        return None
    page = frappe.parse_json(value)
    result: dict[str, Any] = {
        "route": page.get("route", []),
        "doctype": page.get("doctype"),
        "name": page.get("name"),
        "is_new": bool(page.get("is_new")),
        "is_dirty": bool(page.get("is_dirty")),
        "doc": None,
    }
    if not result["doctype"]:
        return result
    document = (
        frappe.new_doc(result["doctype"])
        if result["is_new"]
        else frappe.get_doc(result["doctype"], result["name"])
    )
    document.check_permission("create" if result["is_new"] else "read")
    levels = document.get_permlevel_access("read")

    def fields(doctype: str, data: dict[str, Any]) -> dict[str, Any]:
        """按字段访问级别过滤业务数据，并递归整理子表。"""
        output: dict[str, Any] = {}
        for df in frappe.get_meta(doctype).fields:
            if (
                df.fieldtype == "Password"
                or df.permlevel not in levels
                or df.fieldname not in data
            ):
                continue
            value = data[df.fieldname]
            if df.fieldtype in ("Table", "Table MultiSelect"):
                output[df.fieldname] = [fields(df.options, row) for row in value or []]
            elif df.fieldtype not in (
                "Section Break",
                "Column Break",
                "Tab Break",
                "HTML",
                "Button",
            ):
                output[df.fieldname] = value
        return output

    result["doc"] = fields(result["doctype"], page.get("doc") or {})
    return result
