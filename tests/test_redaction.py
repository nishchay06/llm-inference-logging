"""Tests for the in-house PII redactor (sdk/redaction.py)."""

from sdk.redaction import redact


def test_email():
    assert redact("reach me at a.b+x@mail.co please") == "reach me at [EMAIL] please"


def test_ssn():
    assert redact("ssn 123-45-6789") == "ssn [SSN]"


def test_ip():
    assert redact("from 192.168.0.1 today") == "from [IP] today"


def test_api_key():
    out = redact("key sk-ant-abc123DEF456ghi789 ok")
    assert "[API_KEY]" in out and "sk-ant" not in out


def test_credit_card_valid_luhn_redacted():
    out = redact("card 4111 1111 1111 1111 saved")
    assert "[CARD]" in out and "4111" not in out


def test_invalid_luhn_not_redacted_as_card():
    out = redact("num 1234 5678 9012 3456 here")
    assert "[CARD]" not in out


def test_phone():
    assert "[PHONE]" in redact("call 555-123-4567 now")
    assert "[PHONE]" in redact("intl +14155552671 ok")


def test_clean_text_unchanged():
    text = "hello world, the answer is 42 and pi is 3.14"
    assert redact(text) == text


def test_multiple_pii_in_one_string():
    out = redact("bob@example.com or 555-123-4567")
    assert "[EMAIL]" in out and "[PHONE]" in out
    assert "bob@example.com" not in out


def test_empty_and_none_safe():
    assert redact("") == ""
    assert redact(None) is None
