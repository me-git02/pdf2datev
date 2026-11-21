"""Turn raw statement transactions into DATEV bookings."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from pdf2datev.balance import BalanceCheck, BalanceTracker
from pdf2datev.config import DEFAULT_FALLBACK_GEGENKONTO
from pdf2datev.invoices import InvoiceIndex
from pdf2datev.models import Booking, RawTransaction, Side
from pdf2datev.normalize import infer_year, parse_date
from pdf2datev.rules import SALARY_KEYWORDS, Rule, RuleMatcher
from pdf2datev.textclean import (
    clean_text,
    extract_invoice_number,
    looks_like_summary,
    make_hint,
    purpose_part,
    side_from_text,
)


@dataclass(frozen=True, slots=True)
class BookingOptions:
    konto: int
    fallback_gegenkonto: int = DEFAULT_FALLBACK_GEGENKONTO
    #: Year for dates printed without one; inferred from the statement if None.
    year: int | None = None


@dataclass
class BookingResult:
    bookings: list[Booking] = field(default_factory=list)
    #: Transactions with an amount but no usable date. Reported, never guessed.
    without_date: list[RawTransaction] = field(default_factory=list)
    #: Bookings that occur more than once. They are kept (they can be real), but reported.
    repeated: list[tuple[Booking, int]] = field(default_factory=list)
    #: Bookings whose sign was unreadable and not clear from the text; booked as Haben.
    side_guessed: list[Booking] = field(default_factory=list)
    #: Opening balance + bookings = closing balance, per statement section.
    balance_checks: list[BalanceCheck] = field(default_factory=list)
    #: Year used for dates without a year, and whether it was only a guess (today's year).
    year: int = 0
    year_guessed: bool = False


def _find_rule(matcher: RuleMatcher, raw_text: str, purpose: str, full: str) -> Rule | None:
    rule = matcher.match(purpose)
    if rule is None and full != purpose:
        rule = matcher.match(full)  # the counterparty name is usually before "Verwendungszweck"
    if rule is None:
        low = raw_text.lower()
        for keyword in SALARY_KEYWORDS:
            if keyword in low and (rule := matcher.match(keyword)) is not None:
                break
    return rule


def build_bookings(
    transactions: Sequence[RawTransaction],
    *,
    matcher: RuleMatcher,
    options: BookingOptions,
    source: str,
    invoices: InvoiceIndex | None = None,
) -> BookingResult:
    invoices = invoices or {}
    year = options.year or infer_year(t.date_raw for t in transactions)
    result = BookingResult(
        year=year or dt.date.today().year,
        year_guessed=year is None and any(t.date_raw for t in transactions),
    )

    balances = BalanceTracker()
    for tx in transactions:
        if tx.amount is None:
            continue
        purpose = clean_text(purpose_part(tx.text))
        if looks_like_summary(purpose or tx.text):
            balances.add_balance_line(tx.text, tx.amount, tx.side)
            continue
        if tx.amount == 0:
            continue
        booking_date = parse_date(tx.date_raw, result.year)
        if booking_date is None:
            result.without_date.append(tx)
            continue
        # A lost sign (OCR) is taken from the text, e.g. N26's "Belastungen" / "Gutschriften".
        side = tx.side or side_from_text(tx.text)

        full = clean_text(tx.text)
        rule = _find_rule(matcher, tx.text, purpose, full)
        invoice_no = extract_invoice_number(tx.text)
        if (invoice := invoices.get(tx.amount)) is not None:
            invoice_no = invoice_no or invoice.invoice_no or extract_invoice_number(invoice.text)
            if rule is None and invoice.text:
                rule = matcher.match(invoice.text)

        booking = Booking(
            date=booking_date,
            amount=-tx.amount if side is Side.DEBIT else tx.amount,
            konto=options.konto,
            gegenkonto=rule.gegenkonto if rule else options.fallback_gegenkonto,
            text=rule.text if rule else make_hint(full or tx.text),
            invoice_no=invoice_no,
            source=source,
            matched=rule is not None,
        )
        result.bookings.append(booking)
        balances.add_booking(booking.amount)
        if side is None:
            result.side_guessed.append(booking)

    result.repeated = _repeated(result.bookings)
    result.balance_checks = balances.checks
    return result


def _repeated(bookings: Sequence[Booking]) -> list[tuple[Booking, int]]:
    counts: Counter[tuple[dt.date, object, str]] = Counter()
    first: dict[tuple[dt.date, object, str], Booking] = {}
    for booking in bookings:
        key = (booking.date, booking.amount, booking.text)
        counts[key] += 1
        first.setdefault(key, booking)
    return [(first[key], n) for key, n in counts.items() if n > 1]
