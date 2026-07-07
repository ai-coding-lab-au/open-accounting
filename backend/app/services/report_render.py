"""PDF rendering for the four M3 reports.

Pure local reportlab — no network. Style is intentionally simple (A4, two
colour palette, mono columns for numbers) so the output is print-friendly
and accountant-friendly.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from ..models.master import Company
from ..models.company import StaffMember
from .staff import display_label as _staff_display_label
from .pdf_fonts import FONT_CJK, font_for, needs_cjk
from .pdf_render import FONT_BASE, FONT_BOLD


HEAD = HexColor("#2F3645")
ACCENT = HexColor("#3B6EA8")
ROW_ALT = HexColor("#F5F7FA")
BORDER = HexColor("#CFD4DA")
MUTED = HexColor("#6B7280")


def _esc(v: object) -> str:
    """Escape a value for safe inclusion inside a reportlab Paragraph.

    Paragraph parses a tiny XML subset (<b>, <i>, <br/>, <font>...). User-supplied
    strings containing '<', '>', or '&' would either crash the parser or inject markup.
    """
    if v is None:
        return ""
    s = str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_font(v: object) -> str:
    """_esc plus a CJK font tag when the text needs glyphs Times lacks
    (Chinese company/contact names, imported statement memos). English text
    passes through unchanged, so default reports render identically."""
    s = _esc(v)
    if needs_cjk(s) and font_for(s, FONT_BASE) == FONT_CJK:
        return f'<font name="{FONT_CJK}">{s}</font>'
    return s


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    # Use the shared Times face from pdf_render so generated PDFs stay aligned
    # with the product-wide Times New Roman document style.
    s = {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontName=FONT_BOLD, fontSize=16, textColor=HEAD,
            spaceAfter=2, alignment=0,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontName=FONT_BASE, fontSize=9.5, textColor=MUTED,
            spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontName=FONT_BOLD, fontSize=11.5, textColor=HEAD,
            spaceBefore=10, spaceAfter=4,
        ),
        "small": ParagraphStyle(
            "small", parent=base["Normal"], fontName=FONT_BASE, fontSize=8.5, textColor=MUTED,
        ),
        "kv_label": ParagraphStyle(
            "kv_label", parent=base["Normal"], fontName=FONT_BASE, fontSize=8.5, textColor=MUTED,
        ),
        "kv_value": ParagraphStyle(
            "kv_value", parent=base["Normal"], fontName=FONT_BASE, fontSize=10, textColor=HEAD,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontName=FONT_BASE, fontSize=9.5,
        ),
        "footer": ParagraphStyle(
            "footer", parent=base["Normal"], fontName=FONT_BASE, fontSize=7.5, textColor=MUTED,
            alignment=1,
        ),
    }
    return s


def _money(x: Decimal | None) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    n = abs(x)
    s = f"{n:,.2f}"
    return f"{sign}${s}"


def _company_header_row(company: Company, signing_agent: StaffMember | None = None) -> Table:
    name = company.name or "—"
    bits: list[str] = []
    if getattr(company, "abn", None):
        bits.append(f"ABN {company.abn}")
    if getattr(company, "acn", None):
        bits.append(f"ACN {company.acn}")
    if signing_agent is not None:
        bits.append(_staff_display_label(signing_agent))
    addr_parts = [
        getattr(company, "address_line1", None),
        getattr(company, "suburb", None),
        getattr(company, "state", None),
        getattr(company, "postcode", None),
    ]
    addr = ", ".join([p for p in addr_parts if p])

    sty = _styles()
    left = Paragraph(f"<b>{_esc_font(name)}</b>", sty["kv_value"])
    sub_lines = []
    if bits:
        sub_lines.append(_esc_font(" · ".join(bits)))
    if addr:
        sub_lines.append(_esc_font(addr))
    right = Paragraph(
        "<br/>".join(sub_lines) if sub_lines else "",
        sty["small"],
    )
    t = Table([[left, right]], colWidths=[90 * mm, 90 * mm])
    t.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, BORDER),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])
    )
    return t


def _doc(buffer: io.BytesIO, *, landscape_mode: bool = False) -> BaseDocTemplate:
    pagesize = landscape(A4) if landscape_mode else A4
    # invariant=True freezes ReportLab's internal CreationDate/ModDate and
    # the /ID trailer entry, so identical inputs produce byte-identical
    # PDFs. Without it, /ID is seeded from a fresh timestamp on every
    # render (M5 finding #5).
    doc = BaseDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        invariant=True,
    )
    width, height = pagesize
    frame = Frame(
        doc.leftMargin, doc.bottomMargin,
        width - doc.leftMargin - doc.rightMargin,
        height - doc.topMargin - doc.bottomMargin,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    doc.addPageTemplates([PageTemplate(id="A4", frames=[frame])])
    return doc


def _footer_text() -> str:
    return f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · Internal use"


# ---------------------------------------------------------------------------
# 1) Bank statement
# ---------------------------------------------------------------------------


def render_bank_statement_pdf(*, company: Company, data: dict, signing_agent: StaffMember | None = None) -> bytes:
    sty = _styles()
    buf = io.BytesIO()
    doc = _doc(buf, landscape_mode=False)
    flow: list[Any] = []
    flow.append(_company_header_row(company, signing_agent))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("Bank Statement", sty["title"]))
    flow.append(Paragraph(
        f"{data['bank_account_name']} · "
        f"{data['period_start'].strftime('%d %b %Y')} – "
        f"{data['period_end'].strftime('%d %b %Y')}",
        sty["subtitle"],
    ))

    summary = Table(
        [
            [Paragraph("Opening balance", sty["kv_label"]),
             Paragraph(_money(data["opening_balance"]), sty["kv_value"]),
             Paragraph("Total in", sty["kv_label"]),
             Paragraph(_money(data["total_in"]), sty["kv_value"])],
            [Paragraph("Closing balance", sty["kv_label"]),
             Paragraph(_money(data["closing_balance"]), sty["kv_value"]),
             Paragraph("Total out", sty["kv_label"]),
             Paragraph(_money(data["total_out"]), sty["kv_value"])],
        ],
        colWidths=[35 * mm, 40 * mm, 35 * mm, 40 * mm],
    )
    summary.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(summary)
    flow.append(Spacer(1, 10))

    head = ["Date", "Description", "Category", "In", "Out", "Balance"]
    rows: list[list[Any]] = [head]
    for r in data["rows"]:
        desc = r["memo"] or r["counter_party_name"] or ""
        cat = (
            f"{r['account_code']} · {r['account_name']}"
            if r["account_code"]
            else "—"
        )
        in_str = _money(r["amount"]) if r["direction"] == "in" else ""
        out_str = _money(r["amount"]) if r["direction"] == "out" else ""
        rows.append([
            r["occurred_at"].strftime("%d %b"),
            Paragraph(_esc_font(desc), sty["body"]),
            Paragraph(_esc_font(cat), sty["body"]),
            in_str,
            out_str,
            _money(r["running_balance"]),
        ])

    tbl = Table(
        rows,
        colWidths=[15 * mm, 60 * mm, 45 * mm, 20 * mm, 20 * mm, 22 * mm],
        repeatRows=1,
    )
    tbl_style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEAD),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 8.5),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("ALIGN", (3, 0), (-1, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, BORDER),
    ]
    for i in range(1, len(rows)):
        if i % 2 == 0:
            tbl_style.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    tbl.setStyle(TableStyle(tbl_style))
    flow.append(tbl)

    flow.append(Spacer(1, 8))
    flow.append(Paragraph(_footer_text(), sty["footer"]))

    doc.build(flow)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 2) P&L
# ---------------------------------------------------------------------------


def render_pnl_pdf(*, company: Company, data: dict, signing_agent: StaffMember | None = None) -> bytes:
    sty = _styles()
    buf = io.BytesIO()
    doc = _doc(buf)
    flow: list[Any] = []
    flow.append(_company_header_row(company, signing_agent))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("Profit & Loss Statement", sty["title"]))
    flow.append(Paragraph(
        f"{data['period_start'].strftime('%d %b %Y')} – "
        f"{data['period_end'].strftime('%d %b %Y')}",
        sty["subtitle"],
    ))

    def section(label: str, rows: list[dict], total: Decimal) -> Table:
        body: list[list[Any]] = [[Paragraph(f"<b>{_esc(label)}</b>", sty["body"]), ""]]
        for r in rows:
            body.append([
                Paragraph(f"{_esc(r['code'])} · {_esc_font(r['name'])}", sty["body"]),
                _money(r["total"]),
            ])
        body.append([
            Paragraph(f"<b>Total {_esc(label.lower())}</b>", sty["body"]),
            Paragraph(f"<b>{_money(total)}</b>", sty["body"]),
        ])
        t = Table(body, colWidths=[120 * mm, 40 * mm])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, HEAD),
            ("LINEABOVE", (0, -1), (-1, -1), 0.5, HEAD),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    flow.append(section("Income", data["income_rows"], data["total_income"]))
    flow.append(Spacer(1, 8))
    if data["cogs_rows"]:
        flow.append(section("Cost of Sales", data["cogs_rows"], data["total_cogs"]))
        flow.append(Spacer(1, 6))
        gp = Table(
            [[Paragraph("<b>Gross profit</b>", sty["body"]),
              Paragraph(f"<b>{_money(data['gross_profit'])}</b>", sty["body"])]],
            colWidths=[120 * mm, 40 * mm],
        )
        gp.setStyle(TableStyle([
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("BACKGROUND", (0, 0), (-1, 0), ROW_ALT),
            ("BOX", (0, 0), (-1, 0), 0.5, BORDER),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]))
        flow.append(gp)
        flow.append(Spacer(1, 8))
    flow.append(section("Expenses", data["expense_rows"], data["total_expense"]))
    flow.append(Spacer(1, 8))
    np_tbl = Table(
        [[Paragraph("<b>Net profit</b>", sty["body"]),
          Paragraph(f"<b>{_money(data['net_profit'])}</b>", sty["body"])]],
        colWidths=[120 * mm, 40 * mm],
    )
    np_tbl.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 11),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    flow.append(np_tbl)

    if data["uncategorised_in"] > 0 or data["uncategorised_out"] > 0:
        flow.append(Spacer(1, 10))
        flow.append(Paragraph(
            f"Uncategorised: <b>{_money(data['uncategorised_in'])}</b> in, "
            f"<b>{_money(data['uncategorised_out'])}</b> out — categorise these "
            f"transactions to include them in the P&amp;L.",
            sty["small"],
        ))

    flow.append(Spacer(1, 10))
    flow.append(Paragraph(_footer_text(), sty["footer"]))
    doc.build(flow)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 4) BAS
# ---------------------------------------------------------------------------


def render_bas_pdf(*, company: Company, data: dict, signing_agent: StaffMember | None = None) -> bytes:
    sty = _styles()
    buf = io.BytesIO()
    doc = _doc(buf)
    flow: list[Any] = []
    flow.append(_company_header_row(company, signing_agent))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("Business Activity Statement", sty["title"]))
    flow.append(Paragraph(
        f"FY{data['fy_year']} Q{data['quarter']} · "
        f"{data['period_start'].strftime('%d %b %Y')} – "
        f"{data['period_end'].strftime('%d %b %Y')}",
        sty["subtitle"],
    ))

    if not data["gst_registered"]:
        flow.append(Paragraph(
            "<b>Not GST-registered.</b> All GST fields are zero. "
            "This report is shown so the structure is available; once the firm "
            "registers and you start recording GST splits on transactions, the "
            "numbers below will populate automatically.",
            sty["body"],
        ))
        flow.append(Spacer(1, 8))

    if data.get("uncategorised_count", 0):
        flow.append(Paragraph(
            f"<b>{data['uncategorised_count']} uncategorised transaction(s) excluded.</b> "
            "Categorise them before relying on BAS turnover boxes.",
            sty["body"],
        ))
        flow.append(Spacer(1, 8))

    rows: list[list[Any]] = [
        ["Box", "Label", "Amount"],
        ["G1", "Total sales (gross IN on business accounts)", _money(data["g1_total_sales"])],
        ["1A", "GST on sales", _money(data["one_a_gst_on_sales"])],
        ["Purch.", "Total purchase outflows (GST Exposure breaks out G10/G11/G14)", _money(data["total_purchases"])],
        ["1B", "GST on purchases", _money(data["one_b_gst_on_purchases"])],
        ["", "Net GST payable / (refund)", _money(data["net_gst_payable"])],
    ]
    tbl = Table(rows, colWidths=[15 * mm, 110 * mm, 35 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 9.5),
        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, BORDER),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, HEAD),
        ("FONTNAME", (0, -1), (-1, -1), FONT_BOLD),
        ("BACKGROUND", (0, -1), (-1, -1), ROW_ALT),
    ]))
    flow.append(tbl)

    flow.append(Spacer(1, 10))
    flow.append(Paragraph(_footer_text(), sty["footer"]))
    doc.build(flow)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 5) Trial Balance (M2.2)
# ---------------------------------------------------------------------------


def render_trial_balance_pdf(*, company: Company, data: dict, signing_agent: StaffMember | None = None) -> bytes:
    sty = _styles()
    buf = io.BytesIO()
    doc = _doc(buf, landscape_mode=True)
    flow: list[Any] = []
    flow.append(_company_header_row(company, signing_agent))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("Trial Balance", sty["title"]))
    as_of = data.get("as_of")
    flow.append(Paragraph(
        f"As of {as_of.strftime('%d %b %Y') if isinstance(as_of, date) else 'all time'}",
        sty["subtitle"],
    ))

    # Status banner
    if data["is_balanced"]:
        status = "Balanced ✓"
        status_color = ACCENT
    else:
        status = f"Out of balance — diff {_money(data['diff'])}"
        status_color = HexColor("#B45309")  # amber-700
    banner = Table(
        [[Paragraph(f"<b>{_esc(status)}</b>", sty["body"])]],
        colWidths=[270 * mm],
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ROW_ALT),
        ("BOX", (0, 0), (-1, -1), 0.5, status_color),
        ("TEXTCOLOR", (0, 0), (-1, -1), status_color),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    flow.append(banner)
    flow.append(Spacer(1, 8))

    # Per-account table
    body: list[list[Any]] = [[
        Paragraph("<b>Code</b>", sty["body"]),
        Paragraph("<b>Account / Bank</b>", sty["body"]),
        Paragraph("<b>Type</b>", sty["body"]),
        Paragraph("<b>Debit</b>", sty["body"]),
        Paragraph("<b>Credit</b>", sty["body"]),
        Paragraph("<b>Net Dr</b>", sty["body"]),
    ]]
    for r in data["rows"]:
        body.append([
            _esc(r.get("code") or "—"),
            # Raw table cells can't carry font markup; a CJK account/bank name
            # gets promoted to a Paragraph so it can use the CJK face.
            (
                Paragraph(_esc_font(r["name"]), sty["body"])
                if needs_cjk(str(r["name"]))
                else _esc(r["name"])
            ),
            _esc("Bank" if r["kind"] == "bank" else (r.get("account_type") or "—")),
            _money(r["debit_total"]) if Decimal(str(r["debit_total"])) > 0 else "",
            _money(r["credit_total"]) if Decimal(str(r["credit_total"])) > 0 else "",
            _money(r["net_debit"]),
        ])
    body.append([
        "",
        Paragraph("<b>Totals</b>", sty["body"]),
        "",
        Paragraph(f"<b>{_money(data['total_debit'])}</b>", sty["body"]),
        Paragraph(f"<b>{_money(data['total_credit'])}</b>", sty["body"]),
        Paragraph(f"<b>{_money(data['diff'])}</b>", sty["body"]),
    ])
    t = Table(body, colWidths=[20 * mm, 110 * mm, 30 * mm, 35 * mm, 35 * mm, 35 * mm])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, HEAD),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, HEAD),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BACKGROUND", (0, -1), (-1, -1), ROW_ALT),
    ]))
    flow.append(t)

    # Uncategorised note
    if (
        Decimal(str(data["uncategorised_bank_in"])) > 0
        or Decimal(str(data["uncategorised_bank_out"])) > 0
    ):
        flow.append(Spacer(1, 6))
        flow.append(Paragraph(
            f"Uncategorised bank transactions: "
            f"<b>{_money(data['uncategorised_bank_in'])}</b> in, "
            f"<b>{_money(data['uncategorised_bank_out'])}</b> out. "
            f"Categorise these on the Reconciliation page.",
            sty["small"],
        ))

    # Supplementary
    supp = data.get("supplementary") or {}
    flow.append(Spacer(1, 8))
    supp_lines: list[str] = []
    if Decimal(str(supp.get("ap_open_total") or 0)) > 0:
        supp_lines.append(
            f"Accounts Payable outstanding: <b>{_money(supp['ap_open_total'])}</b>"
        )
    if Decimal(str(supp.get("ar_open_total") or 0)) > 0:
        supp_lines.append(
            f"Accounts Receivable outstanding: <b>{_money(supp['ar_open_total'])}</b>"
        )
    if supp_lines:
        flow.append(Paragraph("<b>Supplementary (not in main totals)</b>", sty["small"]))
        for line in supp_lines:
            flow.append(Paragraph(line, sty["small"]))

    flow.append(Spacer(1, 10))
    flow.append(Paragraph(_footer_text(), sty["footer"]))
    doc.build(flow)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 6) Balance Sheet (M2.2)
# ---------------------------------------------------------------------------


def render_balance_sheet_pdf(*, company: Company, data: dict, signing_agent: StaffMember | None = None) -> bytes:
    sty = _styles()
    buf = io.BytesIO()
    doc = _doc(buf)
    flow: list[Any] = []
    flow.append(_company_header_row(company, signing_agent))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("Balance Sheet", sty["title"]))
    as_of = data.get("as_of")
    flow.append(Paragraph(
        f"As of {as_of.strftime('%d %b %Y') if isinstance(as_of, date) else ''}",
        sty["subtitle"],
    ))

    def side(label: str, groups: list[dict], total: Decimal) -> Table:
        body: list[list[Any]] = [[
            Paragraph(f"<b>{_esc(label)}</b>", sty["body"]), ""
        ]]
        for g in groups:
            body.append([
                Paragraph(f"<i>{_esc(g['label'])}</i>", sty["body"]), ""
            ])
            for l in g["lines"]:
                name = f"{l['code']} · {l['name']}" if l.get("code") else l["name"]
                body.append([
                    Paragraph(_esc_font(name), sty["body"]),
                    _money(l["balance"]),
                ])
            body.append([
                Paragraph(f"<i>Subtotal — {_esc(g['label'].lower())}</i>", sty["small"]),
                Paragraph(f"<i>{_money(g['subtotal'])}</i>", sty["small"]),
            ])
        body.append([
            Paragraph(f"<b>Total {_esc(label.lower())}</b>", sty["body"]),
            Paragraph(f"<b>{_money(total)}</b>", sty["body"]),
        ])
        t = Table(body, colWidths=[120 * mm, 40 * mm])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, HEAD),
            ("LINEABOVE", (0, -1), (-1, -1), 0.5, HEAD),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BACKGROUND", (0, -1), (-1, -1), ROW_ALT),
        ]))
        return t

    flow.append(side("Assets", data["assets"], data["total_assets"]))
    flow.append(Spacer(1, 8))
    flow.append(side("Liabilities", data["liabilities"], data["total_liabilities"]))
    flow.append(Spacer(1, 8))
    flow.append(side("Equity", data["equity"], data["total_equity"]))
    flow.append(Spacer(1, 10))

    # Balance check banner
    if data["is_balanced"]:
        status = "Assets = Liabilities + Equity ✓"
        status_color = ACCENT
    else:
        status = f"Out of balance by {_money(data['diff'])}"
        status_color = HexColor("#B45309")
    banner = Table(
        [[Paragraph(f"<b>{_esc(status)}</b>", sty["body"])]],
        colWidths=[160 * mm],
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ROW_ALT),
        ("BOX", (0, 0), (-1, -1), 0.5, status_color),
        ("TEXTCOLOR", (0, 0), (-1, -1), status_color),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    flow.append(banner)

    flow.append(Spacer(1, 10))
    flow.append(Paragraph(_footer_text(), sty["footer"]))
    doc.build(flow)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 7) GST exposure (M2.3 / M3) — BAS-shaped output backed by tax_code data
# ---------------------------------------------------------------------------


def render_gst_exposure_pdf(*, company: Company, data: dict, signing_agent: StaffMember | None = None) -> bytes:
    sty = _styles()
    buf = io.BytesIO()
    doc = _doc(buf)
    flow: list[Any] = []
    flow.append(_company_header_row(company, signing_agent))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("GST Exposure (BAS preview)", sty["title"]))

    period_start = data["period_start"]
    period_end = data["period_end"]
    fy_year = data.get("fy_year")
    quarter = data.get("quarter")
    period_label = (
        f"FY{fy_year} Q{quarter} · "
        f"{period_start.strftime('%d %b %Y')} – {period_end.strftime('%d %b %Y')}"
        if fy_year and quarter
        else f"{period_start.strftime('%d %b %Y')} – {period_end.strftime('%d %b %Y')}"
    )
    flow.append(Paragraph(period_label, sty["subtitle"]))

    def section(title: str, rows: list[tuple[str, str, str]], bold_last: bool) -> Table:
        body: list[list[Any]] = [[
            Paragraph(f"<b>{_esc(title)}</b>", sty["body"]),
            "",
            "",
        ]]
        for box, label, amount in rows:
            body.append([
                Paragraph(_esc(box), sty["body"]),
                Paragraph(_esc(label), sty["body"]),
                amount,
            ])
        t = Table(body, colWidths=[16 * mm, 110 * mm, 35 * mm])
        styles = [
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, HEAD),
            ("ALIGN", (2, 0), (2, -1), "RIGHT"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("FONTNAME", (0, 1), (0, -1), FONT_BOLD),
        ]
        if bold_last:
            styles.append(("BACKGROUND", (0, -1), (-1, -1), ROW_ALT))
            styles.append(("FONTNAME", (1, -1), (1, -1), FONT_BOLD))
        t.setStyle(TableStyle(styles))
        return t

    sales = [
        ("G1", "Total sales (gross IN)", _money(data["g1_total_sales"])),
        ("G3", "GST-free sales", _money(data["g3_gst_free_sales"])),
        ("G4", "Input-taxed sales", _money(data["g4_input_taxed_sales"])),
        ("G6", "Sales subject to GST (G1−G3−G4)", _money(data["g6_sales_subject_to_gst"])),
        ("1A", "GST collected", _money(data["one_a_gst_on_sales"])),
    ]
    purchases = [
        ("G10", "Capital purchases", _money(data["g10_capital_purchases"])),
        ("G11", "Non-capital purchases", _money(data["g11_non_capital_purchases"])),
        ("G14", "GST-free purchases", _money(data["g14_gst_free_purchases"])),
        ("1B", "GST claimable", _money(data["one_b_gst_on_purchases"])),
    ]

    flow.append(section("Sales", sales, bold_last=True))
    flow.append(Spacer(1, 8))
    flow.append(section("Purchases", purchases, bold_last=True))
    flow.append(Spacer(1, 10))

    net = Table(
        [[
            Paragraph("<b>Net GST payable / (refund)</b>", sty["body"]),
            Paragraph(f"<b>{_money(data['net_gst_payable'])}</b>", sty["body"]),
        ]],
        colWidths=[126 * mm, 35 * mm],
    )
    net.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 11),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    flow.append(net)

    if data.get("excluded_count", 0):
        flow.append(Spacer(1, 8))
        flow.append(Paragraph(
            f"{data['excluded_count']} transaction(s) excluded "
            f"(tax_code = <i>none</i>: owner draws, transfers).",
            sty["small"],
        ))

    if data.get("uncategorised_count", 0):
        flow.append(Spacer(1, 8))
        flow.append(Paragraph(
            f"{data['uncategorised_count']} uncategorised transaction(s) excluded. "
            "Categorise them before relying on GST exposure boxes.",
            sty["small"],
        ))

    flow.append(Spacer(1, 10))
    flow.append(Paragraph(_footer_text(), sty["footer"]))
    doc.build(flow)
    return buf.getvalue()
