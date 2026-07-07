"""CJK-capable font selection for the ReportLab renderers.

The document style is Times (PDF base-14), which has no CJK glyphs — a client
called 张伟 used to render as placeholder boxes. Noto Sans SC (OFL-1.1, bundled
at app/assets/fonts/) is registered lazily and substituted only for strings
that actually contain non-Latin characters, so English documents keep the
exact Times look and Latin-only PDFs stay small (the font subsets on embed).
"""

from __future__ import annotations

from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_CJK = "NotoSansSC"

# Resolves both from source (backend/app/assets/fonts) and from the frozen
# bundle (PyInstaller sets __file__ under _MEIPASS; the spec ships the assets
# dir to the same app/assets relative location).
_FONT_FILE = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansSC-Regular.ttf"

_state: dict[str, bool] = {}


def _cjk_available() -> bool:
    if "available" not in _state:
        try:
            pdfmetrics.registerFont(TTFont(FONT_CJK, str(_FONT_FILE)))
            _state["available"] = True
        except Exception:
            # Missing/corrupt font file: degrade to the old behaviour
            # (placeholder glyphs) rather than failing the whole render.
            _state["available"] = False
    return _state["available"]


def needs_cjk(text: str | None) -> bool:
    """True when the string has characters outside the Latin ranges Times
    covers. Cheap heuristic: anything above U+2E7F (CJK radicals onward,
    incl. fullwidth forms, kana, hangul)."""
    return bool(text) and any(ord(ch) > 0x2E7F for ch in text)


def font_for(text: str | None, base_font: str) -> str:
    """The font to draw `text` with: `base_font` for Latin text, the bundled
    CJK face when the string needs it (Noto Sans SC covers Latin too, so a
    mixed string renders consistently in one face)."""
    if needs_cjk(text) and _cjk_available():
        return FONT_CJK
    return base_font
