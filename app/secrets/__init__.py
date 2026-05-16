"""Secret store abstraction.

In dev/CI we read secrets from environment variables (EnvSecretStore).
In prod we'll plug in a real KMS-backed store (KmsSecretStore — stub until
deploy target is picked). Application code should NEVER read os.environ
directly for secret material; route through `get_secret_store().get(key)`
so logs/redaction/audit can intercept consistently.
"""

import os
from typing import Protocol


class SecretStore(Protocol):
    """Minimal contract for a secret store."""

    def get(self, key: str) -> str | None:
        """Return the secret value or None if missing."""
        ...

    def require(self, key: str) -> str:
        """Return the secret value or raise KeyError if missing."""
        ...


class _BaseStore:
    """Shared implementation of `require()` on top of `get()`."""

    def get(self, key: str) -> str | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None or value == "":
            raise KeyError(f"required secret '{key}' is not configured")
        return value


class EnvSecretStore(_BaseStore):
    """Reads secrets from environment variables.

    Suitable for dev, CI, and single-host deployments. Setting is intentionally
    not implemented — operators rotate by re-deploying with new env values.
    """

    def get(self, key: str) -> str | None:
        return os.environ.get(key)


class KmsSecretStore(_BaseStore):
    """Stub for a cloud KMS-backed store. Implementation lands with the deploy
    target choice (Fly.io / Railway / AWS Secrets Manager / Vault).
    """

    def __init__(self, kms_key_id: str = "") -> None:
        self.kms_key_id = kms_key_id

    def get(self, key: str) -> str | None:
        raise NotImplementedError(
            "KmsSecretStore is a stub. Implement when deploy target is chosen "
            "(see docs/build-plan.md § Locked decisions)."
        )


_default_store: SecretStore = EnvSecretStore()


def get_secret_store() -> SecretStore:
    """Return the configured secret store. Override with `set_secret_store()` in tests."""
    return _default_store


def set_secret_store(store: SecretStore) -> None:
    """Replace the global secret store. Use sparingly — for tests or a custom
    init path."""
    global _default_store
    _default_store = store


__all__ = [
    "EnvSecretStore",
    "KmsSecretStore",
    "SecretStore",
    "get_secret_store",
    "set_secret_store",
]
