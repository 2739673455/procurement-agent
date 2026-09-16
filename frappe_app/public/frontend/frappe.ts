import type { PageContext } from "./types";

interface Doc extends Record<string, unknown> { doctype: string; name: string }
interface Frappe {
    session: { user: string };
    csrf_token: string;
    app?: { sidebar?: { sidebar_title?: string } };
    get_route(): string[];
    get_meta(doctype: string): { fields: { fieldname: string; fieldtype: string }[] };
    set_route(...route: string[]): void;
    router: { on(event: string, handler: () => void): void; off(event: string, handler: () => void): void };
}
declare global {
    interface Window {
        frappe: Frappe;
        cur_frm?: { doc: Doc; doctype: string; is_new(): boolean; is_dirty(): boolean };
        $: (target: Document) => { on(events: string, handler: () => void): void; off(events: string, handler: () => void): void };
    }
}
export const frappe = window.frappe;

/** 每次发送时读取表单快照，包含未保存内容，权限仍由服务端检查。 */
export function pageSnapshot(): PageContext {
    const route = frappe.get_route();
    const frm = window.cur_frm;
    if (route[0] !== "Form" || !frm?.doc || route[1] !== frm.doctype || route[2] !== frm.doc.name) return { route };
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
