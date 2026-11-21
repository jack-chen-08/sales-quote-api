"""运行配置。

所有配置只从环境变量读取，不允许硬编码连接串或密钥。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务配置（env 优先，无 .env 依赖）。"""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # 数据库连接串必须由环境变量提供；默认值仅用于本地 / 测试（SQLite 内存库）。
    database_url: str = Field(
        default="sqlite+pysqlite:///:memory:",
        alias="DATABASE_URL",
        description="SQLAlchemy 连接串，生产为 postgresql+psycopg://...",
    )

    # 报价有效期：审批通过后 N 天过期
    quote_valid_days: int = Field(default=15, alias="QUOTE_VALID_DAYS")

    # 报价单号前缀（业务主体固定 BJ）
    quote_no_prefix: str = Field(default="BJ", alias="QUOTE_NO_PREFIX")

    # 默认操作人（未传 created_by 时使用），生产由网关注入真实工号
    default_actor: str = Field(default="system", alias="DEFAULT_ACTOR")

    # 计算引擎版本号（写入 calc_snapshot，便于回溯）
    engine_version: str = Field(default="stub-engine-1.0.0", alias="PRICING_ENGINE_VERSION")


def get_settings() -> Settings:
    """每次调用重新读取环境变量，便于测试中 monkeypatch。"""
    return Settings()


settings = get_settings()
