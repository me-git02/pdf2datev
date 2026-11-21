"""Write bookings as a DATEV import file (CSV) and as an Excel overview.

The column layout matches the import mapping the tool has been used with so far
(including the historical header spelling "Waerung"). It is not the full DATEV
EXTF "Buchungsstapel" format.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from pdf2datev.config import DEFAULT_CURRENCY
from pdf2datev.models import Booking
from pdf2datev.normalize import format_amount

COLUMNS = ("Gegenkonto", "Konto", "Umsatz", "Belegdatum", "Belegfeld 1", "Waerung", "Buchungstext")
#: DATEV accepts at most 60 characters of Buchungstext.
MAX_TEXT_LENGTH = 60


def to_row(booking: Booking, currency: str = DEFAULT_CURRENCY) -> list[object]:
    return [
        booking.gegenkonto,
        booking.konto,
        format_amount(booking.amount),
        booking.date.strftime("%d%m%Y"),
        booking.invoice_no,
        currency,
        booking.text[:MAX_TEXT_LENGTH],
    ]


def write_csv(bookings: Sequence[Booking], path: Path, currency: str = DEFAULT_CURRENCY) -> None:
    # utf-8-sig: Excel and DATEV detect the encoding from the BOM.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(COLUMNS)
        writer.writerows(to_row(b, currency) for b in bookings)


def write_xlsx(bookings: Sequence[Booking], path: Path, currency: str = DEFAULT_CURRENCY) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Buchungen"
    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for booking in bookings:
        sheet.append(to_row(booking, currency))  # amounts and dates stay text, as DATEV expects
    sheet.freeze_panes = "A2"
    workbook.save(path)
