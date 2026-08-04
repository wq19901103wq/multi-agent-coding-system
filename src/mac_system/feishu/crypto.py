import base64
import binascii
import hashlib
import hmac
import json
from typing import Any

from Crypto.Cipher import AES


def _decrypt_aes_cbc(key: str, encrypted: bytes) -> bytes:
    """Feishu SDK's AES-CBC wire algorithm without importing the whole SDK."""

    if len(encrypted) < AES.block_size * 2 or len(encrypted) % AES.block_size != 0:
        raise ValueError("invalid ciphertext length")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    iv = encrypted[: AES.block_size]
    plaintext = AES.new(digest, AES.MODE_CBC, iv).decrypt(
        encrypted[AES.block_size :]
    )
    if not plaintext:
        raise ValueError("invalid ciphertext: empty plaintext")
    pad = plaintext[-1]
    if pad < 1 or pad > AES.block_size or pad > len(plaintext):
        raise ValueError("invalid PKCS7 padding")
    if plaintext[-pad:] != bytes([pad]) * pad:
        raise ValueError("invalid PKCS7 padding")
    return plaintext[:-pad]


def decrypt_feishu(encrypt_key: str, encrypted: str) -> dict[str, Any]:
    """Decrypt a Feishu encrypted payload string."""
    if not encrypt_key:
        raise ValueError("encrypt_key is required for decryption")
    decrypted = _decrypt_aes_cbc(encrypt_key, base64.b64decode(encrypted))
    return json.loads(decrypted.decode("utf-8"))


def verify_signature(timestamp: str, nonce: str, body: str | bytes, signature: str, app_secret: str) -> bool:
    """Verify Feishu event signature using HMAC-SHA256."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):
        return False
    to_sign = (timestamp + nonce).encode("utf-8") + body
    expected = hmac.new(app_secret.encode("utf-8"), to_sign, hashlib.sha256).digest()
    return hmac.compare_digest(signature_bytes, expected)
