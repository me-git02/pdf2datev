# pdf2datev

Turns German bank statement PDFs into a booking file that can be imported into
**DATEV**, so a month of bank transactions does not have to be typed in by hand.

For every statement line it finds the date, amount and debit/credit side, picks
the contra account (*Gegenkonto*) from the company's own booking history, and
extracts invoice numbers. Lines it cannot assign go to a review account instead
of a guessed one.

```
statement PDF ──► text-layer parser ──► raw transactions ──► rules from booking history ──► DATEV CSV + Excel
                  (or OCR for scans)                          (+ invoice list of the month)
```

## Features

- **Not tied to one bank's layout**: columns are recognised from the table header
  (*Buchungstag, Valuta, Vorgang, Verwendungszweck, Betrag, Soll, Haben, Saldo*, ...)
  in any order, and all distances are relative to the text size, so fonts, sizes
  and spacing do not matter. Signed amounts, separate Soll/Haben columns, `S`/`H`
  marks, running balance columns, value dates on a second line and multi-line
  descriptions are handled. Numbers inside the description
  (*"Ursprungsbetrag 56,00 PLN"*) stay text.
- **Balance check**: opening balance + bookings must equal the closing balance,
  per statement section (one PDF may contain statements of several banks). A
  mismatch is reported, so a lost line or a misread amount cannot go unnoticed.
- **Scanned statements**: OCR (EasyOCR) at the scan's own resolution, with repair
  of typical misreadings (`~` for `-`, `6` for `€`, `O` for `0`, lost dots, a
  table border glued to a date). A lost sign is taken from the text
  (*Belastungen* / *Gutschriften*).
- **Fallbacks**: a heuristic for statements without a recognisable header, a
  line-based regex parser, and OCR for scanned PDFs.
- **Learns from history**: every `Buchungstext → Gegenkonto` pair in the DATEV
  export `Base/Book*.xlsx` becomes a rule; whole-word matching, so "Bahn" does not
  match "Bahnhofsapotheke".
- **Second source**: `<Monat>_Rechnung.pdf` supplies invoice numbers and supplier
  names for statement lines with the same amount.
- **Nothing is dropped silently**: identical transactions are all kept (and
  reported), lines without a valid date are reported instead of getting a made-up
  one, balance and total lines are filtered out.
- Exact money arithmetic with `Decimal`.

## Installation

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # ".[ocr]" to also read scanned PDFs
```

## Usage

```
pdf2datev [PDFs or folders] [--konto 1200] [--base Base] [--no-base]
          [--fallback-gegenkonto 1590] [--year 2026] [-o OUTPUT_DIR] [--no-ocr] [-v]
```

Without arguments, the current folder is searched for statements named after a
month (`Januar.pdf`, `November.pdf`) or month folders (`Januar/*.pdf`); if there
are none, all PDFs in it are used. If `--konto` is omitted, it is asked for.

Expected folder layout:

```
Base/Book1.xlsx            booking history exported from DATEV (any number of Book*.xlsx)
Januar/Kontoauszug.pdf     statements, by month file or month folder
Januar/Januar_Rechnung.pdf optional invoice list for that month
Februar.pdf
```

**Booking history**: the first sheet needs the columns `Buchungstext` and
`Gegenkonto`; the header may be preceded by DATEV's metadata line.

**Output** (`Import_DATEV.csv`, `;`-separated, UTF-8 with BOM, plus the same as
`Import_DATEV_Alle_PDF.xlsx`):

| Gegenkonto | Konto | Umsatz   | Belegdatum | Belegfeld 1 | Waerung | Buchungstext        |
|-----------:|------:|---------:|-----------:|-------------|---------|---------------------|
| 4920       | 1200  | -49,99   | 03022026   | 2026/0142   | EUR     | Telekom Deutschland |

`Umsatz` is signed from the bank account's view (negative = Soll). This is the
column layout of an existing DATEV ASCII import mapping, not the full DATEV
EXTF *Buchungsstapel* format.

## Project structure

```
src/pdf2datev/
  cli.py            command line, console output
  pipeline.py       one statement: parse → match → report
  pdf/statement.py  strategy order: table, header-less heuristic, text lines, OCR
  pdf/table.py      header-driven table parser (column roles, amounts, sides)
  pdf/layout.py     font-relative metrics, line clustering, transaction builder
  pdf/ocr.py        OCR fallback (optional easyocr)
  pdf/ocr_text.py   repair of OCR misreadings in amounts and dates
  balance.py        opening + bookings = closing check
  normalize.py      amounts, dates, signs in German notation
  textclean.py      booking text cleanup, invoice numbers, total lines
  rules.py          rules from the booking history, indexed matcher
  bookings.py       raw transactions → bookings
  invoices.py       invoice list of the month
  discovery.py      which PDFs to process
  export.py         CSV / Excel writer
```

## Limitations and ideas

- A bank with unusual header words may need them added to `_HEADER_KEYWORDS` in
  `pdf/table.py`.
- One run books everything against one `--konto`. A PDF that contains
  statements of several bank accounts should be split, or processed per file.
- Optional export in the full DATEV EXTF format.

## Reproducible demo (synthetic data only)

This repository contains a fictional statement inspired by the structure of publicly available German educational bank-statement examples (e.g. University of Cologne, https://portal.uni-koeln.de/sites/international/aaa/92/92pdf/REFUGEES_pdf/92pdf_ARS_Kontoauszug_Beispiel_2021_07.pdf). **The PDF and transactions were newly created for this project; no third-party PDF or real customer data is redistributed.**

Run from the repository root after `pip install -e .`:

```bash
pdf2datev examples/demo_kontoauszug.pdf --konto 1200 --base examples --no-ocr --output-dir demo_output
```

The command writes `demo_output/Import_DATEV.csv` and an Excel file. Compare the CSV with `examples/expected_import_datev.csv` (UTF-8 with BOM, semicolon-delimited). The six synthetic transactions are:

| Date | Amount (EUR) | Expected Gegenkonto |
|---|---:|---:|
| 02.06.2026 | -119.00 | 4930 |
| 04.06.2026 | +595.00 | 8400 |
| 08.06.2026 | -49.00 | 4964 |
| 12.06.2026 | -12.00 | 4970 |
| 17.06.2026 | -59.50 | 4930 |
| 22.06.2026 | -35.00 | 1590 (unmatched; manual review) |

Opening balance EUR 1,000.00 + transactions EUR 320.50 = closing balance EUR 1,320.50. This arithmetic is verified for the demo data; the command output above does not independently establish that the parser checked the printed balance lines.

The matching accounts are illustrative test fixtures, **not tax advice or a validated DATEV chart-of-accounts determination**. The output is for an existing DATEV ASCII import mapping, not a complete EXTF file. The prototype requires professional review before any real accounting use.
