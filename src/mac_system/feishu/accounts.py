"""Configured Feishu bot accounts and account-scoped bridge construction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


DEFAULT_FEISHU_BOT_ACCOUNT_ID = "default"
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class FeishuBotAccount:
    """One independently authenticated Feishu bot application."""

    id: str
    app_id: str
    app_secret: str
    verification_token: str | None = None
    encrypt_key: str | None = None


def _account_from_mapping(account_id: str, raw: Any) -> FeishuBotAccount:
    if not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError(f"invalid Feishu bot account id: {account_id!r}")
    if not isinstance(raw, dict):
        raise ValueError(f"Feishu bot account {account_id!r} must be an object")
    app_id = str(raw.get("app_id") or "").strip()
    app_secret = str(raw.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise ValueError(
            f"Feishu bot account {account_id!r} requires app_id and app_secret"
        )
    return FeishuBotAccount(
        id=account_id,
        app_id=app_id,
        app_secret=app_secret,
        verification_token=(
            str(raw.get("verification_token") or "").strip() or None
        ),
        encrypt_key=str(raw.get("encrypt_key") or "").strip() or None,
    )


def get_feishu_bot_accounts(settings: Any) -> dict[str, FeishuBotAccount]:
    """Return configured bot accounts while preserving the legacy singleton."""

    accounts: dict[str, FeishuBotAccount] = {}
    raw_json = str(getattr(settings, "feishu_bot_accounts_json", "") or "").strip()
    if raw_json:
        try:
            decoded = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError("FEISHU_BOT_ACCOUNTS_JSON is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("FEISHU_BOT_ACCOUNTS_JSON must be a JSON object")
        for account_id, raw in decoded.items():
            normalized_id = str(account_id).strip()
            accounts[normalized_id] = _account_from_mapping(normalized_id, raw)

    legacy_app_id = str(getattr(settings, "feishu_app_id", "") or "").strip()
    legacy_secret = str(getattr(settings, "feishu_app_secret", "") or "").strip()
    if legacy_app_id and legacy_secret:
        legacy_id = str(
            getattr(settings, "feishu_default_bot_account_id", "")
            or DEFAULT_FEISHU_BOT_ACCOUNT_ID
        ).strip()
        accounts.setdefault(
            legacy_id,
            FeishuBotAccount(
                id=legacy_id,
                app_id=legacy_app_id,
                app_secret=legacy_secret,
                verification_token=getattr(
                    settings, "feishu_verification_token", None
                ),
                encrypt_key=getattr(settings, "feishu_encrypt_key", None),
            ),
        )

    app_owners: dict[str, str] = {}
    for account in accounts.values():
        prior = app_owners.setdefault(account.app_id, account.id)
        if prior != account.id:
            raise ValueError(
                f"Feishu app_id {account.app_id!r} is configured for both "
                f"{prior!r} and {account.id!r}"
            )
    return accounts


def get_feishu_bot_account(
    settings: Any, account_id: str | None = None
) -> FeishuBotAccount | None:
    accounts = get_feishu_bot_accounts(settings)
    selected = str(
        account_id
        or getattr(settings, "feishu_default_bot_account_id", "")
        or DEFAULT_FEISHU_BOT_ACCOUNT_ID
    )
    return accounts.get(selected)


def create_feishu_bridge(settings: Any, account_id: str | None = None):
    """Create the bridge for one account without exposing credentials to callers."""

    account = get_feishu_bot_account(settings, account_id)
    if account is None:
        return None
    from mac_system.feishu.bridge import FeishuBridge

    return FeishuBridge(
        app_id=account.app_id,
        app_secret=account.app_secret,
        encrypt_key=account.encrypt_key,
        verification_token=account.verification_token,
    )
