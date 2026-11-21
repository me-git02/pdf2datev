"""Repair typical OCR misreadings in the amounts and dates of bank statements.

Typical misreadings: "-84,73€" read as "~84,736" (minus as tilde, € as 6),
"45.000,00" as "45.OOO,OO", "02.02.2026" as "0202.2026", a table border in
front of a date ("J03.02.2026"). A token is only changed if, after the repair,
it has the exact shape of an amount or a calendar date, so ordinary words are
never touched.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import Enum

_MINUS_LIKE = "~-–—−"
# Letters OCR confuses with digits. Only applied inside amount- or date-shaped tokens.
_DIGIT_LOOKALIKES = str.maketrans(
    {"O": "0", "o": "0", "D": "0", "Q": "0", "p": "0", "l": "1", "I": "1", "i": "1", "|": "1", "Z": "2",
     "z": "2", "S": "5", "s": "5", "G": "6", "b": "6", "B": "8", "g": "9", "q": "9"}
)  # fmt: skip
# Thousands separators (dot, space, or a comma misread for a dot), decimal comma or
# dot, two decimals, then possibly one misread currency sign or border character,
# and a trailing minus.
_AMOUNT = re.compile(r"(?P<int>\d{1,3}(?:[.,\s]\d{3})+|\d+)[.,](?P<dec>\d{2})[^.,\-]?(?P<trail>-?)")
# The same with the decimal comma lost ("-1.80000€" for "-1.800,00€"). Only tried for
# tokens with a sign or a currency mark, so "0.23727" (an exchange rate) is left alone.
_AMOUNT_NO_DECIMAL_MARK = re.compile(
    r"(?:(?P<int>\d{1,3}(?:[.,\s]\d{3})*)[.,\s])?(?P<last>\d{3})(?P<dec>\d{2})[^.,\-\d]?(?P<trail>-?)"
)
_DATE = re.compile(r"(?P<d>\d{2})[.,]?(?P<m>\d{2})[.,]?(?P<y>\d{4})")
#: A date glued to the text around it ("Jo1.02.2026lGutschrift").
GLUED_DATE = re.compile(r"[0-9OoDQpgIl|]{2}[.,][0-9OoDQ]{2}[.,]?[0-9OoDQ]{4}")
_YEARS = range(1990, 2100)


class Kind(str, Enum):
    AMOUNT = "amount"
    DATE = "date"


def repair_amount(token: str) -> str | None:
    """``'~84,736'`` -> ``'-84,73'``; None if the token is not an amount."""
    text = token.strip()
    sign = ""
    if text[:1] and text[0] in "+" + _MINUS_LIKE:
        sign = "+" if text[0] == "+" else "-"
        text = text[1:].strip()
    candidate = text.translate(_DIGIT_LOOKALIKES)
    if sum(c.isdigit() for c in candidate) < 3:
        return None
    trail = "-" if candidate.endswith("-") else ""
    if (m := _AMOUNT.fullmatch(candidate)) is not None:
        integer = m["int"]
    elif (sign or not candidate[-1:].isdigit()) and (m := _AMOUNT_NO_DECIMAL_MARK.fullmatch(candidate)):
        integer = f"{m['int']}.{m['last']}" if m["int"] else m["last"]
    else:
        return None
    integer = re.sub(r"[,\s]", ".", integer)
    return f"{sign}{integer},{m['dec']}{trail}"


def repair_date(token: str) -> str | None:
    """``'J03.02.2026'`` / ``'0202.2026'`` -> ``'03.02.2026'`` / ``'02.02.2026'``; else None."""
    text = token.strip().rstrip(".,")
    if "." not in text and "," not in text:  # plain numbers are account numbers, not dates
        return None
    for skip in (0, 1, 2):  # up to two noise characters in front (table border)
        m = _DATE.fullmatch(text[skip:].translate(_DIGIT_LOOKALIKES))
        if m is None:
            continue
        day, month, year = int(m["d"]), int(m["m"]), int(m["y"])
        if year not in _YEARS:
            continue
        try:
            dt.date(year, month, day)
        except ValueError:
            continue
        return f"{m['d']}.{m['m']}.{m['y']}"
    return None


def repair_token(token: str) -> tuple[str, Kind | None]:
    """The repaired token and what it is, or the token unchanged."""
    if (date := repair_date(token)) is not None:
        return date, Kind.DATE
    if (amount := repair_amount(token)) is not None:
        return amount, Kind.AMOUNT
    return token, None
