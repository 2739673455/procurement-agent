import type { PageContext } from "./types";

/** 包含单据类型和名称的表单数据，其他业务字段按元数据读取。 */
interface Doc extends Record<string, unknown> { doctype: string; name: string }
/** 聊天面板使用的 Frappe 登录、路由和表单元数据接口。 */
interface Frappe {
    session: { user: string };
    csrf_token: string;
    app?: { sidebar?: { sidebar_title?: string } };
    /** 读取当前页面的路由片段。 */
    get_route(): string[];
    /** 读取指定单据类型的字段元数据。 */
    get_meta(doctype: string): { fields: { fieldname: string; fieldtype: string }[] };
    /** 跳转到指定页面或单据。 */
    set_route(...route: string[]): void;
    router: { on(event: string, handler: () => void): void; off(event: string, handler: () => void): void };
}
declare global {
    /** 由 Frappe 页面提供的运行对象、当前表单和事件接口。 */
    interface Window {
        frappe: Frappe;
        cur_frm?: { doc: Doc; doctype: string; is_new(): boolean; is_dirty(): boolean };
        $: (target: Document) => { on(events: string, handler: () => void): void; off(events: string, handler: () => void): void };
    }
}
export const frappe = window.frappe;

/** 每次发送时读取包含未保存内容的表单快照，访问权限由服务端检查。 */
export function pageSnapshot(): PageContext {
    const route = frappe.get_route();
    const frm = window.cur_frm;
    if (route[0] !== "Form" || !frm?.doc || route[1] !== frm.doctype || route[2] !== frm.doc.name) return { route };
    /** 按元数据整理业务字段与子表，跳过密码字段和未提供的值。 */
    function fields(doc: Doc): Record<string, unknown> {
        const result: Record<string, unknown> = {};
        for (const field of frappe.get_meta(doc.doctype).fields) {
            const value = doc[field.fieldname];
            if (field.fieldtype === "Password" || value === undefined) continue;
            result[field.fieldname] = ["Table", "Table MultiSelect"].includes(field.fieldtype)
                ? ((value || []) as Doc[]).map(fields) : value;
        }
        return result;
    }
    return { route, doctype: frm.doctype, name: frm.doc.name,
        is_new: Boolean(frm.is_new()), is_dirty: Boolean(frm.is_dirty()), doc: fields(frm.doc) };
}
