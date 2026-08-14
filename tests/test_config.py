from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import (
    PROJECT_ROOT,
    Settings,
    get_settings,
    load_news_config,
    load_rules_config,
)


def _valid_rules() -> str:
    return """
watchlist:
  AAPL:
    name: Apple
    market: US
    currency: USD
    cost_basis: 100.0
    sell_rules:
      - id: aapl-upper
        type: price_above
        value: 150.0
    buy_rules: []
"""


def test_repository_configs_are_root_relative_and_disabled(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    rules = load_rules_config()
    news = load_news_config()

    assert rules.watchlist
    assert all(not item.enabled for item in rules.watchlist.values())
    assert news.ai_filter.enabled is False
    assert news.sources.finnhub.enabled is False
    assert news.sources.newsapi.enabled is False
    assert news.sources.akshare.enabled is False
    assert rules.reliability.freshness.fields["price"].session_aware is True
    assert (
        rules.reliability.freshness.fields["price"].allow_observed_only is False
    )
    assert rules.reliability.provider.max_attempts == 3
    assert rules.reliability.provider.bulkhead_max_calls == 4


@pytest.mark.parametrize(
    "replacement, expected_path",
    [
        ("market: US", "watchlist.AAPL.market"),
        ("type: price_above", "watchlist.AAPL.sell_rules.0.type"),
    ],
)
def test_unknown_market_and_rule_fail_with_readable_paths(
    tmp_path, replacement, expected_path
):
    raw = _valid_rules()
    if replacement == "market: US":
        raw = raw.replace(replacement, "market: EU")
    else:
        raw = raw.replace(replacement, "type: execute_order")
    path = tmp_path / "rules.yaml"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ValidationError) as exc_info:
        load_rules_config(path)

    assert expected_path in str(exc_info.value)


def test_unknown_fields_fail_fast_at_nested_path(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        _valid_rules().replace(
            "    cost_basis", "    typo_field: true\n    cost_basis"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="typo_field"):
        load_rules_config(path)


@pytest.mark.parametrize("bad_value", [".nan", ".inf", "-.inf"])
def test_non_finite_rule_threshold_is_rejected(tmp_path, bad_value):
    path = tmp_path / "rules.yaml"
    path.write_text(_valid_rules().replace("150.0", bad_value), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_rules_config(path)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (("cost_basis: 100.0", "cost_basis: 0.0"), "cost_basis"),
        (("currency: USD", "currency: HKD"), "market US requires currency USD"),
    ],
)
def test_cost_basis_and_market_currency_must_be_valid(tmp_path, mutation, expected):
    path = tmp_path / "rules.yaml"
    path.write_text(_valid_rules().replace(*mutation), encoding="utf-8")

    with pytest.raises(ValidationError, match=expected):
        load_rules_config(path)


def test_rule_ids_are_required_and_unique(tmp_path):
    duplicate = _valid_rules().replace(
        "    buy_rules: []",
        """    buy_rules:
      - id: aapl-upper
        type: price_below
        value: 90.0""",
    )
    path = tmp_path / "rules.yaml"
    path.write_text(duplicate, encoding="utf-8")

    with pytest.raises(ValidationError, match="rule ids must be unique"):
        load_rules_config(path)


def test_price_drop_rule_requires_positive_cost_basis(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        _valid_rules()
        .replace("    cost_basis: 100.0\n", "")
        .replace("type: price_above", "type: price_drop_pct")
        .replace("150.0", "10.0"),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="cost_basis"):
        load_rules_config(path)


def test_instrument_safe_defaults_and_cooldown(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(_valid_rules(), encoding="utf-8")

    instrument = load_rules_config(path).watchlist["AAPL"]

    assert instrument.enabled is False
    assert instrument.alert_cooldown_hours == 24.0


def test_news_schema_rejects_unknown_nested_field(tmp_path):
    path = tmp_path / "news.yaml"
    path.write_text(
        """
stock_keywords: {}
macro_topics: []
ai_filter:
  enabled: false
  alert_threshold: 3
  max_ai_calls_per_scan: 2
  typo: true
sources: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="ai_filter.typo"):
        load_news_config(path)


def test_settings_default_to_no_real_notifications(monkeypatch):
    for key in (
        "NOTIFICATIONS_ENABLED",
        "TELEGRAM_ENABLED",
        "ALPHA_GUARD_NOTIFICATIONS_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = get_settings(None)

    assert settings.notifications_enabled is False
    assert settings.heartbeat_enabled is False
    assert settings.heartbeat_url is None
    assert settings.heartbeat_timeout_seconds == 5


def test_settings_are_loaded_centrally_from_explicit_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_path = tmp_path / "alpha.env"
    env_path.write_text(
        """NOTIFICATIONS_ENABLED=true
TELEGRAM_BOT_TOKEN=token-from-env
TELEGRAM_CHAT_ID=12345
ANTHROPIC_MODEL=test-model
""",
        encoding="utf-8",
    )

    settings = get_settings(env_path)

    assert settings.notifications_enabled is True
    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.get_secret_value() == "token-from-env"
    assert settings.telegram_chat_id == "12345"
    assert settings.anthropic_model == "test-model"


def test_relative_env_path_resolves_from_project_root():
    assert PROJECT_ROOT.is_absolute()
    # The public example is not loaded as runtime settings; this simply asserts
    # that a root-relative missing .env is a valid, safe settings source.
    settings = get_settings(".env-does-not-exist")
    assert isinstance(settings, Settings)


def test_reliability_config_rejects_out_of_bounds_provider_controls(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        """reliability:
  provider:
    max_attempts: 0
"""
        + _valid_rules(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_rules_config(path)

    assert "reliability.provider.max_attempts" in str(exc_info.value)


def test_reliability_config_requires_every_supported_field_policy(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        """reliability:
  freshness:
    fields:
      price:
        max_source_age_seconds: 60.0
        max_observation_age_seconds: 60.0
"""
        + _valid_rules(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="missing fields"):
        load_rules_config(path)


def test_heartbeat_settings_are_opt_in_secret_and_bounded(tmp_path):
    env_path = tmp_path / "heartbeat.env"
    env_path.write_text(
        """HEARTBEAT_ENABLED=true
HEARTBEAT_URL=https://heartbeat.example/secret-path
HEARTBEAT_TIMEOUT_SECONDS=30
""",
        encoding="utf-8",
    )

    settings = get_settings(env_path)

    assert settings.heartbeat_enabled is True
    assert settings.heartbeat_url is not None
    assert settings.heartbeat_url.get_secret_value().startswith("https://")
    assert settings.heartbeat_timeout_seconds == 30

    env_path.write_text("HEARTBEAT_TIMEOUT_SECONDS=31\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_settings(env_path)


@pytest.mark.parametrize(
    "heartbeat_url",
    [
        None,
        "ftp://watcher.example/private-token",
        "https://user:private-token@watcher.example/ping",
        "https://watcher.example/ping#private-token",
        "https://watcher.example:99999/private-token",
    ],
)
def test_enabled_heartbeat_requires_safe_http_endpoint(heartbeat_url):
    with pytest.raises(
        ValidationError, match="enabled heartbeat endpoint configuration is invalid"
    ):
        Settings(heartbeat_enabled=True, heartbeat_url=heartbeat_url)
