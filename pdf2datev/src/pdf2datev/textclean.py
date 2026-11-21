"""Booking text cleanup, invoice number extraction and summary-line detection."""

from __future__ import annotations

import re

from pdf2datev.models import Side

# The payment purpose follows these technical markers. Checked in this order;
# the last two are frequent OCR misreadings of "Verwendungszweck".
_PURPOSE_MARKERS = ("verwendungszweck", "referenz", "end-to-end-ref", "verwendungsywek", "verwendungswek")

_IBAN_RE = re.compile(r"\b[A-Z]{2}\s*\d[\d\s]{14,}\b", re.IGNORECASE)
_BIC_COUNTRIES = "AT|BE|BG|CH|CY|CZ|DE|DK|EE|ES|FI|FR|GB|GR|HR|HU|IE|IS|IT|LI|LT|LU|LV|MT|NL|NO|PL|PT|RO|SE|SI|SK"
#: BIC/SWIFT codes such as COBADEFFXXX or MUSTDEFF: bank, country, location, optional branch.
_BIC_RE = re.compile(rf"\b[A-Z]{{4}}(?:{_BIC_COUNTRIES})[A-Z0-9]{{2}}(?:[A-Z0-9]{{3}})?\b")
_ID_WITH_VALUE_RE = re.compile(r"(?i)\b(?:gläubiger-?id|mandat-?id|mandant-?id)\s*[:\s]*[\w\-]+")
_TECHNICAL_WORDS_RE = re.compile(
    r"\b(?:verwendungszweck|referenz|end-to-end-ref|kundenreferenz|gläubiger-?id|mandat-?id|mandant-?id|iban|bic)\b",
    re.IGNORECASE,
)
_LETTER_RUN_RE = re.compile(r"[^\W\d]+")

# Words that describe the kind of payment, not who was paid; left out of the hint.
_HINT_STOPWORDS = frozenset(
    {
        "sepa", "lastschrifteinzug", "lastschrift", "dauerauftrag", "überweisung", "ueberweisung",
        "gutschrift", "kartenzahlung", "einsatz", "debit", "bezahl", "girocard", "online", "ec",
        "gutschriften", "belastungen", "wertstellung", "datum", "uhr",
        "verwendungszweck", "kundenreferenz", "rechnung", "rechnungsnr", "rechnung-nr", "glaeubiger-id",
        "gläubiger-id", "mandat-id", "mandat", "iban", "bic", "ag", "gmbh", "kg", "mbh", "und", "de", "eu",
    }
)  # fmt: skip

# The captured number must contain a digit, so "Rechnung vom ..." does not yield "vom".
_NUMBER = r"((?=[\w\-/]*\d)[\w\-/]{3,45})"
_SEP = r"\s*[:#.\-–]?\s*"
INVOICE_NO_PATTERNS = (
    re.compile(rf"(?i)\brechnung\s*nr\.?{_SEP}([0-9]+(?:\s*/\s*[0-9]+){{1,4}})"),
    re.compile(rf"(?i)\binvoice\s*no\.?{_SEP}([0-9]+(?:\s*/\s*[0-9]+){{1,4}})"),
    re.compile(
        r"(?i)\b(?:rechnung(?:s)?\s*(?:nr|nummer|no)?\.?|invoice(?:\s*nr|\s*no|\s*number)?"
        rf"|beleg(?:nr|nummer)?){_SEP}{_NUMBER}"
    ),
    re.compile(rf"(?i)\b(?:re[-\s]?nr|inv(?:oice)?[-\s]?nr|rg\.?\s*nr\.?){_SEP}{_NUMBER}"),
    re.compile(rf"(?i)\b(?:rechnung|invoice|beleg)\s*#\s*{_NUMBER}"),
    re.compile(rf"(?i)(?:rechnungs|beleg)[^\n]{{0,50}}?nr\.?{_SEP}{_NUMBER}"),
)

_SUMMARY_PHRASES = (
    "summe soll",
    "summe haben",
    "endsaldo",
    "anfangssaldo",
    "neuer saldo",
    "alter saldo",
    "saldovortrag",
    "kontostand",
    "ausgehende transaktionen",
    "eingehende transaktionen",
    "einkommende transaktionen",
    "übertrag",
    "ubertrag",
)


def purpose_part(text: str) -> str:
    """The text after the first technical marker ("Verwendungszweck", "Referenz", ...)."""
    s = text.strip()
    low = s.lower()
    for marker in _PURPOSE_MARKERS:
        idx = low.find(marker)
        if idx != -1:
            after = s[idx + len(marker) :].strip(" :/\t-\n")
            return after if len(after) > 1 else s
    return s


def clean_text(text: str) -> str:
    """Remove IBANs, BICs, mandate/creditor IDs, technical words and numbers."""
    s = text.strip()
    if not s:
        return ""
    s = _IBAN_RE.sub(" ", s)
    s = _BIC_RE.sub(" ", s)
    s = _ID_WITH_VALUE_RE.sub(" ", s)  # before the label words are removed below
    s = _TECHNICAL_WORDS_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    words = [w for w in _LETTER_RUN_RE.findall(s) if len(w) >= 2 and any(c.isalpha() for c in w)]
    return " ".join(words) if words else s


def make_hint(text: str, max_words: int = 5) -> str:
    """Short Buchungstext for lines that no rule matched: the first meaningful words."""
    text = text.strip()
    words = [
        tok
        for tok in re.findall(r"\w+", text)
        if (len(tok) >= 3 or _is_short_name(tok)) and not tok.isdigit() and tok.lower() not in _HINT_STOPWORDS
    ]
    return " ".join(words[:max_words]) if words else text[:80]


def _is_short_name(token: str) -> bool:
    """Two-letter names in capitals such as "DM" are kept; "Nr", "zu" are not."""
    return len(token) == 2 and token.isalpha() and token.isupper()


def extract_invoice_number(text: str) -> str:
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", text)
    for pattern in INVOICE_NO_PATTERNS:
        m = pattern.search(flat)
        if not m:
            continue
        number = m.group(1).strip(" .,:;#-_/–")
        number = re.sub(r"\s*/\s*", "/", number)
        number = re.sub(r"\s+", "", number)
        if number and not re.fullmatch(r"\d{1,2}", number):
            return number
    return ""


_CREDIT_WORDS = ("gutschrift", "zahlungseingang", "erstattung")
_DEBIT_WORDS = ("belastung", "lastschrift", "kartenzahlung", "mastercard", "abbuchung", "auszahlung", "entgelt")


def side_from_text(text: str) -> Side | None:
    """Debit or credit from words such as N26's "Belastungen" / "Gutschriften"."""
    low = text.lower()
    if any(word in low for word in _CREDIT_WORDS):
        return Side.CREDIT
    if any(word in low for word in _DEBIT_WORDS):
        return Side.DEBIT
    return None


def looks_like_summary(text: str) -> bool:
    """Totals and balance lines of a statement, which are not transactions."""
    t = text.strip().lower()
    if not t:
        return False
    if any(phrase in t for phrase in _SUMMARY_PHRASES):
        return True
    if "anzahl" in t and "summe" in t:
        return True
    return t.startswith(("summe ", "saldo "))
