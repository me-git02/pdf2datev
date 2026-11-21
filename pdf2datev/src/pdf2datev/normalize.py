"""Amounts, dates and debit/credit signs in German notation."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal

from pdf2datev.models import Side

#: 1.234,56 (thousands dots) or 98765,43 (none); a minus may lead or trail ("13,05-").
AMOUNT_RE = re.compile(r"(?<!\d)-?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}-?(?!\d)")
#: OCR output is sloppier: spaces as thousands separators and "." as decimal mark occur.
OCR_AMOUNT_RE = re.compile(r"(?<!\d)-?(?:\d{1,3}(?:[.\s]\d{3})+|\d+)[.,]\d{2}-?(?!\d)")
#: 05.02.2026, 5.2.26, 05/02/2026 or 05.02 (year omitted).
DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b")

_UNICODE_MINUS = "−"


def parse_amount(raw: str) -> Decimal | None:
    """Parse a German amount into its absolute value: ``'-1.234,56'`` -> ``Decimal('1234.56')``."""
    s = raw.strip().replace(_UNICODE_MINUS, "-").strip("-").replace(" ", "")
    integer, sep, frac = s.rpartition(",")
    digits = integer.replace(".", "")
    if not sep or not (digits.isascii() and digits.isdigit() and frac.isascii() and frac.isdigit()):
        return None
    return Decimal(f"{digits}.{frac}")


def parse_ocr_amount(raw: str) -> Decimal | None:
    """Like :func:`parse_amount`, but also accepts ``'13.05'`` and ``'1 234,56'`` from OCR."""
    s = raw.strip().replace(" ", "").replace(_UNICODE_MINUS, "-").strip("-").replace(";", ",")
    if "," not in s and "." in s:
        head, _, tail = s.rpartition(".")
        s = f"{head},{tail}"  # the last dot is the decimal mark
    m = re.search(r"\d+,\d{2}", s.replace(".", ""))
    return parse_amount(m.group(0)) if m else None


def format_amount(value: Decimal) -> str:
    """``Decimal('-1234.5')`` -> ``'-1234,50'`` (DATEV style, no thousands separator)."""
    return f"{value:.2f}".replace(".", ",")


def side_from_sign(text: str) -> Side:
    """A leading or trailing minus marks a debit; anything else (including ``+``) a credit."""
    t = text.strip().replace(_UNICODE_MINUS, "-")
    return Side.DEBIT if t.startswith("-") or t.endswith("-") else Side.CREDIT


def _split_date(raw: str) -> list[str]:
    return [p for p in re.split(r"[./]", raw.strip()) if p]


def explicit_year(raw: str | None) -> int | None:
    """The year written in a date string, or None if the date has no (valid) year."""
    if not raw:
        return None
    parts = _split_date(raw)
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    year = parts[2]
    if len(year) == 2:
        return 2000 + int(year)
    return int(year) if len(year) == 4 else None


def parse_date(raw: str | None, default_year: int) -> dt.date | None:
    """Parse ``DD.MM[.YY[YY]]``; returns None for anything that is not a real calendar date."""
    if not raw:
        return None
    parts = _split_date(raw)
    if len(parts) == 2:
        year: int | None = default_year
    elif len(parts) == 3:
        year = explicit_year(raw)
    else:
        return None
    if year is None:
        return None
    try:
        return dt.date(year, int(parts[1]), int(parts[0]))
    except ValueError:
        return None


def infer_year(date_raws: Iterable[str | None]) -> int | None:
    """Most common explicit year among the dates of one statement."""
    years = Counter(y for y in map(explicit_year, date_raws) if y is not None)
    return years.most_common(1)[0][0] if years else None
