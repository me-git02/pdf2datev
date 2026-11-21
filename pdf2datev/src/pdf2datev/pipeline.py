"""One statement PDF in, DATEV bookings out, with a readable report in the log."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from pdf2datev.balance import BalanceCheck
from pdf2datev.bookings import BookingOptions, build_bookings
from pdf2datev.invoices import find_invoice_pdf, load_invoice_index
from pdf2datev.models import Booking
from pdf2datev.normalize import format_amount
from pdf2datev.pdf.statement import parse_statement
from pdf2datev.rules import RuleMatcher, find_history_files, load_rules

log = logging.getLogger(__name__)


def load_matcher(base_dir: Path | None) -> RuleMatcher:
    """Rules from ``base_dir/Book*.xlsx``; an empty matcher if there is no history."""
    if base_dir is None:
        return RuleMatcher()
    if not base_dir.is_dir():
        log.warning("Booking history folder not found: %s. Continuing without rules.", base_dir)
        return RuleMatcher()
    files = find_history_files(base_dir)
    if not files:
        log.warning("No Book*.xlsx in %s. Continuing without rules.", base_dir)
        return RuleMatcher()
    matcher = RuleMatcher(load_rules(files))
    log.info("Loaded %d rules from %d history file(s).", len(matcher), len(files))
    return matcher


def convert_statement(
    pdf_path: Path,
    *,
    matcher: RuleMatcher,
    options: BookingOptions,
    invoice_dirs: Sequence[Path],
    use_ocr: bool = True,
) -> list[Booking]:
    log.info("--- %s ---", pdf_path.name)
    transactions = parse_statement(pdf_path, use_ocr=use_ocr)

    invoices = {}
    if (invoice_pdf := find_invoice_pdf(pdf_path, invoice_dirs)) is not None:
        invoices = load_invoice_index(invoice_pdf)
        if invoices:
            log.info("Using invoice list %s.", invoice_pdf.name)

    result = build_bookings(transactions, matcher=matcher, options=options, source=pdf_path.name, invoices=invoices)

    if result.year_guessed:
        log.warning("No year found in the statement; assuming %d (use --year to set it).", result.year)
    for tx in result.without_date:
        log.warning("Skipped, no valid date: %s %s '%s'", tx.side.value if tx.side else "?", tx.amount, tx.text)
    for booking, count in result.repeated:
        log.warning("Appears %dx, kept all - please check: %s", count, _describe(booking))
    for booking in result.side_guessed:
        log.warning("Sign unreadable, booked as Haben - please check: %s", _describe(booking))
    for check in result.balance_checks:
        _report_balance(check)
    for booking in result.bookings:
        log.debug(
            "%s %12s  %s -> %s  '%s'",
            booking.date.strftime("%d.%m.%Y"),
            format_amount(booking.amount),
            booking.konto,
            booking.gegenkonto,
            booking.text,
        )

    matched = sum(b.matched for b in result.bookings)
    log.info(
        "%d bookings: %d matched by rules, %d to %s for review.",
        len(result.bookings),
        matched,
        len(result.bookings) - matched,
        options.fallback_gegenkonto,
    )
    return result.bookings


def _describe(booking: Booking) -> str:
    return f"{booking.date:%d.%m.%Y} {format_amount(booking.amount)} '{booking.text}'"


def _report_balance(check: BalanceCheck) -> None:
    if check.opening is None:
        log.info("Closing balance %s found without an opening balance; not checked.", format_amount(check.closing))
    elif check.ok:
        log.info(
            "Balance check OK: opening %s, bookings %s (%d), closing %s.",
            format_amount(check.opening),
            format_amount(check.bookings_total),
            check.bookings,
            format_amount(check.closing),
        )
    else:
        log.warning(
            "Balance check FAILED: %s + bookings %s = %s, but the statement shows %s (difference %s). "
            "An amount is missing or misread - please compare this statement with the bookings.",
            format_amount(check.opening),
            format_amount(check.bookings_total),
            format_amount(check.opening + check.bookings_total),
            format_amount(check.closing),
            format_amount(check.difference),
        )
