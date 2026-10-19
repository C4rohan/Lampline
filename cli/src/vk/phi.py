"""Pattern scan for patient/customer data accidentally pasted into an agent.

This is a guard rail, not a compliance control: it catches the common
copy-paste mistakes (a real SSN in a test, a member ID in a prompt) before
they are pushed or published. It cannot prove text is free of PHI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Hit:
    code: str
    severity: str  # "error" | "warning"
    label: str
    excerpt: str


_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_DATE = re.compile(
    r"\b(?:(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}"
    r"|(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))\b"
)
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?[2-9]\d{2}\)?[ .-]?[2-9]\d{2}[ .-]?\d{4}(?!\d)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_RECORD_ID = re.compile(
    r"\b(?:mrn|medical record(?: number| no\.?)?|member id|patient id|policy (?:number|no\.?))"
    r"\s*[:#]?\s*[A-Z0-9-]{6,}\b",
    re.IGNORECASE,
)

# Example values that are obviously fake and fine to keep in prompts/tests.
_ALLOWED = {
    "123-45-6789",          # canonical example SSN
    "555-555-5555",
}
_FICTIONAL_PHONE = re.compile(r"\b555[ .-]?01\d{2}\b")  # 555-0100..0199 is reserved for fiction
_EXAMPLE_EMAIL = re.compile(r"@(?:example\.(?:com|org|net)|test\.invalid)$", re.IGNORECASE)


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _excerpt(text: str, start: int, end: int) -> str:
    lo, hi = max(0, start - 20), min(len(text), end + 20)
    snippet = text[lo:hi].replace("\n", " ")
    return ("…" if lo else "") + snippet + ("…" if hi < len(text) else "")


def scan(text: str) -> list[Hit]:
    if not text:
        return []
    hits: list[Hit] = []

    for m in _SSN.finditer(text):
        if m.group(0) not in _ALLOWED:
            hits.append(Hit("VK201", "error", "looks like a Social Security number", _excerpt(text, *m.span())))

    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits) and len(set(digits)) > 1:
            hits.append(Hit("VK202", "error", "looks like a payment card number", _excerpt(text, *m.span())))

    for m in _RECORD_ID.finditer(text):
        hits.append(Hit("VK206", "error", "looks like a medical record / member ID", _excerpt(text, *m.span())))

    for m in _DATE.finditer(text):
        hits.append(Hit("VK203", "warning", "full date (could be a date of birth)", _excerpt(text, *m.span())))

    for m in _PHONE.finditer(text):
        value = m.group(0)
        if value in _ALLOWED or _FICTIONAL_PHONE.search(value):
            continue
        hits.append(Hit("VK204", "warning", "phone number", _excerpt(text, *m.span())))

    for m in _EMAIL.finditer(text):
        if _EXAMPLE_EMAIL.search(m.group(0)):
            continue
        hits.append(Hit("VK205", "warning", "email address", _excerpt(text, *m.span())))

    return hits
