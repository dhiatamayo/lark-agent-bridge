"""Tests for the encrypted secret store."""

from __future__ import annotations

import base64

import pytest

from lark_agent_bridge.secrets import (
    SecretError,
    SecretStore,
    decrypt,
    encrypt,
    generate_key,
)


def test_generate_key_is_32_bytes_base64():
    key = generate_key()
    raw = base64.b64decode(key)
    assert len(raw) == 32


def test_encrypt_decrypt_roundtrip():
    key = base64.b64decode(generate_key())
    plaintext = b'{"hello": "world"}'
    blob = encrypt(plaintext, key)
    assert decrypt(blob, key) == plaintext


def test_decrypt_with_wrong_key_fails():
    key1 = base64.b64decode(generate_key())
    key2 = base64.b64decode(generate_key())
    blob = encrypt(b"secret", key1)
    with pytest.raises(SecretError):
        decrypt(blob, key2)


def test_decrypt_tampered_blob_fails():
    key = base64.b64decode(generate_key())
    blob = encrypt(b"secret", key)
    raw = bytearray(base64.b64decode(blob))
    raw[-1] ^= 0x01  # flip a ciphertext bit
    tampered = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(SecretError):
        decrypt(tampered, key)


def test_store_set_get_roundtrip(tmp_path):
    key = generate_key()
    path = tmp_path / "secrets.enc"
    store = SecretStore(path, env_key=key)
    store.set("openobserve.MTM-DEV.token", "abc123")
    store.set("openobserve.MTM-DEV.user", "me@sam.com")
    store.save()

    # Reload from disk with the same key.
    store2 = SecretStore(path, env_key=key)
    assert store2.get("openobserve.MTM-DEV.token") == "abc123"
    assert store2.get("openobserve.MTM-DEV.user") == "me@sam.com"


def test_store_set_many(tmp_path):
    key = generate_key()
    store = SecretStore(tmp_path / "s.enc", env_key=key)
    store.set_many({
        "openobserve.MTM-DEV.token": "t1",
        "grafana.gateway.api_key": "g1",
    })
    assert store.get("openobserve.MTM-DEV.token") == "t1"
    assert store.get("grafana.gateway.api_key") == "g1"


def test_get_missing_fails_closed(tmp_path):
    store = SecretStore(tmp_path / "s.enc", env_key=generate_key())
    with pytest.raises(SecretError):
        store.get("does.not.exist")


def test_get_default_returns_fallback(tmp_path):
    store = SecretStore(tmp_path / "s.enc", env_key=generate_key())
    assert store.get_default("nope.nope", "fallback") == "fallback"


def test_has(tmp_path):
    store = SecretStore(tmp_path / "s.enc", env_key=generate_key())
    store.set("a.b", "c")
    assert store.has("a.b") is True
    assert store.has("a.x") is False


def test_no_key_configured_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_SECRET_KEY", raising=False)
    monkeypatch.delenv("LAB_SECRET_KEY_FILE", raising=False)
    with pytest.raises(SecretError):
        SecretStore(tmp_path / "s.enc")


def test_key_from_file(tmp_path):
    key = generate_key()
    key_path = tmp_path / "secret.key"
    key_path.write_text(key, encoding="utf-8")
    store = SecretStore(tmp_path / "s.enc", key_file=str(key_path))
    store.set("x.y", "z")
    store.save()
    store2 = SecretStore(tmp_path / "s.enc", key_file=str(key_path))
    assert store2.get("x.y") == "z"


def test_wrong_key_length_fails(tmp_path):
    short_key = base64.b64encode(b"too-short").decode("ascii")
    with pytest.raises(SecretError):
        SecretStore(tmp_path / "s.enc", env_key=short_key)


def test_reload_with_wrong_key_fails(tmp_path):
    key1 = generate_key()
    store = SecretStore(tmp_path / "s.enc", env_key=key1)
    store.set("a", "b")
    store.save()
    with pytest.raises(SecretError):
        SecretStore(tmp_path / "s.enc", env_key=generate_key())
