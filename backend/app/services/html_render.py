"""Render outgoing documents (invoice / payment request / receipt) as HTML,
then to PDF via Playwright/Chromium.

This is the preferred renderer; CSS handles wrapping/layout that the reportlab
renderer (pdf_render.py) does by hand. Callers fall back to pdf_render when
Chromium is unavailable — see HtmlRenderUnavailable.

The public entry point `render_document_pdf` takes the SAME arguments as
pdf_render.render_document_pdf, so the two are drop-in interchangeable.
"""

from __future__ import annotations

import html
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

NAVY = "#1f3a5f"

DOC_TITLES = {
    "receipt": "RECEIPT",
}


class HtmlRenderUnavailable(RuntimeError):
    """Playwright/Chromium is not installed or failed to launch. Caller should
    fall back to the reportlab renderer."""


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _fmt_money(v: Decimal | None, currency: str = "AUD") -> str:
    if v is None:
        return ""
    return f"{Decimal(v):,.2f}"


def _fmt_date(d: date | None) -> str:
    return d.strftime("%d %b %Y") if d else ""


def _company_lines(company: dict) -> str:
    bits = []
    for key in ("address_line1", "address_line2"):
        if company.get(key):
            bits.append(_esc(company[key]))
    loc = " ".join(
        _esc(company[k]) for k in ("suburb", "state", "postcode") if company.get(k)
    )
    if loc:
        bits.append(loc)
    meta = " · ".join(
        _esc(company[k]) for k in ("abn", "phone", "email") if company.get(k)
    )
    addr = ", ".join(bits)
    out = []
    if addr:
        out.append(addr)
    if meta:
        out.append(meta)
    return "<br>".join(out)


def _bill_to(customer: dict) -> str:
    rows = [f"<strong>{_esc(customer.get('name'))}</strong>"]
    if customer.get("address"):
        # Address may be multi-line (\n separated).
        addr = "<br>".join(_esc(ln) for ln in str(customer["address"]).splitlines() if ln.strip())
        rows.append(f'<span class="lbl">Address:</span> {addr}')
    if customer.get("abn"):
        rows.append(f'<span class="lbl">ABN:</span> {_esc(customer["abn"])}')
    if customer.get("email"):
        rows.append(f'<span class="lbl">Email:</span> {_esc(customer["email"])}')
    if customer.get("phone"):
        rows.append(f'<span class="lbl">Phone:</span> {_esc(customer["phone"])}')
    return "<br>".join(rows)


def _line_rows(lines: list[dict], currency: str) -> str:
    out = []
    for ln in lines:
        out.append(
            "<tr>"
            f"<td>{_esc(ln.get('description'))}</td>"
            f"<td class='num'>{_esc(_fmt_qty(ln.get('quantity')))}</td>"
            f"<td class='num'>{_fmt_money(ln.get('unit_price'), currency)}</td>"
            f"<td class='num'>{_fmt_money(ln.get('amount'), currency)}</td>"
            "</tr>"
        )
    return "".join(out)


def _fmt_qty(q) -> str:
    if q is None:
        return ""
    s = str(Decimal(str(q)).normalize())
    if "." in s and "E" not in s.upper():
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _bank_rows(company: dict) -> str:
    pairs = [
        ("Account Name", company.get("bank_account_name")),
        ("Bank", company.get("bank_name")),
        ("BSB", company.get("bank_bsb")),
        ("Account Number", company.get("bank_account_number")),
        ("SWIFT Code", company.get("bank_swift")),
    ]
    rows = [
        f"<tr><td class='k'>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in pairs
        if v
    ]
    return "".join(rows)


def _receipt_rows(payment_method: str | None, paid_date: date | None) -> str:
    rows = ["<tr><td class='k'>Payment received</td><td>Yes</td></tr>"]
    if paid_date:
        rows.append(f"<tr><td class='k'>Paid on</td><td>{_esc(_fmt_date(paid_date))}</td></tr>")
    if payment_method:
        rows.append(f"<tr><td class='k'>Method</td><td>{_esc(payment_method)}</td></tr>")
    return "".join(rows)


def build_html(
    *,
    doc_type: str,
    doc_number: str,
    issue_date: date,
    expiration_date: date | None = None,
    company: dict,
    customer: dict,
    lines: list[dict],
    subtotal: Decimal,
    gst_amount: Decimal,
    total: Decimal,
    currency: str = "AUD",
    paid_date: date | None = None,
    payment_method: str | None = None,
    notes: str | None = None,
    is_gst_registered: bool = True,
) -> str:
    title = DOC_TITLES.get(doc_type, doc_type.replace("_", " ").upper())

    # Document-info rows differ a little per type.
    meta_rows = [f"{_esc(doc_number)}", f"Issue {_esc(_fmt_date(issue_date))}"]
    if doc_type == "receipt" and paid_date:
        meta_rows.append(f"Paid {_esc(_fmt_date(paid_date))}")
    elif expiration_date:
        meta_rows.append(f"Due {_esc(_fmt_date(expiration_date))}")
    meta_html = "<br>".join(meta_rows)

    # Payment block: receipts summarise what was paid + the account; invoices /
    # PRs show where to send money.
    if doc_type == "receipt":
        pay_rows = _receipt_rows(payment_method, paid_date) + _bank_rows(company)
    else:
        pay_rows = _bank_rows(company)
    pay_block = (
        f'<div class="pay"><div class="h">PAYMENT METHOD</div>'
        f"<table>{pay_rows}</table></div>"
        if pay_rows
        else ""
    )

    gst_row = (
        f'<tr><td>GST (10%)</td><td class="num">{_fmt_money(gst_amount, currency)}</td></tr>'
        if is_gst_registered
        else ""
    )
    notes_block = (
        f'<div class="notes"><div class="h2">NOTES</div><div>{_esc(notes).replace(chr(10), "<br>")}</div></div>'
        if notes
        else ""
    )
    gst_disclaimer = (
        '<div class="disclaimer">No GST has been charged. This is not a tax invoice.</div>'
        if not is_gst_registered
        else ""
    )

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size:A4; margin:0; }}
* {{ box-sizing:border-box; }}
body {{ font-family:'Times New Roman',Times,'Noto Sans CJK SC','Microsoft YaHei','PingFang SC',serif; color:#1a1a1a; font-size:10.5pt; margin:0; }}
.sheet {{ padding:18mm; }}
.band {{ background:{NAVY}; height:8px; }}
.head {{ display:flex; justify-content:space-between; align-items:flex-start; margin-top:20px; }}
.co .name {{ font-size:17pt; font-weight:bold; color:{NAVY}; }}
.co small {{ color:#666; font-size:9pt; line-height:1.6; }}
.ti {{ text-align:right; }} .ti h1 {{ margin:0; font-size:20pt; color:{NAVY}; letter-spacing:2px; }}
.ti .meta {{ font-size:9.5pt; color:#444; line-height:1.7; margin-top:6px; }}
.parties {{ margin-top:26px; }}
.box .h {{ background:{NAVY}; color:#fff; font-size:9pt; letter-spacing:1.5px; padding:5px 10px; }}
.box .b {{ border:1px solid #ddd; border-top:none; padding:10px; line-height:1.5; font-size:9.5pt; }}
.box .b .lbl {{ color:#888; }}
table.items {{ width:100%; border-collapse:collapse; margin-top:26px; }}
table.items th {{ background:{NAVY}; color:#fff; text-align:left; padding:7px 10px; font-size:9.5pt; letter-spacing:.5px; }}
table.items th.num, table.items td.num {{ text-align:right; }}
table.items td {{ padding:8px 10px; border-bottom:1px solid #eee; }}
table.items tbody tr:nth-child(even) {{ background:#f7f9fc; }}
.tot {{ width:42%; margin-left:58%; margin-top:12px; border-collapse:collapse; }}
.tot td {{ padding:6px 10px; }} .tot .num {{ text-align:right; }}
.tot .grand td {{ background:{NAVY}; color:#fff; font-weight:bold; font-size:12pt; }}
.pay {{ margin-top:28px; }} .pay .h {{ background:{NAVY}; color:#fff; padding:5px 10px; font-size:9.5pt; letter-spacing:1px; }}
.pay table {{ width:100%; border-collapse:collapse; }} .pay td {{ padding:5px 10px; border-bottom:1px solid #eee; font-size:9.5pt; }} .pay .k {{ color:#888; width:32%; }}
.notes {{ margin-top:22px; font-size:9.5pt; }} .notes .h2 {{ color:{NAVY}; font-weight:bold; letter-spacing:1px; margin-bottom:4px; }}
.disclaimer {{ margin-top:18px; color:#888; font-style:italic; font-size:9pt; }}
.foot {{ margin-top:30px; text-align:center; color:#999; font-size:8.5pt; border-top:1px solid #eee; padding-top:10px; }}
</style></head><body>
<div class="band"></div>
<div class="sheet">
  <div class="head">
    <div class="co"><div class="name">{_esc(company.get('name'))}</div><small>{_company_lines(company)}</small></div>
    <div class="ti"><h1>{_esc(title)}</h1><div class="meta">{meta_html}</div></div>
  </div>
  <div class="parties">
    <div class="box"><div class="h">BILL TO</div><div class="b">{_bill_to(customer)}</div></div>
  </div>
  <table class="items"><thead><tr><th>DESCRIPTION</th><th class="num">QTY</th><th class="num">UNIT PRICE</th><th class="num">AMOUNT</th></tr></thead><tbody>{_line_rows(lines, currency)}</tbody></table>
  <table class="tot"><tr><td>Subtotal</td><td class="num">{_fmt_money(subtotal, currency)}</td></tr>{gst_row}<tr class="grand"><td>TOTAL ({_esc(currency)})</td><td class="num">{_fmt_money(total, currency)}</td></tr></table>
  {pay_block}
  {notes_block}
  {gst_disclaimer}
  <div class="foot">{_esc(company.get('name'))}{(' · ABN ' + _esc(company['abn'])) if company.get('abn') else ''}</div>
</div>
</body></html>"""


def render_document_pdf(**kwargs) -> bytes:
    """Render an outgoing document to PDF via HTML + Chromium.

    Same signature as pdf_render.render_document_pdf. Raises
    HtmlRenderUnavailable if Chromium can't be launched, so the caller can fall
    back to the reportlab renderer.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise HtmlRenderUnavailable("playwright is not installed") from exc

    html_str = build_html(**kwargs)
    tmp = Path(tempfile.mkdtemp(prefix="doc_pdf_"))
    pdf_path = tmp / "doc.pdf"
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # Chromium binary missing / launch failed
                raise HtmlRenderUnavailable(f"Chromium launch failed: {exc}") from exc
            page = browser.new_page()
            page.set_content(html_str, wait_until="networkidle")
            page.pdf(path=str(pdf_path), format="A4", print_background=True)
            browser.close()
        return pdf_path.read_bytes()
    finally:
        try:
            pdf_path.unlink(missing_ok=True)
            tmp.rmdir()
        except OSError:
            pass
