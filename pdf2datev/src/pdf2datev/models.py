"""Data types shared across the pipeline."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    """Booking side from the bank account's point of view."""

    DEBIT = "S"  # Soll: money leaves the account
    CREDIT = "H"  # Haben: money arrives


@dataclass(frozen=True, slots=True)
class Word:
    """A positioned piece of text on a page, from pdfplumber or OCR."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass(slots=True)
class RawTransaction:
    """A statement entry as recognised by a parser, before validation.

    Parsers build these incrementally: a line with a date and an amount starts a
    transaction, following lines without an amount extend its text.
    """

    date_raw: str | None
    text: str = ""
    amount: Decimal | None = None  # absolute value; the sign is in ``side``
    side: Side | None = None  # None with an amount: sign unreadable, decided from the text later

    @property
    def has_amount(self) -> bool:
        return self.amount is not None

    def add_text(self, extra: str) -> None:
        extra = extra.strip()
        if extra:
            self.text = f"{self.text.strip()} {extra}".strip()

    def set_amount(self, amount: Decimal, side: Side | None) -> None:
        self.amount = amount
        self.side = side


@dataclass(frozen=True, slots=True)
class Booking:
    """One row of the DATEV import file."""

    date: dt.date
    amount: Decimal  # signed: negative = Soll (debit)
    konto: int
    gegenkonto: int | str
    text: str
    invoice_no: str
    source: str
    matched: bool  # True if a history rule supplied the Gegenkonto
