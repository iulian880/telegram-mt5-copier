"""Credential storage in the OS keyring (Windows Credential Manager on Windows).

Secrets never go to the config file, the database or logs.  Every secret read
through this module is registered with the log redaction filter.
"""
from __future__ import annotations

import logging

from tgmt5.logging_setup import register_secret

SERVICE = "tgmt5"
log = logging.getLogger(__name__)


class SecretStore:
    def __init__(self, backend=None) -> None:
        if backend is None:
            import keyring
            backend = keyring
        self._kr = backend

    def get(self, key: str) -> str | None:
        try:
            v = self._kr.get_password(SERVICE, key)
        except Exception as e:  # noqa: BLE001 - keyring backends raise various errors
            log.error("keyring read failed for %s: %s", key, type(e).__name__)
            return None
        if v:
            register_secret(v)
        return v

    def set(self, key: str, value: str) -> None:
        register_secret(value)
        self._kr.set_password(SERVICE, key, value)

    def delete(self, key: str) -> None:
        try:
            self._kr.delete_password(SERVICE, key)
        except Exception:  # noqa: BLE001
            pass

    # convenience keys
    TG_API_ID = "telegram_api_id"
    TG_API_HASH = "telegram_api_hash"
    TG_SESSION = "telegram_session"

    @staticmethod
    def mt5_password_key(login: int) -> str:
        return f"mt5_password_{login}"


class MemoryKeyring:
    """In-memory backend for tests and the sandbox. Not persistent."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self._d.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self._d[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        self._d.pop((service, key), None)
