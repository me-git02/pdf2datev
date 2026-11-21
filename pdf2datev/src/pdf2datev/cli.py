"""Command line interface: ``pdf2datev [PDFs or folders] --konto 1200``."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from pdf2datev import __version__
from pdf2datev.bookings import BookingOptions
from pdf2datev.config import (
    DEFAULT_BASE_DIR,
    DEFAULT_FALLBACK_GEGENKONTO,
    DEFAULT_KONTO,
    OUTPUT_CSV_NAME,
    OUTPUT_XLSX_NAME,
)
from pdf2datev.discovery import discover_statements
from pdf2datev.export import write_csv, write_xlsx
from pdf2datev.models import Booking
from pdf2datev.pipeline import convert_statement, load_matcher

log = logging.getLogger("pdf2datev")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2datev",
        description="Convert bank statement PDFs into a DATEV booking import file.",
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="statement PDFs or folders (default: month-named PDFs/folders in the current folder)",
    )
    parser.add_argument(
        "-k", "--konto", type=int, help=f"bank account to book against (asked if omitted, default {DEFAULT_KONTO})"
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=DEFAULT_BASE_DIR,
        help="folder with the Book*.xlsx booking history (default: %(default)s)",
    )
    parser.add_argument("--no-base", action="store_true", help="do not use a booking history")
    parser.add_argument(
        "--fallback-gegenkonto",
        type=int,
        default=DEFAULT_FALLBACK_GEGENKONTO,
        help="contra account for unmatched lines (default: %(default)s)",
    )
    parser.add_argument("--year", type=int, help="year for dates printed without one (default: from the statement)")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path(), help="where to write the results")
    parser.add_argument("--no-ocr", action="store_true", help="never fall back to OCR for scanned PDFs")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every booking")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return f"{record.levelname}: {message}" if record.levelno >= logging.WARNING else message


def _configure_console(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles default to a legacy code page and choke on umlauts.
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ConsoleFormatter("%(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.propagate = False


def _ask_konto() -> int:
    while sys.stdin.isatty():
        try:
            raw = input(f"Bank account (Konto) to book against [{DEFAULT_KONTO}]: ").strip()
        except EOFError:  # e.g. stdin is the Windows NUL device, which claims to be a terminal
            print()
            break
        if not raw:
            return DEFAULT_KONTO
        if raw.isdigit():
            return int(raw)
        print("Please enter digits only, e.g. 1200.")
    log.info("Booking against Konto %d (use --konto to change).", DEFAULT_KONTO)
    return DEFAULT_KONTO


def _write_outputs(bookings: Sequence[Booking], output_dir: Path) -> bool:
    output_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, writer in ((OUTPUT_XLSX_NAME, write_xlsx), (OUTPUT_CSV_NAME, write_csv)):
        path = output_dir / name
        try:
            writer(bookings, path)
        except PermissionError:
            log.error("Cannot write %s - is it open in Excel?", path)
            ok = False
        else:
            log.info("Written: %s", path)
    return ok


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_console(args.verbose)

    workdir = Path.cwd()
    statements = discover_statements(args.inputs, workdir)
    if not statements:
        log.error("No statement PDFs found.")
        return 1

    matcher = load_matcher(None if args.no_base else args.base)
    options = BookingOptions(
        konto=args.konto if args.konto is not None else _ask_konto(),
        fallback_gegenkonto=args.fallback_gegenkonto,
        year=args.year,
    )

    bookings: list[Booking] = []
    for pdf_path in statements:
        bookings += convert_statement(
            pdf_path,
            matcher=matcher,
            options=options,
            invoice_dirs=(pdf_path.parent, workdir),
            use_ocr=not args.no_ocr,
        )

    if not bookings:
        log.warning("No bookings found.")
        return 1
    log.info("Total: %d bookings from %d statement(s).", len(bookings), len(statements))
    return 0 if _write_outputs(bookings, args.output_dir) else 1
