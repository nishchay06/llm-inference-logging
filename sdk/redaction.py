"""Lightweight, in-house PII redaction for inference-log previews.

Regex detectors + typed-token replacement (Datadog/CloudWatch style), applied at
the source before a log is emitted. No NER/Presidio — previews are short and we
want zero heavy deps. Imperfect by design (won't catch names/addresses; number
heuristics can false-positive) — see REDACTION_DESIGN.md.
"""

import re

# Order matters: keys/email before the numeric detectors; cards before phone
# (so a 16-digit card isn't mislabeled a phone); phone last (greediest).
_API_KEY = re.compile(r"\b(?:sk|pk|rk)-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{10,}\b")  # sk-…, sk-ant-…
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PHONE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
    r"|(?<!\d)\+\d{9,15}(?!\d)"
)
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group())
        return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group()

    return _CARD_CANDIDATE.sub(repl, text)


def redact(text):
    """Return `text` with PII replaced by typed tokens. Fail-safe: on any error,
    return '[REDACTED]' rather than risk leaking the raw text."""
    if not text:
        return text
    try:
        text = _API_KEY.sub("[API_KEY]", text)
        text = _AWS_KEY.sub("[API_KEY]", text)
        text = _EMAIL.sub("[EMAIL]", text)
        text = _redact_cards(text)
        text = _SSN.sub("[SSN]", text)
        text = _IP.sub("[IP]", text)
        text = _PHONE.sub("[PHONE]", text)
        return text
    except Exception:
        return "[REDACTED]"
