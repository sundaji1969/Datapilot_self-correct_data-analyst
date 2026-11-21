"""Application settings, loaded only from environment / .env."""
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_MODELS = {
    "openai": {"coder": "gpt-4.1", "reviewer": "gpt-4.1", "planner": "gpt-4.1-mini", "answer": "gpt-4.1-mini"},
    "anthropic": {
        "coder": "claude-sonnet-5-5",
        "reviewer": "claude-sonnet-5-5",
        "planner": "claude-haiku-5-5",
        "answer": "claude-haiku-5-5",
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    LLM_PROVIDER: Literal["openai", "anthropic"] = "openai"
    OPENAI_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None

    CODER_MODEL: str = ""
    REVIEWER_MODEL: str = ""
    PLANNER_MODEL: str = ""
    ANSWER_MODEL: str = ""

    MAX_MESSAGES: int = 12
    EXEC_TIMEOUT_S: int = 60
    MAX_FILE_MB: int = 25
    MAX_ROWS: int = 500_000
    TEAM_MODE: Literal["selector", "roundrobin"] = "selector"
    USE_DOCKER: bool = True

    @model_validator(mode="after")
    def _fill_models_and_check_key(self) -> "Settings":
        defaults = _DEFAULT_MODELS[self.LLM_PROVIDER]
        self.CODER_MODEL = self.CODER_MODEL or defaults["coder"]
        self.REVIEWER_MODEL = self.REVIEWER_MODEL or defaults["reviewer"]
        self.PLANNER_MODEL = self.PLANNER_MODEL or defaults["planner"]
        self.ANSWER_MODEL = self.ANSWER_MODEL or defaults["answer"]
        key = self.OPENAI_API_KEY if self.LLM_PROVIDER == "openai" else self.ANTHROPIC_API_KEY
        if key is None or not key.get_secret_value().strip():
            name = "OPENAI_API_KEY" if self.LLM_PROVIDER == "openai" else "ANTHROPIC_API_KEY"
            raise ValueError(f"{name} is missing. Copy .env.example to .env and set {name}.")
        return self

    @property
    def api_key(self) -> SecretStr:
        return self.OPENAI_API_KEY if self.LLM_PROVIDER == "openai" else self.ANTHROPIC_API_KEY  # type: ignore[return-value]
