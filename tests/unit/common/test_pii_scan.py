"""Unit tests for common.pii_scan -- issue #342 Phase 1's advisory,
non-blocking sensitive-data-pattern scan. Pure functions, no DB, mirroring
tests/unit/common/test_content_advisory.py's structure.
"""

from __future__ import annotations

from common.pii_scan import detect_pii_risks

# A real, valid (but public/well-known-example) Visa test number -- passes
# the Luhn checksum, same one payment gateways publish as a sandbox test
# card, not a real cardholder's number.
_VALID_VISA = "4532015112830366"
_INVALID_LUHN = "4532015112830367"

# A real routing number (JPMorgan Chase, publicly documented) -- passes the
# ABA checksum.
_VALID_ROUTING = "021000021"
_INVALID_ABA = "123456789"


class TestSsn:
    def test_dashed_ssn_is_flagged(self):
        adv = detect_pii_risks("Employee SSN: 234-56-7890 on file.")
        assert [f.kind for f in adv.findings] == ["ssn"]

    def test_invalid_area_is_not_flagged(self):
        assert detect_pii_risks("Case number 000-56-7890 assigned.").findings == ()
        assert detect_pii_risks("Case number 666-56-7890 assigned.").findings == ()
        assert detect_pii_risks("Case number 900-56-7890 assigned.").findings == ()

    def test_invalid_group_or_serial_is_not_flagged(self):
        assert detect_pii_risks("Ref 234-00-7890 in table.").findings == ()
        assert detect_pii_risks("Ref 234-56-0000 in table.").findings == ()

    def test_unformatted_nine_digits_is_not_flagged(self):
        # Deliberately not scanned -- see module docstring on false-positive
        # risk against part numbers/case IDs.
        assert detect_pii_risks("Part number 234567890 in stock.").findings == ()

    def test_clean_text_has_no_findings(self):
        assert detect_pii_risks("A perfectly ordinary quarterly operations report.").findings == ()

    def test_empty_text_has_no_findings(self):
        assert detect_pii_risks("").findings == ()


class TestCreditCard:
    def test_luhn_valid_card_is_flagged(self):
        adv = detect_pii_risks(f"Card on file: {_VALID_VISA}.")
        assert [f.kind for f in adv.findings] == ["credit_card"]

    def test_luhn_invalid_number_is_not_flagged(self):
        assert detect_pii_risks(f"Tracking id {_INVALID_LUHN}.").findings == ()

    def test_grouped_card_number_is_flagged(self):
        grouped = "4532 0151 1283 0366"
        adv = detect_pii_risks(f"Card: {grouped}")
        assert [f.kind for f in adv.findings] == ["credit_card"]


class TestBankRouting:
    def test_checksum_valid_routing_number_is_flagged(self):
        adv = detect_pii_risks(f"Routing number {_VALID_ROUTING} for wire transfers.")
        assert [f.kind for f in adv.findings] == ["bank_routing"]

    def test_checksum_invalid_nine_digits_is_not_flagged(self):
        assert detect_pii_risks(f"Reference {_INVALID_ABA} in log.").findings == ()


class TestApiKeysAndTokens:
    def test_aws_access_key_is_flagged(self):
        # Built via concatenation, not a literal 20-char AKIA-shaped string --
        # secret-scanners (gitleaks, GitHub push protection) match the same
        # public key-ID shape our own detector does and would otherwise flag
        # this fixture as a real leaked credential.
        fake_key = "AKIA" + "A" * 16
        adv = detect_pii_risks(f"Key: {fake_key} in config.")
        assert [f.kind for f in adv.findings] == ["api_key"]
        assert "AWS" in adv.findings[0].detail

    def test_github_token_is_flagged(self):
        token = "ghp_" + "a" * 36
        adv = detect_pii_risks(f"token={token}")
        assert [f.kind for f in adv.findings] == ["api_key"]
        assert "GitHub" in adv.findings[0].detail

    def test_slack_token_is_flagged(self):
        token = "xoxb-" + "a" * 20
        adv = detect_pii_risks(f"Slack bot token {token}")
        assert [f.kind for f in adv.findings] == ["api_key"]

    def test_generic_secret_assignment_is_flagged(self):
        adv = detect_pii_risks('api_key = "not-a-real-value-abcdefghijklmnop"')
        assert [f.kind for f in adv.findings] == ["api_key"]

    def test_short_value_is_not_flagged(self):
        assert detect_pii_risks('api_key = "short"').findings == ()

    def test_private_key_block_header_is_flagged(self):
        adv = detect_pii_risks("-----BEGIN RSA PRIVATE KEY-----\nMIIB...")
        assert [f.kind for f in adv.findings] == ["private_key_block"]


class TestEvidenceNeverEchoesRawValue:
    def test_context_never_contains_the_matched_ssn(self):
        adv = detect_pii_risks("Employee SSN: 234-56-7890 on file.")
        assert "234-56-7890" not in adv.findings[0].context
        assert "[REDACTED]" in adv.findings[0].context

    def test_context_never_contains_the_matched_card_number(self):
        adv = detect_pii_risks(f"Card on file: {_VALID_VISA}.")
        assert _VALID_VISA not in adv.findings[0].context

    def test_detail_never_contains_the_matched_value(self):
        adv = detect_pii_risks("Employee SSN: 234-56-7890 on file.")
        assert "234-56-7890" not in adv.findings[0].detail


class TestCapsAndOrdering:
    def test_findings_are_capped_per_kind(self):
        text = " ".join(f"111-22-{3330 + i:04d}" for i in range(10))
        adv = detect_pii_risks(text)
        assert len(adv.findings) == 5

    def test_findings_are_ordered_by_offset(self):
        text = f"SSN 234-56-7890 then card {_VALID_VISA} later."
        adv = detect_pii_risks(text)
        offsets = [f.offset for f in adv.findings]
        assert offsets == sorted(offsets)


class TestPhoneAndEmailAreOutOfScope:
    def test_phone_number_is_not_flagged(self):
        assert detect_pii_risks("Call 555-867-5309 for support.").findings == ()

    def test_email_address_is_not_flagged(self):
        assert detect_pii_risks("Contact poc@example.mil for questions.").findings == ()
