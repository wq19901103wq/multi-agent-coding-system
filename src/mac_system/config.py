import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    state_machines_dir: Path = Field(default=Path("./state_machines"), alias="STATE_MACHINES_DIR")
    workflow_profiles_dir: Path = Field(default=Path("./workflow_profiles"), alias="WORKFLOW_PROFILES_DIR")
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
    codex_bin: Path = Field(default=Path("codex"), alias="CODEX_BIN")
    coordinator_codex_home: Path = Field(
        default=Path("~/.mac-system/codex-coordinator"),
        alias="MAC_COORDINATOR_CODEX_HOME",
    )
    agent_codex_home: Path = Field(
        default=Path("~/.mac-system/codex-agents"),
        alias="MAC_AGENT_CODEX_HOME",
    )
    pi_bin: Path = Field(default=Path("pi"), alias="PI_BIN")
    zcode_bin: Path = Field(
        default=Path("/Applications/ZCode.app/Contents/MacOS/ZCode"),
        alias="ZCODE_BIN",
    )

    # Feishu
    feishu_app_id: str | None = Field(default=None, alias="FEISHU_APP_ID")
    feishu_app_secret: str | None = Field(default=None, alias="FEISHU_APP_SECRET")
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
    max_agent_retries: int = Field(default=3, alias="MAX_AGENT_RETRIES")
    max_test_retries: int = Field(default=2, alias="MAX_TEST_RETRIES")
    max_review_retries: int = Field(default=2, alias="MAX_REVIEW_RETRIES")
    agent_timeout_seconds: int = Field(default=600, alias="AGENT_TIMEOUT_SECONDS")

    # Intent router
    intent_high_threshold: float = Field(default=0.85, alias="INTENT_HIGH_THRESHOLD")
    intent_low_threshold: float = Field(default=0.5, alias="INTENT_LOW_THRESHOLD")

    # Agent progress
    debug_agent_progress: bool = Field(default=False, alias="DEBUG_AGENT_PROGRESS")

    # Admin panel
    admin_token: str | None = Field(default=None, alias="ADMIN_TOKEN")
    control_api_token: str | None = Field(default=None, alias="CONTROL_API_TOKEN")

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
    path = get_settings().agent_config_path
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state_machine() -> dict[str, Any]:
    """Load the single delivery state-machine template."""
    path = get_settings().state_machines_dir / "delivery.json"
    return _load_json(path, label="Delivery state machine")


def load_workflow_profile(task_type: str) -> dict[str, Any]:
    """Load task-specific stages without duplicating lifecycle transitions."""
    path = get_settings().workflow_profiles_dir / f"{task_type}.json"
    return _load_json(path, label=f"Workflow profile for task_type={task_type}")


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
