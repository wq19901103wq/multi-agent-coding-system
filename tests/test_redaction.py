from mac_system.utils.redaction import redact_secrets, redact_text


def test_redact_text_masks_env_assignments_and_token_like_values():
    value = "API_KEY=sk-abcdefghijklmnopqrstuvwxyz BASE_URL=https://example.test"
    redacted = redact_text(value)
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "API_KEY=[REDACTED]" in redacted
    assert "https://example.test" in redacted


def test_redact_text_masks_query_credentials():
    value = "wss://example.test/ws?access_key=abc123456789&ticket=ticket-secret-value"
    redacted = redact_text(value)
    assert "abc123456789" not in redacted
    assert "ticket-secret-value" not in redacted


def test_redact_secrets_recurses_through_inspection_evidence():
    value = {
        "summary": "found key",
        "findings": [{"evidence": "client_secret: abcdefghijklmnopqrstuvwxyz"}],
    }
    redacted = redact_secrets(value)
    assert redacted["summary"] == "found key"
    assert redacted["findings"][0]["evidence"] == "client_secret: [REDACTED]"
