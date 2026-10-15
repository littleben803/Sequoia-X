"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。"""

import os
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_path: str = "data/sequoia_v3.db"
    start_date: str = "2024-01-01"
    notifications_enabled: bool = False
    feishu_webhook_url: str | None = None
    strategy_webhooks: dict[str, str] = {}
    enabled_strategies: str = "ma_volume,turtle,flag,rps"
    minimum_latest_coverage: float = 0.95
    max_data_age_days: int = 10

    _parsed_strategy_webhooks: ClassVar[dict[str, str]] = {}

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):  # type: ignore[override]
        """扩展配置源，支持从环境变量扫描 STRATEGY_WEBHOOK_ 前缀。"""
        sources = super().settings_customise_sources(settings_cls, **kwargs)
        prefix = "STRATEGY_WEBHOOK_"
        cls._parsed_strategy_webhooks = {
            key[len(prefix) :].lower(): value
            for key, value in os.environ.items()
            if key.upper().startswith(prefix) and value
        }
        return sources

    def model_post_init(self, __context: object) -> None:
        """将策略专属 Webhook 合并到配置。"""
        # 开发期 V2 默认路径曾写入本地 .env。V3 不允许把新 Schema 混入旧库，
        # 因此只迁移这个精确历史默认值；用户自定义路径仍原样保留。
        if self.db_path == "data/sequoia_v2.db":
            object.__setattr__(self, "db_path", "data/sequoia_v3.db")
        webhooks = dict(self.strategy_webhooks)
        webhooks.update(self._parsed_strategy_webhooks)
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """返回策略对应的 Webhook；通知启用但未配置时明确失败。"""
        url = self.strategy_webhooks.get(webhook_key.lower(), self.feishu_webhook_url)
        if not url:
            raise ValueError("已启用通知，但没有配置飞书 Webhook URL")
        return url

    def get_enabled_strategy_keys(self) -> set[str]:
        """返回经过清理的策略标识集合。"""
        return {key.strip().lower() for key in self.enabled_strategies.split(",") if key.strip()}


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
