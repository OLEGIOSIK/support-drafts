import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://support:support@localhost:5432/support"
    db_pool_size: int = 10

    # --- LLM ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"
    llm_max_tokens: int = 4000
    llm_timeout_s: float = 60.0
    llm_rate_limit_per_min: int = 60  # global, across all worker processes
    llm_concurrency: int = 30  # in-flight LLM calls per worker process
    stub_delay_min_s: float = 5.0
    stub_delay_max_s: float = 30.0
    gen_max_attempts: int = 5

    # --- Delivery to helpdesk ---
    send_timeout_s: float = 10.0
    send_max_attempts: int = 8
    send_concurrency: int = 20

    # --- URLs ---
    # Public URL of this service (used for KB links in drafts). Render sets RENDER_EXTERNAL_URL.
    public_base_url: str = ""
    # How the worker reaches this service's own HTTP (for the helpdesk mock).
    internal_base_url: str = ""
    # reply_url hosts that are redirected to the built-in helpdesk mock.
    mock_helpdesk_hosts: str = "helpdesk.example.com"
    # If non-empty: comma-separated allowlist for reply_url hosts (SSRF protection).
    reply_url_allowed_hosts: str = ""

    # --- Security ---
    webhook_secret: str = ""
    operator_password: str = ""

    # --- Helpdesk mock behaviour ---
    mock_fail_rate: float = 0.2
    mock_slow_rate: float = 0.1
    mock_slow_seconds: float = 15.0

    # Run worker loops inside the web process (single-container hosting).
    run_worker: bool = False

    @property
    def public_url(self) -> str:
        url = self.public_base_url or os.environ.get("RENDER_EXTERNAL_URL") or "http://localhost:8000"
        return url.rstrip("/")

    @property
    def internal_url(self) -> str:
        url = self.internal_base_url or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
        return url.rstrip("/")

    @property
    def mock_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.mock_helpdesk_hosts.split(",") if h.strip()}

    @property
    def allowed_reply_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.reply_url_allowed_hosts.split(",") if h.strip()}


settings = Settings()
