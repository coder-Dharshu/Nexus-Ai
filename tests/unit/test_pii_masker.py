"""
Nexus AI — PII masker unit tests.
"""
from __future__ import annotations

import pytest
from src.security.pii_masker import PIIMasker


@pytest.fixture
def masker():
    return PIIMasker()


class TestEmailMasking:

    def test_simple_email(self, masker):
        r = masker.mask("My email is test@example.com")
        assert "test@example.com" not in r.masked
        assert r.was_modified

    def test_email_in_sentence(self, masker):
        r = masker.mask("Contact john.doe+filter@company.co.uk for help.")
        assert "john.doe+filter@company.co.uk" not in r.masked

    def test_multiple_emails(self, masker):
        r = masker.mask("From: a@b.com, To: c@d.com")
        assert "a@b.com" not in r.masked
        assert "c@d.com" not in r.masked


class TestPhoneNumbers:

    def test_indian_phone_with_prefix(self, masker):
        r = masker.mask("Call +91 98765 43210 now")
        assert "98765 43210" not in r.masked

    def test_indian_phone_without_prefix(self, masker):
        r = masker.mask("My number: 9876543210")
        assert "9876543210" not in r.masked

    def test_indian_phone_with_dashes(self, masker):
        r = masker.mask("Phone: 98765-43210")
        assert "98765-43210" not in r.masked


class TestAadhaarAndPAN:

    def test_aadhaar_spaced(self, masker):
        r = masker.mask("Aadhaar: 1234 5678 9012")
        assert "1234 5678 9012" not in r.masked
        assert "[AADHAAR]" in r.masked

    def test_aadhaar_no_spaces(self, masker):
        r = masker.mask("Aadhaar: 123456789012")
        assert "123456789012" not in r.masked

    def test_pan_card(self, masker):
        r = masker.mask("PAN: ABCDE1234F is required")
        assert "ABCDE1234F" not in r.masked
        assert "[PAN]" in r.masked

    def test_pan_lowercase_not_matched(self, masker):
        # PAN is always uppercase — lowercase should not be falsely detected
        r = masker.mask("abcde1234f is not a PAN")
        assert r.masked == "abcde1234f is not a PAN"


class TestAPIKeysAndTokens:

    def test_api_key_in_query_string(self, masker):
        r = masker.mask("api_key=sk-1234567890abcdef1234567890abcdef1234")
        assert "sk-1234567890abcdef" not in r.masked

    def test_bearer_token(self, masker):
        r = masker.mask("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.xyz")
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in r.masked

    def test_aws_key(self, masker):
        r = masker.mask("AWS key: AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in r.masked
        assert "[AWS_KEY]" in r.masked

    def test_secret_key_pattern(self, masker):
        r = masker.mask('secret_key = "abcdef1234567890abcdef1234567890"')
        assert "abcdef1234567890abcdef1234567890" not in r.masked


class TestCleanTextPreservation:

    def test_gold_price_unchanged(self, masker):
        text = "Gold price: ₹71,211 per 10 grams. Confidence: 96%."
        r = masker.mask(text)
        assert r.masked == text
        assert not r.was_modified

    def test_flight_info_unchanged(self, masker):
        text = "IndiGo 6E-204 departs BLR 06:05, arrives DEL 08:45. Price: ₹4,299."
        r = masker.mask(text)
        assert r.masked == text
        assert not r.was_modified

    def test_empty_string_safe(self, masker):
        r = masker.mask("")
        assert r.masked == ""
        assert not r.was_modified

    def test_none_like_inputs(self, masker):
        r = masker.mask("   ")
        assert not r.was_modified


class TestMaskForLog:

    def test_log_strips_aadhaar(self, masker):
        log = "user authenticated, Aadhaar: 1234 5678 9012"
        masked = masker.mask_for_log(log)
        assert "1234 5678 9012" not in masked

    def test_log_strips_api_key(self, masker):
        log = "request with api_key=abcdef1234567890abcdef1234"
        masked = masker.mask_for_log(log)
        assert "abcdef1234567890abcdef1234" not in masked

    def test_log_preserves_structure(self, masker):
        log = "task_id=abc123 user_id=xyz status=completed"
        masked = masker.mask_for_log(log)
        # Structure should be preserved, no PII in this string
        assert "task_id=abc123" in masked
        assert "status=completed" in masked
