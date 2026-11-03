"""BYOK secrets (D7) — the cloud API key lives in Windows Credential Manager,
never in TOML, never in logs. Every key that passes through here is also
registered with the log scrubber, so even an accidental direct log of the
value gets redacted before it hits disk.
"""

from __future__ import annotations

from typing import Protocol

from marshmellow.core.logging import register_secret

SERVICE = "Marshmellow"
ACCOUNT = "llm_api_key"


class KeyringLike(Protocol):
    def set_password(self, service: str, account: str, password: str) -> None: ...

    def get_password(self, service: str, account: str) -> str | None: ...


def store_api_key(key: str, backend: KeyringLike | None = None) -> None:
    register_secret(key)
    if backend is None:
        import keyring

        keyring.set_password(SERVICE, ACCOUNT, key)
    else:
        backend.set_password(SERVICE, ACCOUNT, key)


def load_api_key(backend: KeyringLike | None = None) -> str | None:
    if backend is None:
        import keyring

        key = keyring.get_password(SERVICE, ACCOUNT)
    else:
        key = backend.get_password(SERVICE, ACCOUNT)
    if key:
        register_secret(key)
    return key


def delete_api_key(backend: KeyringLike | None = None) -> None:
    if backend is None:
        import keyring

        keyring.delete_password(SERVICE, ACCOUNT)
    else:
        backend.delete_password(SERVICE, ACCOUNT)  # type: ignore[attr-defined]
