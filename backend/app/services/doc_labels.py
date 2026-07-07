"""Bilingual (English + Chinese) labels for outgoing-document PDFs.

Per-company opt-in via Company.bilingual_labels (Settings → business details).
With the flag off — the default — every template renders byte-identically to
the English-only original. With it on, fixed labels become "ENGLISH 中文" so a
Chinese-reading customer can read the whole document while the English keeps
it usable for accountants and the ATO. User content is never translated.

Shared by both renderers (pdf_render / html_render) so the two stay in step.
"""

from __future__ import annotations

_ZH = {
    "RECEIPT": "收据",
    "INVOICE": "发票",
    "PAYMENT REQUEST": "付款请求",
    "BILL TO": "客户",
    "Receipt #": "收据编号",
    "Invoice #": "发票编号",
    "Payment Request": "付款请求编号",
    "Date": "日期",
    "Issue": "开具",
    "Paid": "已付",
    "Due": "到期",
    "Paid On": "付款日期",
    "Paid on": "付款日期",
    "Due Date": "到期日",
    "Expiration Date": "有效期至",
    "DESCRIPTION": "描述",
    "QTY": "数量",
    "UNIT PRICE": "单价",
    "AMOUNT": "金额",
    "SUBTOTAL": "小计",
    "Subtotal": "小计",
    "TOTAL (EXCL. GST)": "总计（不含GST）",
    "GST (10%)": "GST（10%）",
    "TOTAL (INCL. GST)": "总计（含GST）",
    "TOTAL": "总计",
    "PAYMENT METHOD": "付款方式",
    "Payment received": "已收款",
    "Method": "方式",
    "Account Name": "账户名",
    "Bank": "银行",
    "Account Number": "账号",
    "NOTES": "备注",
    "No GST has been charged. This is not a tax invoice.": "未收取GST，本单据非税务发票。",
}


def label(text: str, bilingual: bool) -> str:
    """`text` unchanged when bilingual is off or no translation exists;
    otherwise "ENGLISH 中文"."""
    if not bilingual:
        return text
    zh = _ZH.get(text)
    return f"{text} {zh}" if zh else text
