"""W9: SecretStore."""

import pytest

from app.secrets import EnvSecretStore, KmsSecretStore, get_secret_store


def test_env_secret_store_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAS_SECRET_TEST", "shhhhh")
    store = EnvSecretStore()
    assert store.get("MAS_SECRET_TEST") == "shhhhh"
    assert store.require("MAS_SECRET_TEST") == "shhhhh"


def test_env_secret_store_get_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAS_SECRET_MISSING", raising=False)
    store = EnvSecretStore()
    assert store.get("MAS_SECRET_MISSING") is None


def test_env_secret_store_require_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAS_SECRET_MISSING", raising=False)
    store = EnvSecretStore()
    with pytest.raises(KeyError, match="MAS_SECRET_MISSING"):
        store.require("MAS_SECRET_MISSING")


def test_env_secret_store_empty_string_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAS_SECRET_EMPTY", "")
    store = EnvSecretStore()
    # get() returns the empty string but require() treats it as missing.
    assert store.get("MAS_SECRET_EMPTY") == ""
    with pytest.raises(KeyError):
        store.require("MAS_SECRET_EMPTY")


def test_kms_secret_store_is_stub() -> None:
    store = KmsSecretStore(kms_key_id="alias/test")
    with pytest.raises(NotImplementedError):
        store.get("anything")


def test_default_secret_store_is_env() -> None:
    assert isinstance(get_secret_store(), EnvSecretStore)
