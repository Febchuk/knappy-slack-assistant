"""Runtime settings loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required settings are missing."""


def load_dotenv(path: Path | None = None) -> None:
    """Load the repo-root `.env` into the process. Existing variables win."""
    env_path = path or Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] not in {'"', "'"} and " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


DEFAULT_MODEL_AGENT = "gemini-3-flash-preview"
DEFAULT_MODEL_LIGHT = "gemini-3.1-flash-lite-preview"
REQUIRED = ("SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET", "GEMINI_API_KEY")
# Spec 22: without these, Knappy serves only the workspace SLACK_BOT_TOKEN belongs to.
DISTRIBUTION = ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "KNAPPY_PUBLIC_URL", "KNAPPY_SECRET_KEY")


@dataclass(frozen=True)
class Settings:
    slack_app_token: str
    slack_signing_secret: str
    gemini_api_key: str
    # Spec 22: the workspace that installed the app by hand. OAuth installs keep their tokens in the database.
    slack_bot_token: str | None = None
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    database_url: str = "sqlite:///knappy.db"
    model_agent: str = DEFAULT_MODEL_AGENT
    model_light: str = DEFAULT_MODEL_LIGHT
    daily_budget_usd: float = 1.00
    admission_threshold: float = 0.4
    raw_retention_days: int = 90
    # Spec 18: the installer's User OAuth Token. Without it, workspace awareness is off.
    slack_user_token: str | None = None
    awareness_threshold: float = 0.5
    # Spec 19: MCP connections need both a public HTTPS origin for the OAuth redirect and a secret for token encryption.
    public_url: str | None = None
    secret_key: str | None = None
    callback_port: int = 8080

    @property
    def distributes(self) -> bool:
        """Other workspaces can install Knappy through /slack/install."""
        return bool(self.slack_client_id and self.slack_client_secret and self.public_url and self.secret_key)

    @classmethod
    def from_env(cls) -> Settings:
        missing = [name for name in REQUIRED if not os.environ.get(name)]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")
        if not os.environ.get("SLACK_BOT_TOKEN") and not all(os.environ.get(name) for name in DISTRIBUTION):
            raise ConfigError(f"Set SLACK_BOT_TOKEN, or set {', '.join(DISTRIBUTION)} to install through OAuth")
        return cls(
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN") or None,
            slack_client_id=os.environ.get("SLACK_CLIENT_ID") or None,
            slack_client_secret=os.environ.get("SLACK_CLIENT_SECRET") or None,
            slack_app_token=os.environ["SLACK_APP_TOKEN"],
            slack_signing_secret=os.environ["SLACK_SIGNING_SECRET"],
            gemini_api_key=os.environ["GEMINI_API_KEY"],
            database_url=os.environ.get("KNAPPY_DATABASE_URL", "sqlite:///knappy.db"),
            model_agent=os.environ.get("KNAPPY_MODEL_AGENT") or DEFAULT_MODEL_AGENT,
            model_light=os.environ.get("KNAPPY_MODEL_LIGHT") or DEFAULT_MODEL_LIGHT,
            daily_budget_usd=_number("KNAPPY_DAILY_BUDGET_USD", "1.00", float),
            admission_threshold=_number("KNAPPY_ADMISSION_THRESHOLD", "0.4", float),
            raw_retention_days=_number("KNAPPY_RAW_RETENTION_DAYS", "90", int),
            slack_user_token=os.environ.get("SLACK_USER_TOKEN") or None,
            awareness_threshold=_number("KNAPPY_AWARENESS_THRESHOLD", "0.5", float),
            public_url=os.environ.get("KNAPPY_PUBLIC_URL") or None,
            secret_key=os.environ.get("KNAPPY_SECRET_KEY") or None,
            callback_port=_number("KNAPPY_CALLBACK_PORT", "8080", int),
        )


def _number(name: str, default: str, kind: type[float] | type[int]):
    raw = os.environ.get(name) or default
    try:
        return kind(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
