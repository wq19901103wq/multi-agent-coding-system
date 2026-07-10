import base64
import binascii
import hashlib
import hmac
import json
from typing import Any

from lark_oapi import AESCipher


def decrypt_feishu(encrypt_key: str, encrypted: str) -> dict[str, Any]:
    """Decrypt a Feishu encrypted payload string."""
    if not encrypt_key:
        raise ValueError("encrypt_key is required for decryption")
    cipher = AESCipher(encrypt_key)
    decrypted = cipher.decrypt(base64.b64decode(encrypted))
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
