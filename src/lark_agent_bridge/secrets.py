"""Encrypted secret store for tool credentials (OpenObserve, Grafana, ...).

Credentials for observability backends must not sit in plaintext ``.env`` (which
is easy to leak or commit). This module keeps them in a small AES-GCM encrypted
JSON blob on disk (``secrets.enc``), unlocked by a key that lives OUTSIDE the
repo (an env var or a key file with tight permissions).

Design:
    - AES-256-GCM via ``pycryptodome`` (already a dependency; no new package).
    - The on-disk format is base64 of ``nonce(12) || tag(16) || ciphertext``,
      so a single opaque string / file.
    - Dotted lookup keys: ``get("openobserve.MTM-DEV.token")``.
    - FAIL-CLOSED: a missing key, missing file, bad key, or missing secret path
      raises :class:`SecretError`. Callers never get a silent empty credential.

The key is resolved (first match wins):
    1. env ``LAB_SECRET_KEY``             (raw base64 of a 32-byte key)
    2. file at env ``LAB_SECRET_KEY_FILE`` (contents = base64 key)

Use :func:`generate_key` once to mint a key, store it outside the repo, then
:meth:`SecretStore.set_many` + :meth:`SecretStore.save` to write secrets. The
agent only ever calls :meth:`SecretStore.get`.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # GCM standard nonce
_TAG_BYTES = 16


class SecretError(Exception):
    """Raised for any secret-store failure (fail-closed; never returns empty)."""


def generate_key() -> str:
    """Return a fresh base64-encoded 32-byte key (store this outside the repo)."""
    return base64.b64encode(get_random_bytes(_KEY_BYTES)).decode("ascii")


def _resolve_key(env_key: str | None = None, key_file: str | None = None) -> bytes:
    """Resolve raw key bytes from an env var or a key file. Fail-closed."""
    raw_b64 = env_key if env_key is not None else os.getenv("LAB_SECRET_KEY")
    if not raw_b64:
        path = key_file if key_file is not None else os.getenv("LAB_SECRET_KEY_FILE")
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                raise SecretError(f"secret key file not found: {p}")
            raw_b64 = p.read_text(encoding="utf-8").strip()
    if not raw_b64:
        raise SecretError(
            "no secret key configured (set LAB_SECRET_KEY or LAB_SECRET_KEY_FILE)"
        )
    try:
        key = base64.b64decode(raw_b64)
    except Exception as exc:  # noqa: BLE001 - normalise to SecretError
        raise SecretError(f"secret key is not valid base64: {exc}") from None
    if len(key) != _KEY_BYTES:
        raise SecretError(f"secret key must be {_KEY_BYTES} bytes, got {len(key)}")
    return key


def encrypt(plaintext: bytes, key: bytes) -> str:
    """AES-256-GCM encrypt -> base64(nonce || tag || ciphertext)."""
    nonce = get_random_bytes(_NONCE_BYTES)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return base64.b64encode(nonce + tag + ciphertext).decode("ascii")


def decrypt(blob_b64: str, key: bytes) -> bytes:
    """Inverse of :func:`encrypt`. Raises :class:`SecretError` on tamper/bad key."""
    try:
        blob = base64.b64decode(blob_b64)
    except Exception as exc:  # noqa: BLE001
        raise SecretError(f"secret blob is not valid base64: {exc}") from None
    if len(blob) < _NONCE_BYTES + _TAG_BYTES:
        raise SecretError("secret blob too short / corrupt")
    nonce = blob[:_NONCE_BYTES]
    tag = blob[_NONCE_BYTES : _NONCE_BYTES + _TAG_BYTES]
    ciphertext = blob[_NONCE_BYTES + _TAG_BYTES :]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        return cipher.decrypt_and_verify(ciphertext, tag)
    except (ValueError, KeyError) as exc:
        raise SecretError(
            f"secret decryption failed (bad key or tampered): {exc}"
        ) from None


class SecretStore:
    """Load/save a nested dict of secrets from an encrypted file.

    :meth:`get` takes a dotted path. The store never logs or prints values.
    """

    def __init__(
        self,
        path: str | Path,
        env_key: str | None = None,
        key_file: str | None = None,
    ):
        self.path = Path(path).expanduser()
        self._key = _resolve_key(env_key, key_file)
        self._data: dict[str, Any] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        blob = self.path.read_text(encoding="utf-8").strip()
        if not blob:
            self._data = {}
            return
        plaintext = decrypt(blob, self._key)
        try:
            self._data = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise SecretError(f"decrypted secrets are not valid JSON: {exc}") from None

    def save(self) -> None:
        """Encrypt the current secret dict and write it to disk."""
        plaintext = json.dumps(self._data).encode("utf-8")
        blob = encrypt(plaintext, self._key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(blob, encoding="utf-8")

    def set(self, dotted_key: str, value: Any) -> None:
        """Set a secret at a dotted path (in memory; call :meth:`save` to persist)."""
        parts = dotted_key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise SecretError(f"cannot set '{dotted_key}': '{part}' is not a map")
        node[parts[-1]] = value

    def set_many(self, flat: dict[str, Any]) -> None:
        """Convenience: set multiple dotted keys at once."""
        for k, v in flat.items():
            self.set(k, v)

    def get(self, dotted_key: str) -> Any:
        """Return the secret at a dotted path. Fail-closed (raises if missing)."""
        parts = dotted_key.split(".")
        node: Any = self._data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                raise SecretError(f"secret not found: '{dotted_key}'")
            node = node[part]
        return node

    def get_default(self, dotted_key: str, default: Any = None) -> Any:
        """Like :meth:`get` but returns *default* instead of raising when missing."""
        try:
            return self.get(dotted_key)
        except SecretError:
            return default

    def has(self, dotted_key: str) -> bool:
        try:
            self.get(dotted_key)
            return True
        except SecretError:
            return False


__all__ = [
    "SecretStore",
    "SecretError",
    "generate_key",
    "encrypt",
    "decrypt",
]
