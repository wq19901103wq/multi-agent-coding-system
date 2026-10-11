"""Generate opaque IDs without importing persistence or runtime services."""

import secrets
from datetime import datetime


def generate_id(prefix: str) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}_{secrets.token_hex(8)}"
