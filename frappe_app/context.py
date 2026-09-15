"""整理当前页面的表单快照和用户选择的附件。"""

import base64
import mimetypes

import frappe


def page_snapshot(value):
    """校验单据访问权限，保留表单业务字段；快照仍是用户提交的数据。"""
    if not value:
        return None
    page = frappe.parse_json(value)
    result = {
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

    def fields(doctype, data):
        output = {}
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


def attachment_payloads(value):
    """读取当前用户有权限访问的本地附件，不接受浏览器提供的文件路径。"""
    result = []
    for identifier in frappe.parse_json(value) or []:
        file = frappe.get_doc("File", identifier)
        file.check_permission("read")
        if file.is_folder or file.is_remote_file:
            frappe.throw("请选择已上传的本地文件。")
        # 保留上传文件的原始字节，避免默认文本解码改变 PDF 或图片内容。
        content = file.get_content(encodings=())
        if isinstance(content, str):
            content = content.encode("utf-8")
        result.append(
            {
                "id": file.name,
                "name": file.file_name,
                "media_type": mimetypes.guess_type(file.file_name)[0]
                or "application/octet-stream",
                "data": base64.b64encode(content).decode("ascii"),
            }
        )
    return result
