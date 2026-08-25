from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    GATEKEEPER_HOST: str = "0.0.0.0"
    GATEKEEPER_PORT: int = 8080

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    CLASSIFIER_BASE_URL: str = "http://127.0.0.1:9000"

    GATEKEEPER_KEYS_FILE: str = "/etc/gatekeeper/keys.yaml"
    GATEKEEPER_LOG_FILE: str = "/var/log/gatekeeper/requests.jsonl"

    GATEKEEPER_DEFAULT_RATE_LIMIT_PER_MIN: int = 60
    GATEKEEPER_REQUEST_TIMEOUT_SECONDS: int = 120

    GATEKEEPER_LOG_LEVEL: str = "INFO"


settings = Settings()
