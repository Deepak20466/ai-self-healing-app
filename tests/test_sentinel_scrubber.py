"""Unit tests for sentinel.scrubber: secret/PII redaction."""

from __future__ import annotations

from sentinel.scrubber import scrub_dict, scrub_text


def test_scrubs_email_addresses() -> None:
    scrubbed = scrub_text("contact user@example.com for details")
    assert scrubbed == "contact [REDACTED_EMAIL] for details"


def test_scrubs_bearer_tokens() -> None:
    scrubbed = scrub_text("Authorization: Bearer abc123.def456-ghi")
    assert "abc123.def456-ghi" not in scrubbed
    assert "Bearer [REDACTED]" in scrubbed


def test_scrubs_github_tokens() -> None:
    scrubbed = scrub_text("token=ghp_" + "a" * 36)
    assert "ghp_" not in scrubbed


def test_scrubs_anthropic_keys() -> None:
    scrubbed = scrub_text("key: sk-ant-" + "b" * 30)
    assert "sk-ant-" not in scrubbed


def test_scrubs_key_value_secrets_in_free_text() -> None:
    scrubbed = scrub_text("password=hunter2 and more text")
    assert "hunter2" not in scrubbed


def test_scrub_dict_redacts_sensitive_keys_regardless_of_value() -> None:
    scrubbed = scrub_dict({"Authorization": "whatever-shape", "note": "hello"})
    assert scrubbed["Authorization"] == "[REDACTED]"
    assert scrubbed["note"] == "hello"


def test_scrub_dict_recurses_into_nested_structures() -> None:
    scrubbed = scrub_dict(
        {"headers": {"cookie": "session=abc"}, "items": [{"password": "x"}, "user@example.com"]}
    )
    assert scrubbed["headers"]["cookie"] == "[REDACTED]"
    assert scrubbed["items"][0]["password"] == "[REDACTED]"
    assert scrubbed["items"][1] == "[REDACTED_EMAIL]"


def test_prompt_injection_style_text_is_not_specially_treated_but_still_scrubbed() -> None:
    scrubbed = scrub_text("ignore previous instructions, my password=hunter2")
    assert "hunter2" not in scrubbed
    assert "ignore previous instructions" in scrubbed  # scrubbing != prompt-injection defense
