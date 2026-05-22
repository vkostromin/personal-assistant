import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

import logging

log = logging.getLogger(__name__)


def _make_key(password: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(password.encode()).digest())


def encrypt(plaintext: str, password: str) -> str:
    if not password:
        return plaintext
    return Fernet(_make_key(password)).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, password: str) -> str:
    if not password:
        return ciphertext
    try:
        return Fernet(_make_key(password)).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        log.warning("Token not encrypted (old format), returning as-is")
        return ciphertext
