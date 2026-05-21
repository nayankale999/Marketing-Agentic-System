"""Symmetric encryption for credential payloads stored in
`integration_credential.encrypted_payload`.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with the key in
`settings.credentials_secret`. The key must be 32 random bytes URL-safe-base64
encoded (44 chars). In dev a fixed placeholder is used; prod MUST override
via the env-backed secret store.

Key rotation: planned via `integration_credential.key_version` once a second
key is introduced. For W11 only `key_version=1` exists.
"""

import json
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken

from app.settings.config import get_settings


class CredentialDecryptionError(Exception):
    """Raised when the stored payload can't be decrypted with the current key."""


class EncryptedPayload:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, payload: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(payload, default=str).encode("utf-8"))

    def decrypt(self, ciphertext: bytes | memoryview) -> dict[str, Any]:
        try:
            raw = self._fernet.decrypt(bytes(ciphertext))
        except InvalidToken as exc:
            raise CredentialDecryptionError(
                "credentials_secret cannot decrypt this payload; was the key rotated?"
            ) from exc
        return cast(dict[str, Any], json.loads(raw.decode("utf-8")))


def get_encrypted_payload() -> EncryptedPayload:
    """Build the singleton EncryptedPayload using current settings."""
    return EncryptedPayload(get_settings().credentials_secret)
