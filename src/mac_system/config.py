import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_CHATGPT_BUNDLED_CODEX_PATHS = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
)


def default_codex_bin() -> Path:
    """Select a Codex binary compatible with the desktop-owned thread store.

    ChatGPT updates the shared ``~/.codex`` store together with its bundled
    Codex binary. Prefer that binary on macOS so an older standalone CLI does
    not try to resume a newer on-disk thread format. ``CODEX_BIN`` remains an
    explicit override, and hosts without ChatGPT keep PATH-based lookup.
    """

    for bundled in _CHATGPT_BUNDLED_CODEX_PATHS:
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return bundled
    return Path("codex")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API / App
    app_name: str = "MAC System"
    debug: bool = Field(default=False, alias="DEBUG")
    mcs_port: int = Field(default=18080, alias="MCS_PORT")
    database_url: str = Field(default="sqlite:///./mac_system.db", alias="DATABASE_URL")
    projects_dir: Path = Field(default=Path("./projects"), alias="PROJECTS_DIR")
    agent_config_path: Path = Field(default=Path("./agent_config.json"), alias="AGENT_CONFIG_PATH")
    secret_store_path: Path = Field(
        default=Path("./.mac-secrets.json"), alias="MAC_SECRET_STORE_PATH"
    )

    # Claude Code
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_auth_token: str | None = Field(default=None, alias="ANTHROPIC_AUTH_TOKEN")
    claude_bin: Path = Field(default=Path("claude"), alias="CLAUDE_BIN")
    claude_model: str | None = Field(default=None, validation_alias=AliasChoices("CLAUDE_MODEL", "ANTHROPIC_MODEL"))
    anthropic_base_url: str | None = Field(default=None, alias="ANTHROPIC_BASE_URL")
    anthropic_default_opus_model: str | None = Field(default=None, alias="ANTHROPIC_DEFAULT_OPUS_MODEL")
    anthropic_default_sonnet_model: str | None = Field(default=None, alias="ANTHROPIC_DEFAULT_SONNET_MODEL")
    anthropic_default_haiku_model: str | None = Field(default=None, alias="ANTHROPIC_DEFAULT_HAIKU_MODEL")
    claude_code_effort_level: str | None = Field(default=None, alias="CLAUDE_CODE_EFFORT_LEVEL")
    api_timeout_ms: int | None = Field(default=None, alias="API_TIMEOUT_MS")

    # Alternative coding-agent CLIs. Provider selection is configured per
    # Agent in agent_config.json; these paths only describe local executables.
    kimi_bin: Path = Field(default=Path("kimi"), alias="KIMI_BIN")
    codex_bin: Path = Field(default_factory=default_codex_bin, alias="CODEX_BIN")
    pi_bin: Path = Field(default=Path("pi"), alias="PI_BIN")
    zcode_bin: Path = Field(
        default=Path("/Applications/ZCode.app/Contents/MacOS/ZCode"),
        alias="ZCODE_BIN",
    )

    # Feishu
    feishu_app_id: str | None = Field(default=None, alias="FEISHU_APP_ID")
    feishu_app_secret: str | None = Field(default=None, alias="FEISHU_APP_SECRET")
    feishu_bot_accounts_json: str = Field(
        default="", alias="FEISHU_BOT_ACCOUNTS_JSON"
    )
    feishu_default_bot_account_id: str = Field(
        default="default", alias="FEISHU_DEFAULT_BOT_ACCOUNT_ID"
    )
    feishu_verification_token: str | None = Field(default=None, alias="FEISHU_VERIFICATION_TOKEN")
    feishu_encrypt_key: str | None = Field(default=None, alias="FEISHU_ENCRYPT_KEY")
    feishu_event_prefix: str = Field(default="/feishu/events", alias="FEISHU_EVENT_PREFIX")
    feishu_transport: str = Field(default="websocket", alias="FEISHU_TRANSPORT")
    feishu_test_mode: bool = Field(default=False, alias="FEISHU_TEST_MODE")

    # Git
    git_remote_url: str | None = Field(default=None, alias="GIT_REMOTE_URL")
    git_user_name: str = Field(default="MAC System", alias="GIT_USER_NAME")
    git_user_email: str = Field(default="mac@example.com", alias="GIT_USER_EMAIL")

    # Runtime
    agent_timeout_seconds: int = Field(default=600, alias="AGENT_TIMEOUT_SECONDS")
    project_lead_provider: str = Field(
        default="codex", alias="PROJECT_LEAD_PROVIDER"
    )
    project_lead_failover_provider: str = Field(
        default="", alias="PROJECT_LEAD_FAILOVER_PROVIDER"
    )
    project_lead_silence_timeout_seconds: int = Field(
        default=300, ge=1, alias="PROJECT_LEAD_SILENCE_TIMEOUT_SECONDS"
    )
    project_owner_provider: str = Field(
        default="codex", alias="PROJECT_OWNER_PROVIDER"
    )
    project_owner_wakeup_seconds: int = Field(
        default=15, alias="PROJECT_OWNER_WAKEUP_SECONDS"
    )

    # Admin panel
    admin_token: str | None = Field(default=None, alias="ADMIN_TOKEN")
    control_api_token: str | None = Field(default=None, alias="CONTROL_API_TOKEN")
    mac_public_base_url: str | None = Field(default=None, alias="MAC_PUBLIC_BASE_URL")
    mac_file_link_secret: str | None = Field(
        default=None, alias="MAC_FILE_LINK_SECRET"
    )

    # Directory binding (chat-scoped /bind)
    # Comma-separated list of absolute paths the bot is allowed to scan and
    # bind to. Empty by default - /bind (no-args) falls back to asking the
    # user to type an absolute path explicitly.
    mac_allowed_bind_roots: str = Field(default="", alias="MAC_ALLOWED_BIND_ROOTS")


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_agent_config() -> dict[str, Any]:
    return load_agent_config(get_settings().agent_config_path)


def load_agent_config(path: Path) -> dict[str, Any]:
    """Load operator-owned runtime and native Codex role configuration."""

    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_allowed_bind_roots() -> list[Path]:
    """Return the list of absolute paths `/bind` (no-args) is allowed to scan."""
    raw = get_settings().mac_allowed_bind_roots
    if not raw:
        return []
    roots: list[Path] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        path = Path(part).expanduser()
        if not path.is_absolute():
            continue
        roots.append(path)
    return roots


def path_within_allowed_bind_roots(path: str | Path) -> bool:
    """Return whether ``path`` is inside a configured bind root.

    An empty allowlist is fail-closed: explicit host-directory binding remains
    disabled until an operator deliberately configures allowed roots.
    """
    resolved = Path(path).expanduser().resolve()
    for root in get_allowed_bind_roots():
        root_resolved = root.resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False
