"""Safety-net normalisation applied to model output before schema validation.

The prompt already asks the model to return western digits, dot decimals and ISO dates.
These helpers catch the cases where it doesn't (Arabic-Indic digits, Arabic separators,
European number formats, a date left as printed).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# U+0660..U+0669 Arabic-Indic, U+06F0..U+06F9 Extended (Persian/Urdu) Arabic-Indic.
_DIGITS = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGITS |= {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_ARABIC_DECIMAL = "٫"  # ٫
_ARABIC_THOUSANDS = "٬"  # ٬
# Directional marks that PDF text / OCR often leaves around numbers.
_INVISIBLES = dict.fromkeys(map(ord, "‎‏؜‪‫‬‭‮⁦⁧⁨⁩"))


def normalize_digits(text: str) -> str:
    """Arabic-Indic digits -> western; Arabic decimal sign -> '.'; Arabic thousands sign removed."""
    return (
        text.translate(_DIGITS)
        .translate(_INVISIBLES)
        .replace(_ARABIC_DECIMAL, ".")
        .replace(_ARABIC_THOUSANDS, "")
    )


_NUM_CHARS = re.compile(r"[^0-9.,\-]")


def normalize_number(value: Any) -> str | None:
    """Return a plain decimal string ("1234.56") or None.

    Handles "1,234.56", "1.234,56", "1 234,56", "١٬٢٣٤٫٥٦", currency symbols and "%".
    Rules when the separator is ambiguous:
      - both '.' and ',' present: the right-most one is the decimal separator;
      - only ',' present: decimal if it appears once with 1-2 digits after it, else thousands;
      - only '.' present: decimal if it appears once, else thousands.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = repr(value) if isinstance(value, float) else str(value)
    s = normalize_digits(str(value)).strip()
    if not s:
        return None
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = _NUM_CHARS.sub("", s).lstrip("-")
    if not re.search(r"\d", s):
        return None
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        grp = "," if dec == "." else "."
        s = s.replace(grp, "").replace(dec, ".")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", ".") if len(parts) == 2 and 1 <= len(parts[1]) <= 2 else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    return f"-{s}" if neg else s


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d-%m-%Y",
    "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y",
)


def normalize_date(value: Any) -> str | None:
    """Return an ISO date string, or the cleaned input unchanged if no known format matches
    (so schema validation fails loudly instead of us guessing). Slash dates other than
    YYYY/MM/DD are deliberately not parsed: DD/MM vs MM/DD is ambiguous."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    s = re.sub(r"\s+", " ", normalize_digits(str(value))).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s
