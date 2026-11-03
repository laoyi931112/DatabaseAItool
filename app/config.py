from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "DatabaseAItool"
    host: str = "127.0.0.1"
    port: int = 8000

    data_dir: Path = BASE_DIR / "data"

    # 设置 admin_password 后，管理界面启用 HTTP Basic 认证；MCP 端点始终使用 API Key。
    admin_username: str = "admin"
    admin_password: str | None = None

    # MCP 传输层 DNS 重绑定保护的允许 Host，逗号分隔；留空时仅允许本机。
    mcp_allowed_hosts: str = ""
    # 关闭 MCP 的 Host/Origin 校验；仅在可信反向代理后使用。
    mcp_disable_host_check: bool = False

    default_max_rows: int = 500
    hard_max_rows: int = 5000
    default_query_timeout: int = 30
    connect_timeout: int = 5

    # 同一 API Key 的 last_used_at 最小刷新间隔，避免每个工具调用都写库。
    last_used_update_interval_seconds: int = 60

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
