from __future__ import annotations

import pytest

from app.security import (
    decrypt_secret,
    encrypt_secret,
    generate_api_key,
    hash_api_key,
)


def test_api_keys_are_unique_and_prefixed() -> None:
    first = generate_api_key()
    second = generate_api_key()
    assert first.startswith("dbt_")
    assert first != second
    assert len(first) > 20


def test_api_key_hash_is_deterministic_and_not_reversible() -> None:
    key = generate_api_key()
    digest = hash_api_key(key)
    assert digest == hash_api_key(key)
    assert len(digest) == 64
    assert key not in digest


def test_secret_roundtrip() -> None:
    token = encrypt_secret("s3cret-password")
    assert token != "s3cret-password"
    assert decrypt_secret(token) == "s3cret-password"
    assert decrypt_secret(None) is None


def test_invalid_token_raises() -> None:
    with pytest.raises(Exception):
        decrypt_secret("not-a-fernet-token")
