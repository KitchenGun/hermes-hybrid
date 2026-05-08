"""Tests for src.memory.ingestion.pii (P0-A)."""
from __future__ import annotations

import pytest

from src.memory.ingestion.pii import PIIScanner


@pytest.fixture
def scanner() -> PIIScanner:
    return PIIScanner()


def _categories(scanner: PIIScanner, text: str) -> list[str]:
    return [f.category for f in scanner.scan(text)]


def test_email_detected(scanner: PIIScanner) -> None:
    assert "email" in _categories(scanner, "ping me at alice@example.com please")


def test_phone_detected(scanner: PIIScanner) -> None:
    cats = _categories(scanner, "call me at 010-1234-5678 tomorrow")
    assert "phone" in cats


def test_credit_card_detected(scanner: PIIScanner) -> None:
    cats = _categories(scanner, "card: 4111 1111 1111 1111 expires 12/29")
    assert "card" in cats


def test_ssn_detected(scanner: PIIScanner) -> None:
    cats = _categories(scanner, "SSN 123-45-6789 on file")
    assert "ssn" in cats


@pytest.mark.parametrize(
    "key",
    [
        "sk-ant-abc1234567890ABCDEFGHIJ",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_abcdefghijklmnopqrstuvwx",
        "xoxb-1234567890-abcdefghij",
    ],
)
def test_api_key_shapes_detected(scanner: PIIScanner, key: str) -> None:
    cats = _categories(scanner, f"the token is {key} now")
    assert "api_key" in cats, f"missed {key!r}"


def test_pem_block_detected(scanner: PIIScanner) -> None:
    blob = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIB...\n-----END RSA PRIVATE KEY-----"
    assert "pem" in _categories(scanner, blob)


def test_keyword_password_detected(scanner: PIIScanner) -> None:
    cats = _categories(scanner, "the password is hunter2 (don't tell)")
    assert "keyword" in cats


def test_keyword_must_be_word_boundary(scanner: PIIScanner) -> None:
    # 'crosspasswording' must not match 'password' as a keyword.
    findings = scanner.scan("crosspasswording is a fake word")
    assert findings == []


def test_clean_text_yields_no_findings(scanner: PIIScanner) -> None:
    assert scanner.scan("the cat sat on the mat") == []


def test_match_excerpt_is_masked(scanner: PIIScanner) -> None:
    findings = scanner.scan("contact: alice@example.com")
    email = next(f for f in findings if f.category == "email")
    # The excerpt must NOT contain the full literal address.
    assert "alice@example.com" not in email.match_excerpt
    assert "…" in email.match_excerpt or len(email.match_excerpt) < len(
        "alice@example.com"
    )


def test_has_pii_short_circuits(scanner: PIIScanner) -> None:
    assert scanner.has_pii("alice@example.com") is True
    assert scanner.has_pii("nothing to see here") is False


def test_extra_keywords_extend_default_list() -> None:
    scanner = PIIScanner(extra_keywords=("internal-only-marker",))
    cats = _categories(scanner, "this contains internal-only-marker text")
    assert "keyword" in cats
