"""
Nexus AI — Keychain manager.

All secrets (JWT key, API keys, tokens) live in the OS keychain.
NEVER store secrets in .env files, config files, or environment variables.

Supported backends (auto-selected by `keyring`):
  macOS   → Keychain Access
  Linux   → SecretService (GNOME Keyring / KWallet)
  Windows → Windows Credential Locker
  CI/test → keyring.backend.fail.Keyring (raises, forces explicit mock)
"""
from __future__ import annotations

import secrets
import string
from typing import Optional

import keyring
import keyring.errors
import structlog

log = structlog.get_logger(__name__)

SERVICE = "nexus-ai"


class KeychainError(RuntimeError):
    """Raised when a required secret is missing or unreadable."""


class SecretsManager:
    """
    Thin wrapper around `keyring` with structured logging and
    a strong-password generator for first-run setup.
    """

    def __init__(self, service: str = SERVICE) -> None:
        self._service = service

    # ── Read ─────────────────────────────────────────────────────────────────

    def get(self, key: str, *, required: bool = True) -> Optional[str]:
        """
        Fetch a secret by key name.

        Args:
            key:      Keychain username / secret identifier.
            required: If True and the secret is missing, raise KeychainError.
        """
        try:
            value = keyring.get_password(self._service, key)
        except keyring.errors.KeyringError as exc:
            log.error("keychain_read_error", key=key, error=str(exc))
            raise KeychainError(f"Cannot read '{key}' from keychain: {exc}") from exc

        if value is None:
            if required:
                raise KeychainError(
                    f"Secret '{key}' not found in keychain service '{self._service}'. "
                    f"Run `nexus setup` to initialise secrets."
                )
            log.warning("keychain_secret_missing", key=key)
            return None

        log.debug("keychain_read_ok", key=key)
        return value

    # ── Write ─────────────────────────────────────────────────────────────────

    def set(self, key: str, value: str) -> None:
        """Store a secret in the keychain. Overwrites silently if exists."""
        try:
            keyring.set_password(self._service, key, value)
            log.info("keychain_write_ok", key=key)
        except keyring.errors.KeyringError as exc:
            log.error("keychain_write_error", key=key, error=str(exc))
            raise KeychainError(f"Cannot write '{key}' to keychain: {exc}") from exc

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete(self, key: str) -> None:
        """Remove a secret. Silently ignores missing keys."""
        try:
            keyring.delete_password(self._service, key)
            log.info("keychain_delete_ok", key=key)
        except keyring.errors.PasswordDeleteError:
            log.warning("keychain_delete_missing", key=key)
        except keyring.errors.KeyringError as exc:
            raise KeychainError(f"Cannot delete '{key}' from keychain: {exc}") from exc

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def generate_strong_secret(length: int = 64) -> str:
        """
        Cryptographically secure random secret string.
        Uses secrets.choice (CSPRNG) — NOT random.choice.
        """
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def ensure_jwt_secret(self, key: str = "jwt_secret") -> str:
        """
        Return the JWT signing secret, generating and storing one on first run.
        Idempotent — safe to call on every startup.
        """
        existing = self.get(key, required=False)
        if existing:
            return existing

        log.info("jwt_secret_generating", key=key)
        new_secret = self.generate_strong_secret(64)
        self.set(key, new_secret)
        log.info("jwt_secret_stored", key=key)
        return new_secret


# Module-level singleton
secrets_manager = SecretsManager()
