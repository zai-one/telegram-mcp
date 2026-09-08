from __future__ import annotations

from zai_telegram.sanitizer import (
    REDACTED,
    TRUNCATED,
    SanitizerLimits,
    sanitize_provider_response,
)


def test_recursive_response_sanitizer_redacts_keys_and_inline_credentials() -> None:
    source = {
        "result": [
            {
                "id": "record-1",
                "accessToken": "fixture-access-token",
                "nested": {
                    "api_key": "fixture-api-key",
                    "note": "Authorization: Bearer fixture-bearer",
                    "url": "https://example.invalid/?token=fixture-query&safe=yes",
                    "diagnostic": "access_token=fixture-inline-access",
                },
            }
        ]
    }

    sanitized = sanitize_provider_response(source)

    assert sanitized["result"][0]["id"] == "record-1"
    assert sanitized["result"][0]["accessToken"] == REDACTED
    assert sanitized["result"][0]["nested"]["api_key"] == REDACTED
    rendered = repr(sanitized)
    assert "fixture-access-token" not in rendered
    assert "fixture-api-key" not in rendered
    assert "fixture-bearer" not in rendered
    assert "fixture-query" not in rendered
    assert "fixture-inline-access" not in rendered


def test_response_sanitizer_bounds_depth_items_strings_and_handles_cycles() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    source = {
        "deep": {"level1": {"level2": {"level3": "hidden"}}},
        "items": ["first", "second", "third"],
        "long": "abcdefghij",
        "cycle": cyclic,
    }

    sanitized = sanitize_provider_response(
        source,
        limits=SanitizerLimits(max_depth=2, max_items=20, max_string_chars=5, max_key_chars=32),
    )

    assert TRUNCATED in repr(sanitized["deep"])
    assert sanitized["long"] == f"abcde{TRUNCATED}"
    assert sanitized["cycle"]["self"] == "***cycle-redacted***"

    item_bounded = sanitize_provider_response(
        {"one": 1, "two": 2, "three": 3},
        limits=SanitizerLimits(max_depth=2, max_items=2, max_string_chars=10, max_key_chars=10),
    )
    assert item_bounded == {"one": 1, "two": 2, "__truncated__": TRUNCATED}


def test_response_sanitizer_returns_json_safe_values_for_binary_and_unknown_objects() -> None:
    sanitized = sanitize_provider_response({"binary": b"fixture-secret", "object": object()})

    assert sanitized == {
        "binary": "***binary-redacted***",
        "object": "***unsupported:object***",
    }


def test_sanitizer_preserves_boolean_secret_flags_and_contract_fields() -> None:
    sanitized = sanitize_provider_response(
        {
            "secret_disclosed": False,
            "token_present": True,
            "session": None,
            "api_key": "fixture-value",
            "token_count": 3,
        }
    )

    assert sanitized["secret_disclosed"] is False
    assert sanitized["token_present"] is True
    assert sanitized["session"] is None
    assert sanitized["api_key"] == REDACTED
    assert sanitized["token_count"] == REDACTED


def test_default_limits_do_not_truncate_large_legitimate_payloads() -> None:
    page = "x" * 200_000
    rows = [{"campaign": index, "clicks": index} for index in range(5_000)]

    sanitized = sanitize_provider_response({"markdown": page, "rows": rows})

    assert sanitized["markdown"] == page
    assert len(sanitized["rows"]) == 5_000
    assert TRUNCATED not in repr(sanitized["rows"][-1])
