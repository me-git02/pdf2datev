"""Balance check: opening balance + bookings must equal the closing balance.

This is how an accountant reconciles a statement, and it catches what no parser
can notice by itself: a lost line, a misread digit, a wrong sign. One PDF may
hold several statements (e.g. two banks), so the check runs per section: a
closing balance line ends a section, an opening balance line starts one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pdf2datev.models import Side

_OPENING = ("alter kontostand", "alter saldo", "anfangssaldo", "saldovortrag")
_CLOSING = ("neuer kontostand", "neuer saldo", "endsaldo")


@dataclass(frozen=True, slots=True)
class BalanceCheck:
    opening: Decimal | None  # None: the statement showed no opening balance
    closing: Decimal
    bookings_total: Decimal
    bookings: int

    @property
    def ok(self) -> bool | None:
        """True/False, or None if there is nothing to compare with."""
        return None if self.opening is None else self.opening + self.bookings_total == self.closing

    @property
    def difference(self) -> Decimal:
        return self.closing - (self.opening or Decimal(0)) - self.bookings_total


def _kind(text_lower: str) -> str | None:
    if any(phrase in text_lower for phrase in _OPENING):
        return "opening"
    if any(phrase in text_lower for phrase in _CLOSING):
        return "closing"
    if "kontostand" in text_lower or "saldo" in text_lower:
        return "either"  # "Kontostand am 31.01.": opening before the bookings, closing after them
    return None  # totals such as "Summe Soll" or "Ausgehende Transaktionen"


class BalanceTracker:
    def __init__(self) -> None:
        self.checks: list[BalanceCheck] = []
        self._opening: Decimal | None = None
        self._total = Decimal(0)
        self._count = 0

    def add_booking(self, signed_amount: Decimal) -> None:
        self._total += signed_amount
        self._count += 1

    def add_balance_line(self, text: str, amount: Decimal, side: Side | None) -> None:
        kind = _kind(text.lower())
        if kind is None:
            return
        value = -amount if side is Side.DEBIT else amount
        if kind == "opening" or (kind == "either" and self._count == 0 and self._opening is None):
            self._opening = value
            return
        self.checks.append(BalanceCheck(self._opening, value, self._total, self._count))
        self._opening, self._total, self._count = None, Decimal(0), 0
