import pytest

from src.crypto import encrypt, decrypt


def test_encrypt_returns_plaintext_when_password_empty():
    result = encrypt("my-refresh-token", "")
    assert result == "my-refresh-token"


def test_decrypt_returns_ciphertext_when_password_empty():
    result = decrypt("some-ciphertext", "")
    assert result == "some-ciphertext"


def test_encrypt_produces_different_output():
    password = "my-secret-key-1234567890123456"
    token = "refresh-token-abc123"
    encrypted = encrypt(token, password)
    assert encrypted != token
    assert len(encrypted) > 0


def test_encrypt_roundtrip():
    password = "my-secret-key-1234567890123456"
    token = "refresh-token-abc123"
    encrypted = encrypt(token, password)
    decrypted = decrypt(encrypted, password)
    assert decrypted == token


def test_encrypt_different_calls_produce_different_ciphertext_due_to_iv():
    password = "my-secret-key-1234567890123456"
    token = "refresh-token-abc123"
    first = encrypt(token, password)
    second = encrypt(token, password)
    assert first != second  # Fernet uses random IV, so ciphertexts differ


def test_decrypt_invalid_token_returns_as_is():
    password = "my-secret-key-1234567890123456"
    result = decrypt("not-encrypted-data", password)
    assert result == "not-encrypted-data"


def test_encrypt_empty_token():
    password = "my-secret-key-1234567890123456"
    encrypted = encrypt("", password)
    decrypted = decrypt(encrypted, password)
    assert decrypted == ""


def test_different_passwords_produce_different_ciphertext():
    token = "refresh-token-abc123"
    pw1 = "password1-12345678901234567890"
    pw2 = "password2-12345678901234567890"
    assert encrypt(token, pw1) != encrypt(token, pw2)


def test_decrypt_with_wrong_password_returns_as_is():
    token = "refresh-token-abc123"
    pw1 = "password1-12345678901234567890"
    pw2 = "password2-12345678901234567890"
    encrypted = encrypt(token, pw1)
    result = decrypt(encrypted, pw2)
    assert result == encrypted
