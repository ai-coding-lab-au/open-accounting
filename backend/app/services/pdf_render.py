"""Render outgoing documents (INVOICE / PAYMENT REQUEST / RECEIPT) to PDF.

Pure reportlab; no fonts loaded from the network, no external HTTP. The visual
style matches the user's existing Word template:
  * top-left: company name (deep blue, bold) + address/phone block
  * top-right: large document-type wordmark (deep blue)
  * blue header bar "BILL TO" on the left, "PAYMENT REQUEST / DATE / EXPIRATION" on the right
  * line items table with blue header row
  * subtotal row + big blue TOTAL band
  * bottom: "PAYMENT METHOD" table with bank details
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Iterable

from reportlab.lib.colors import Color, HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .pdf_fonts import font_for


# Documents are English-only, so use Times — ReportLab's built-in serif face,
# the PDF equivalent of the product-wide Times New Roman document style.
# It has the classic look for invoices and formal documents and needs no
# external font file.
FONT_BASE = "Times-Roman"
FONT_BOLD = "Times-Bold"
FONT_OBLIQUE = "Times-Italic"


# Matches the deep navy in the user's template (eyeballed from sample PDFs).
BRAND_BLUE = HexColor("#1F3864")
HEADER_BG = HexColor("#1F3864")
ROW_DIVIDER = HexColor("#D6DCE5")
LIGHT_GREY = HexColor("#F2F2F2")


DOC_TITLE = {
    "invoice": "INVOICE",
    "payment_request": "PAYMENT REQUEST",
    "receipt": "RECEIPT",
}


def _fmt_money(v: Decimal | float | int, currency: str = "AUD") -> str:
    n = float(v)
    return f"${n:,.2f}"


def _fmt_date(d: date | str | None) -> str:
    if d is None:
        return ""
    if isinstance(d, str):
        return d
    return d.strftime("%d/%m/%Y")


def _draw_text(c, x, y, text, *, font=FONT_BASE, size=10, color=black):
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawString(x, y, text or "")


def _draw_right(c, x, y, text, *, font=FONT_BASE, size=10, color=black):
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawRightString(x, y, text or "")


def _draw_bar(c, x, y, w, h, fill: Color):
    c.setFillColor(fill)
    c.rect(x, y, w, h, stroke=0, fill=1)


def _draw_box_with_header(c, x, y, w, header_h, label: str, body_h: float):
    """Header bar + bordered body box. Returns the inner top y for the body."""
    _draw_bar(c, x, y - header_h, w, header_h, HEADER_BG)
    c.setFillColor(white)
    c.setFont(FONT_BOLD, 10)
    c.drawString(x + 6, y - header_h + 4, label.upper())
    c.setStrokeColor(ROW_DIVIDER)
    c.setFillColor(white)
    c.rect(x, y - header_h - body_h, w, body_h, stroke=1, fill=0)
    return y - header_h - 6  # baseline for first body line


def render_document_pdf(
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
    is_gst_registered: bool = False,
) -> bytes:
    """Render one document to PDF and return the bytes.

    `company` keys: name, address_line1, address_line2, suburb, state, postcode,
                    phone, email, abn, bank_account_name, bank_name, bank_bsb,
                    bank_account_number, bank_swift.
    `customer` keys: name, address (multi-line str), email, phone.
    `lines`: list of {description, quantity, unit_price, amount}.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    margin = 18 * mm
    content_w = page_w - 2 * margin

    # ----- Top-left: company name + address -----
    y = page_h - margin
    c.setFillColor(BRAND_BLUE)
    company_name = company.get("name", "")
    c.setFont(font_for(company_name, FONT_BOLD), 20)
    c.drawString(margin, y - 16, company_name)

    y_addr = y - 16 - 16
    addr_lines = _company_address_lines(company)
    c.setFillColor(black)
    for line in addr_lines:
        c.setFont(font_for(line, FONT_BASE), 9.5)
        c.drawString(margin, y_addr, line)
        y_addr -= 12

    # ----- Top-right: document title wordmark -----
    # Size down for longer titles so a wide one (e.g. "PAYMENT REQUEST") doesn't
    # overrun toward the left column / company name.
    title_text = DOC_TITLE.get(doc_type, doc_type.upper())
    title_size = 28 if len(title_text) <= 8 else 18
    c.setFillColor(BRAND_BLUE)
    c.setFont(FONT_BOLD, title_size)
    c.drawRightString(page_w - margin, y - 16, title_text)

    # ----- BILL TO / document-info bands -----
    band_top = min(y_addr - 6, y - 70)
    band_top_y = band_top  # top edge of the header bar
    half_gap = 14
    left_w = (content_w - half_gap) * 0.55
    right_w = content_w - left_w - half_gap

    header_h = 16

    # Left: BILL TO header + body. Wrap each line to the box width FIRST so the
    # box can grow to fit a long name/address instead of clipping it.
    bill_max_w = left_w - 12  # 6pt padding each side
    bill_lines = _customer_bill_to_lines(customer)
    # Wrap with the font each line will actually be drawn in (CJK names get
    # the CJK face, whose glyph widths differ from Times).
    wrapped_bill: list[tuple[str, str]] = []
    for line in bill_lines:
        line_font = font_for(line, FONT_BASE)
        for piece in _wrap_line(c, line, line_font, 10, bill_max_w):
            wrapped_bill.append((piece, line_font))
    # Grow the box to fit the wrapped content (min 70 keeps the original look).
    body_h_left = max(70, 10 + 12 * len(wrapped_bill))
    body_top_left = _draw_box_with_header(
        c, margin, band_top_y, left_w, header_h, "BILL TO", body_h_left
    )
    ly = body_top_left - 6
    c.setFillColor(black)
    for wrapped, line_font in wrapped_bill:
        c.setFont(line_font, 10)
        c.drawString(margin + 6, ly, wrapped)
        ly -= 12

    # Right: document info (number/date/expiration). Build dynamically.
    right_x = margin + left_w + half_gap
    info_rows = _doc_info_rows(doc_type, doc_number, issue_date, expiration_date, paid_date)
    body_h_right = max(70, 4 + 14 * len(info_rows))
    _draw_bar(c, right_x, band_top_y - header_h, right_w, header_h, HEADER_BG)
    # Header row is a 2-col band where each label sits above its value (label in white).
    # Simpler: draw rows of (label, value).
    c.setStrokeColor(ROW_DIVIDER)
    c.setFillColor(white)
    c.rect(right_x, band_top_y - header_h - body_h_right, right_w, body_h_right, stroke=1, fill=0)
    ry = band_top_y - header_h - 4
    label_x = right_x + 6
    value_x = right_x + right_w - 6
    # Hide the white "header bar text" — we want the rows inside instead.
    # First "row" is technically the bar; put first label/value in white on the bar.
    if info_rows:
        first_label, first_value = info_rows[0]
        c.setFillColor(white)
        c.setFont(FONT_BOLD, 10)
        c.drawString(label_x, band_top_y - header_h + 4, first_label.upper())
        c.drawRightString(value_x, band_top_y - header_h + 4, first_value)
        rest = info_rows[1:]
    else:
        rest = []
    ry = band_top_y - header_h - 14
    c.setFillColor(black)
    for label, value in rest:
        c.setFont(FONT_BOLD, 9.5)
        c.drawString(label_x, ry, label.upper())
        c.setFont(FONT_BASE, 9.5)
        c.drawRightString(value_x, ry, value)
        ry -= 14

    # ----- Line items table -----
    table_top = min(body_top_left - body_h_left, ry) - 20
    table_x = margin
    table_w = content_w

    col_widths = [
        table_w * 0.54,  # description
        table_w * 0.10,  # qty
        table_w * 0.16,  # unit price
        table_w * 0.20,  # amount
    ]
    col_x = [table_x]
    for w in col_widths[:-1]:
        col_x.append(col_x[-1] + w)
    col_right = [col_x[i] + col_widths[i] for i in range(4)]

    # Header
    row_h = 18
    _draw_bar(c, table_x, table_top - row_h, table_w, row_h, HEADER_BG)
    c.setFillColor(white)
    c.setFont(FONT_BOLD, 10)
    c.drawString(col_x[0] + 6, table_top - row_h + 5, "DESCRIPTION")
    c.drawCentredString((col_x[1] + col_right[1]) / 2, table_top - row_h + 5, "QTY")
    c.drawRightString(col_right[2] - 6, table_top - row_h + 5, "UNIT PRICE")
    c.drawRightString(col_right[3] - 6, table_top - row_h + 5, "AMOUNT")

    # Rows — pad to at least 4 rows so the table doesn't look empty for single-line invoices
    body_lines = list(lines) + [None] * max(0, 4 - len(lines))
    row_y = table_top - row_h
    c.setStrokeColor(ROW_DIVIDER)
    for li in body_lines:
        row_y -= row_h
        c.setFillColor(black)
        c.line(table_x, row_y, table_x + table_w, row_y)
        if li is None:
            # Show a dash in AMOUNT cell to mirror the user's template
            c.setFont(FONT_BASE, 10)
            c.drawRightString(col_right[3] - 6, row_y + 5, "-")
            continue
        desc = str(li.get("description", ""))
        c.setFont(font_for(desc, FONT_BASE), 10)
        c.drawString(col_x[0] + 6, row_y + 5, desc)
        c.setFont(FONT_BASE, 10)
        qty = li.get("quantity")
        if qty is not None:
            qty_str = _fmt_qty(qty)
            c.drawCentredString((col_x[1] + col_right[1]) / 2, row_y + 5, qty_str)
        unit = li.get("unit_price")
        if unit is not None:
            # Show $0.00 explicitly for zero-priced lines (e.g. a second
            # visa subclass bundled into the first item's fee). Leaving
            # the cell blank looked like a render bug.
            c.drawRightString(col_right[2] - 6, row_y + 5, _fmt_money(unit, currency))
        amt = li.get("amount")
        if amt is not None:
            c.drawRightString(col_right[3] - 6, row_y + 5, _fmt_money(amt, currency))

    # Use the document's persisted totals directly:
    #   subtotal       = pre-GST
    #   gst_amount     = 10% when company is GST-registered, else 0
    #   total          = subtotal + gst_amount
    # The fees row, GST row and bottom TOTAL band all read from these,
    # so the PDF stays consistent with the list/detail UI and downstream
    # PR/Receipt copies. No reverse-derivation here.
    subtotal_d = Decimal(str(subtotal))
    gst_display = Decimal(str(gst_amount)) if is_gst_registered else Decimal("0.00")
    total_display = Decimal(str(total))

    # Subtotal row (right-aligned label + value, sitting below the table).
    # Label flips to "TOTAL (EXCL. GST)" when GST-registered so the reader
    # can distinguish it from the GST-inclusive total below.
    sub_y = row_y - row_h
    c.setFillColor(black)
    c.setFont(FONT_BASE, 10)
    subtotal_label = "TOTAL (EXCL. GST)" if is_gst_registered else "SUBTOTAL"
    c.drawRightString(col_right[2] - 6, sub_y + 5, subtotal_label)
    c.drawRightString(col_right[3] - 6, sub_y + 5, _fmt_money(subtotal_d, currency))

    next_y = sub_y - 4

    # GST row only if GST-registered (per user: not registered → suppress)
    if is_gst_registered:
        next_y -= row_h
        c.drawRightString(col_right[2] - 6, next_y + 5, "GST (10%)")
        c.drawRightString(col_right[3] - 6, next_y + 5, _fmt_money(gst_display, currency))

    # TOTAL big blue band — labelled "TOTAL (INCL. GST)" when GST-registered.
    # The band spans more of the table width when the label is longer so
    # the text and the amount don't collide.
    total_band_h = 24
    if is_gst_registered:
        # Stretch the band left by one extra column-width so "TOTAL (INCL. GST)"
        # has breathing room before the right-aligned amount.
        total_band_w = col_widths[1] + col_widths[2] + col_widths[3]
        total_band_x = col_x[1]
    else:
        total_band_w = col_widths[2] + col_widths[3]
        total_band_x = col_x[2]
    next_y -= total_band_h + 6
    _draw_bar(c, total_band_x, next_y, total_band_w, total_band_h, HEADER_BG)
    c.setFillColor(white)
    c.setFont(FONT_BOLD, 14 if is_gst_registered else 16)
    total_label = "TOTAL (INCL. GST)" if is_gst_registered else "TOTAL"
    c.drawString(total_band_x + 8, next_y + 6, total_label)
    c.drawRightString(total_band_x + total_band_w - 8, next_y + 6, _fmt_money(total_display, currency))

    # ----- Payment method table (bank details) -----
    pm_top = next_y - 30
    pm_rows = _payment_method_rows(doc_type, company, payment_method, paid_date)
    if pm_rows:
        pm_label_w = table_w * 0.35
        pm_value_w = table_w * 0.65
        pm_row_h = 16
        # Header
        _draw_bar(c, table_x, pm_top - pm_row_h, table_w, pm_row_h, HEADER_BG)
        c.setFillColor(white)
        c.setFont(FONT_BOLD, 10)
        c.drawString(table_x + 6, pm_top - pm_row_h + 4, "PAYMENT METHOD")
        # Rows
        rr_y = pm_top - pm_row_h
        c.setStrokeColor(ROW_DIVIDER)
        for label, value in pm_rows:
            rr_y -= pm_row_h
            c.setStrokeColor(ROW_DIVIDER)
            c.line(table_x, rr_y, table_x + table_w, rr_y)
            c.setFillColor(black)
            c.setFont(FONT_BASE, 9.5)
            c.drawString(table_x + 6, rr_y + 4, label)
            c.setFont(font_for(value, FONT_BASE), 9.5)
            c.drawString(table_x + pm_label_w + 6, rr_y + 4, value or "")
        # Outer border around the pm body
        body_h = pm_row_h * len(pm_rows)
        c.setStrokeColor(ROW_DIVIDER)
        c.rect(table_x, pm_top - pm_row_h - body_h, table_w, body_h, stroke=1, fill=0)
        pm_bottom = pm_top - pm_row_h - body_h
    else:
        pm_bottom = pm_top

    # ----- Notes (optional) -----
    if notes:
        ny = pm_bottom - 24
        c.setFillColor(BRAND_BLUE)
        c.setFont(FONT_BOLD, 9.5)
        c.drawString(margin, ny, "NOTES")
        ny -= 12
        c.setFillColor(black)
        for line in notes.splitlines():
            c.setFont(font_for(line, FONT_BASE), 9.5)
            c.drawString(margin, ny, line)
            ny -= 11

    # ----- GST disclosure footer (for not-registered businesses, ATO best practice) -----
    if not is_gst_registered:
        c.setFillColor(HexColor("#808080"))
        c.setFont(FONT_OBLIQUE, 8)
        c.drawString(margin, margin - 2, "No GST has been charged. This is not a tax invoice.")

    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _company_address_lines(company: dict) -> list[str]:
    out: list[str] = []
    if company.get("address_line1"):
        out.append(company["address_line1"])
    if company.get("address_line2"):
        out.append(company["address_line2"])
    locality_bits = [company.get("suburb"), company.get("state"), company.get("postcode")]
    locality = ", ".join(b for b in locality_bits if b)
    if locality:
        out.append(locality)
    if company.get("phone"):
        out.append(f"Phone: {company['phone']}")
    if company.get("email"):
        out.append(f"Email: {company['email']}")
    if company.get("abn"):
        out.append(f"ABN: {company['abn']}")
    return out


def _customer_bill_to_lines(customer: dict) -> list[str]:
    out: list[str] = []
    if customer.get("name"):
        out.append(customer["name"])
    # Address may be multi-line; label the first line "Address:" and indent the
    # rest under it.
    addr_lines = [ln.strip() for ln in (customer.get("address") or "").splitlines() if ln.strip()]
    for i, line in enumerate(addr_lines):
        out.append(f"Address: {line}" if i == 0 else line)
    if customer.get("abn"):
        out.append(f"ABN: {customer['abn']}")
    if customer.get("email"):
        out.append(f"Email: {customer['email']}")
    if customer.get("phone"):
        out.append(f"Phone: {customer['phone']}")
    return out


def _wrap_line(c, text: str, font: str, size: float, max_w: float, indent: str = "  ") -> list[str]:
    """Word-wrap `text` to `max_w` points. Continuation lines get `indent` so a
    wrapped "Address: ..." reads as one field, not several. A single word longer
    than max_w is hard-broken so it never overflows the box."""
    def fits(s: str) -> bool:
        return c.stringWidth(s, font, size) <= max_w

    out: list[str] = []
    words = text.split()
    if not words:
        return [text]
    cur = ""
    for w in words:
        prefix = indent if out else ""
        candidate = f"{cur} {w}".strip()
        if fits(prefix + candidate) or not cur:
            # Hard-break a word that can't fit even alone on a line.
            if not cur and not fits(prefix + w):
                piece = ""
                for ch in w:
                    if fits(prefix + piece + ch):
                        piece += ch
                    else:
                        out.append(prefix + piece)
                        piece = ch
                        prefix = indent
                cur = piece
            else:
                cur = candidate
        else:
            out.append(prefix + cur)
            cur = w
    if cur:
        out.append((indent if out else "") + cur)
    return out


def _doc_info_rows(
    doc_type: str,
    doc_number: str,
    issue_date: date,
    expiration_date: date | None,
    paid_date: date | None,
) -> list[tuple[str, str]]:
    if doc_type == "payment_request":
        rows = [("Payment Request", doc_number), ("Date", _fmt_date(issue_date))]
        if expiration_date:
            rows.append(("Expiration Date", _fmt_date(expiration_date)))
        return rows
    if doc_type == "receipt":
        rows = [("Receipt #", doc_number), ("Date", _fmt_date(issue_date))]
        if paid_date:
            rows.append(("Paid On", _fmt_date(paid_date)))
        return rows
    # invoice
    rows = [("Invoice #", doc_number), ("Date", _fmt_date(issue_date))]
    if expiration_date:
        rows.append(("Due Date", _fmt_date(expiration_date)))
    return rows


def _payment_method_rows(
    doc_type: str,
    company: dict,
    payment_method: str | None,
    paid_date: date | None,
) -> list[tuple[str, str]]:
    """For receipts we summarise what was already paid. For invoices/payment
    requests we print the bank account so the customer knows where to send money."""
    if doc_type == "receipt":
        rows = [("Payment received", "Yes")]
        if paid_date:
            rows.append(("Paid on", _fmt_date(paid_date)))
        if payment_method:
            rows.append(("Method", payment_method))
        # Also show the bank account they paid into, for the customer's records
        rows.extend(_bank_rows(company))
        return rows
    # invoice / payment_request — show bank details (only if filled in)
    rows = _bank_rows(company)
    return rows


def _bank_rows(company: dict) -> list[tuple[str, str]]:
    pairs = [
        ("Account Name", company.get("bank_account_name")),
        ("Bank", company.get("bank_name")),
        ("BSB", company.get("bank_bsb")),
        ("Account Number", company.get("bank_account_number")),
        ("SWIFT Code", company.get("bank_swift")),
    ]
    return [(k, v) for k, v in pairs if v]


def _fmt_qty(q) -> str:
    """Format a quantity: drop trailing zeros so "1.0000" displays as "1"."""
    s = str(Decimal(str(q)).normalize())
    if "." in s and "E" not in s.upper():
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-"):
        s = "0"
    return s
