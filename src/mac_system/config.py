import json
import os
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
    database_url: str = Field(default="sqlite:///./mac_system.db", alias="DATABASE_URL")
    projects_dir: Path = Field(default=Path("./projects"), alias="PROJECTS_DIR")
    state_machines_dir: Path = Field(default=Path("./state_machines"), alias="STATE_MACHINES_DIR")
    agent_config_path: Path = Field(default=Path("./agent_config.json"), alias="AGENT_CONFIG_PATH")

    # Claude Code
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_auth_token: str | None = Field(default=None, alias="ANTHROPIC_AUTH_TOKEN")
    claude_bin: Path = Field(default=Path("/Users/yihanwang/.local/node/bin/claude"), alias="CLAUDE_BIN")
    claude_model: str | None = Field(default=None, validation_alias=AliasChoices("CLAUDE_MODEL", "ANTHROPIC_MODEL"))
    anthropic_base_url: str | None = Field(default=None, alias="ANTHROPIC_BASE_URL")
    anthropic_default_opus_model: str | None = Field(default=None, alias="ANTHROPIC_DEFAULT_OPUS_MODEL")
    anthropic_default_sonnet_model: str | None = Field(default=None, alias="ANTHROPIC_DEFAULT_SONNET_MODEL")
    anthropic_default_haiku_model: str | None = Field(default=None, alias="ANTHROPIC_DEFAULT_HAIKU_MODEL")
    claude_code_effort_level: str | None = Field(default=None, alias="CLAUDE_CODE_EFFORT_LEVEL")
    api_timeout_ms: int | None = Field(default=None, alias="API_TIMEOUT_MS")

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

    # Admin panel
    admin_token: str | None = Field(default=None, alias="ADMIN_TOKEN")


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


def load_state_machine(task_type: str) -> dict[str, Any]:
    path = get_settings().state_machines_dir / f"{task_type}.json"
    if not path.exists():
        raise FileNotFoundError(f"State machine for task_type={task_type} not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
