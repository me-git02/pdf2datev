"""Optional second source: the month's invoice PDF (e.g. ``Januar_Rechnung.pdf``).

Statement lines often lack the supplier name or invoice number. If an invoice in
that PDF has the same amount, its text and number are used instead.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pdfplumber
from pdfplumber.utils.exceptions import MalformedPDFException, PdfminerException

from pdf2datev.discovery import INVOICE_SUFFIX, month_of
from pdf2datev.normalize import AMOUNT_RE, parse_amount
from pdf2datev.textclean import clean_text, extract_invoice_number

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InvoiceInfo:
    text: str
    invoice_no: str


InvoiceIndex = dict[Decimal, InvoiceInfo]


def find_invoice_pdf(statement: Path, search_dirs: Iterable[Path]) -> Path | None:
    month = month_of(statement)
    if month is None:
        return None
    for folder in search_dirs:
        candidate = folder / f"{month}{INVOICE_SUFFIX.title()}.pdf"
        if candidate.is_file():
            return candidate
    return None


def load_invoice_index(path: Path) -> InvoiceIndex:
    """Map every amount on an invoice page to that page's text and invoice number."""
    index: InvoiceIndex = {}
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                info = InvoiceInfo(clean_text(text), extract_invoice_number(text))
                for m in AMOUNT_RE.finditer(text):
                    amount = parse_amount(m.group(0))
                    if amount is not None:
                        index.setdefault(amount, info)
    except (OSError, PdfminerException, MalformedPDFException) as exc:
        log.warning("Could not read invoice PDF %s: %s", path.name, exc)
    return index
