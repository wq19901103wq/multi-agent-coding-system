#!/usr/bin/env python3
"""Import Feishu app credentials from cc-connect into MAC's local .env.

The script deliberately prints account names only. Credentials never leave the
source config and destination environment file.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from dotenv import dotenv_values, set_key

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


_ACCOUNT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _account_id(project_name: str) -> str:
    value = _ACCOUNT_ID_RE.sub("-", project_name.strip()).strip("-.")
    if not value:
        raise ValueError("cc-connect project name cannot form a MAC account id")
    return value[:64]


def _load_cc_connect_accounts(path: Path) -> dict[str, dict[str, str]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    accounts: dict[str, dict[str, str]] = {}
    app_owners: dict[str, str] = {}
    for project in data.get("projects", []):
        project_name = str(project.get("name") or "").strip()
        for platform in project.get("platforms", []):
            if str(platform.get("type") or "").lower() != "feishu":
                continue
            options = platform.get("options") or {}
            app_id = str(options.get("app_id") or "").strip()
            app_secret = str(options.get("app_secret") or "").strip()
            if not app_id or not app_secret:
                raise ValueError(
                    f"cc-connect project {project_name!r} has incomplete Feishu credentials"
                )
            account_id = _account_id(project_name)
            prior = app_owners.setdefault(app_id, account_id)
            if prior != account_id:
                raise ValueError(
                    f"one Feishu app is assigned to both {prior!r} and {account_id!r}"
                )
            if account_id in accounts and accounts[account_id]["app_id"] != app_id:
                raise ValueError(f"duplicate MAC account id: {account_id!r}")
            account = {"app_id": app_id, "app_secret": app_secret}
            for optional_key in ("verification_token", "encrypt_key"):
                value = str(options.get(optional_key) or "").strip()
                if value:
                    account[optional_key] = value
            accounts[account_id] = account
    return accounts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.home() / ".cc-connect" / "config.toml",
    )
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    target = args.env.expanduser().resolve()
    accounts = _load_cc_connect_accounts(source)
    if not accounts:
        raise SystemExit("no Feishu accounts found in cc-connect config")

    current = dotenv_values(target) if target.exists() else {}
    legacy_app_id = str(current.get("FEISHU_APP_ID") or "").strip()
    legacy_secret = str(current.get("FEISHU_APP_SECRET") or "").strip()
    if legacy_app_id and legacy_secret:
        legacy_account = {
            "app_id": legacy_app_id,
            "app_secret": legacy_secret,
        }
        for env_key, account_key in (
            ("FEISHU_VERIFICATION_TOKEN", "verification_token"),
            ("FEISHU_ENCRYPT_KEY", "encrypt_key"),
        ):
            value = str(current.get(env_key) or "").strip()
            if value:
                legacy_account[account_key] = value
        if all(row["app_id"] != legacy_app_id for row in accounts.values()):
            accounts = {"default": legacy_account, **accounts}

    target.touch(mode=0o600, exist_ok=True)
    target.chmod(0o600)
    payload = json.dumps(accounts, ensure_ascii=True, separators=(",", ":"))
    set_key(str(target), "FEISHU_BOT_ACCOUNTS_JSON", payload, quote_mode="always")
    set_key(
        str(target),
        "FEISHU_DEFAULT_BOT_ACCOUNT_ID",
        "default" if "default" in accounts else next(iter(accounts)),
        quote_mode="always",
    )
    print(f"Imported {len(accounts)} Feishu bot account(s): {', '.join(accounts)}")


if __name__ == "__main__":
    main()
