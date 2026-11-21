"""User-facing defaults. Parser tuning constants live next to the parsers that use them."""

from pathlib import Path

#: Bank account the statement lines are booked against (SKR03 1200 = Bank).
DEFAULT_KONTO = 1200
#: Contra account for lines no rule matched, so they can be reviewed manually.
DEFAULT_FALLBACK_GEGENKONTO = 1590
DEFAULT_CURRENCY = "EUR"

#: Folder with the DATEV booking history (Book*.xlsx) used to learn rules.
DEFAULT_BASE_DIR = Path("Base")

OUTPUT_CSV_NAME = "Import_DATEV.csv"
OUTPUT_XLSX_NAME = "Import_DATEV_Alle_PDF.xlsx"
