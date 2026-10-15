"""配置管理属性测试。"""

from hypothesis import HealthCheck, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st


# Feature: sequoia-x-v2, Property 1: 环境变量覆盖配置默认值
@given(
    db_path=st.text(
        min_size=1,
        max_size=100,
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd"),
            whitelist_characters="/_.-",
        ),
    )
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_env_overrides_default(db_path: str, monkeypatch) -> None:
    """属性 1：任意合法 db_path 通过环境变量设置后，Settings 实例应反映该值。"""
    import sequoia_x.core.config as cfg_module

    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(cfg_module, "_settings", None)
    from sequoia_x.core.config import Settings

    s = Settings()
    assert s.db_path == db_path


def test_local_mode_does_not_require_webhook() -> None:
    """默认本地模式不需要飞书配置。"""
    from sequoia_x.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.notifications_enabled is False
    assert settings.feishu_webhook_url is None
    assert settings.get_enabled_strategy_keys() == {"ma_volume", "turtle", "flag", "rps"}
