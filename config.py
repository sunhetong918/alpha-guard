"""Strict, root-relative configuration for Alpha Guard.

This module is the only place that reads process settings or ``.env`` files.
Domain modules receive :class:`Settings` explicitly when deterministic tests or
multiple runtime profiles are needed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent
ROOT_DIR = PROJECT_ROOT
RULES_CONFIG_PATH = PROJECT_ROOT / "signals" / "rules.yaml"
NEWS_CONFIG_PATH = PROJECT_ROOT / "news" / "config.yaml"
TRADING_CONFIG_PATH = PROJECT_ROOT / "trading" / "futu.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
MOBILE_TRUST_PROOF_MAX_AGE_SECONDS = 24 * 60 * 60

RuleType = Literal[
    "price_above",
    "price_below",
    "pe_above",
    "pe_below",
    "roe_above",
    "price_drop_pct",
]
Market = Literal["US", "HK"]
Currency = Literal["USD", "HKD"]
FreshnessField = Literal["price", "pe_ttm", "pb", "roe"]

StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
Keyword = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
WhatsAppTemplateName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=512,
        pattern=r"^[a-z0-9_]+$",
    ),
]


class StrictModel(BaseModel):
    """Base class for immutable fail-fast YAML models."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        str_strip_whitespace=True,
    )


class RuleConfig(StrictModel):
    """One deterministic threshold rule with a durable audit identifier."""

    id: StableId
    type: RuleType
    value: float = Field(allow_inf_nan=False)
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def validate_threshold(self) -> Self:
        value = self.value
        if not math.isfinite(value):
            raise ValueError("rule threshold must be finite")
        if (
            self.type
            in {
                "price_above",
                "price_below",
                "pe_above",
                "pe_below",
            }
            and value <= 0
        ):
            raise ValueError(f"{self.type} threshold must be greater than zero")
        if self.type == "price_drop_pct" and not 0 < value <= 100:
            raise ValueError("price_drop_pct threshold must be in (0, 100]")
        return self

    @property
    def threshold(self) -> float:
        """Semantic alias used by evidence renderers."""

        return self.value


# Concise public alias retained for callers that prefer the domain name.
Rule = RuleConfig


class InstrumentConfig(StrictModel):
    """Configuration for a single canonical watchlist instrument."""

    name: NonEmptyText
    market: Market
    currency: Currency
    enabled: bool = False
    cost_basis: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    alert_cooldown_hours: float = Field(
        default=24.0, ge=0, le=8_760, allow_inf_nan=False
    )
    sell_rules: list[RuleConfig] = Field(default_factory=list)
    buy_rules: list[RuleConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_instrument(self) -> Self:
        expected_currency = {"US": "USD", "HK": "HKD"}[self.market]
        if self.currency != expected_currency:
            raise ValueError(
                f"market {self.market} requires currency {expected_currency}"
            )

        rules = self.sell_rules + self.buy_rules
        rule_ids = [rule.id for rule in rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rule ids must be unique within an instrument")
        if (
            any(rule.type == "price_drop_pct" for rule in rules)
            and self.cost_basis is None
        ):
            raise ValueError("price_drop_pct requires a positive cost_basis")
        return self


class FreshnessFieldConfig(StrictModel):
    """Two-clock freshness budget for one normalized market-data field."""

    max_source_age_seconds: float | None = Field(
        default=None, gt=0, le=31_536_000, allow_inf_nan=False
    )
    max_observation_age_seconds: float = Field(
        default=86_400, gt=0, le=31_536_000, allow_inf_nan=False
    )
    aging_ratio: float = Field(default=0.8, gt=0, lt=1, allow_inf_nan=False)
    allow_observed_only: bool = False
    session_aware: bool = False

    @model_validator(mode="after")
    def validate_time_basis_policy(self) -> Self:
        if not self.allow_observed_only and self.max_source_age_seconds is None:
            raise ValueError(
                "max_source_age_seconds is required when observed-only data is denied"
            )
        return self


def _default_freshness_fields() -> dict[FreshnessField, FreshnessFieldConfig]:
    return {
        # During a live session quotes must be no more than 30 minutes old.  The
        # exchange-session watermark handles weekends and closed-market periods.
        "price": FreshnessFieldConfig(
            max_source_age_seconds=1_800,
            max_observation_age_seconds=900,
            aging_ratio=0.8,
            allow_observed_only=False,
            session_aware=True,
        ),
        # Free fundamentals do not expose a reliable event timestamp.  They are
        # explicitly lower-confidence observations, never fabricated event time.
        "pe_ttm": FreshnessFieldConfig(
            max_source_age_seconds=None,
            max_observation_age_seconds=86_400,
            aging_ratio=0.8,
            allow_observed_only=True,
            session_aware=False,
        ),
        "pb": FreshnessFieldConfig(
            max_source_age_seconds=None,
            max_observation_age_seconds=86_400,
            aging_ratio=0.8,
            allow_observed_only=True,
            session_aware=False,
        ),
        "roe": FreshnessFieldConfig(
            max_source_age_seconds=None,
            max_observation_age_seconds=86_400,
            aging_ratio=0.8,
            allow_observed_only=True,
            session_aware=False,
        ),
    }


class FreshnessConfig(StrictModel):
    future_tolerance_seconds: float = Field(
        default=300, ge=0, le=3_600, allow_inf_nan=False
    )
    fields: dict[FreshnessField, FreshnessFieldConfig] = Field(
        default_factory=_default_freshness_fields
    )

    @model_validator(mode="after")
    def require_supported_field_policies(self) -> Self:
        required = {"price", "pe_ttm", "pb", "roe"}
        missing = required.difference(self.fields)
        if missing:
            raise ValueError(
                "freshness policy is missing fields: " + ", ".join(sorted(missing))
            )
        return self


class ProviderReliabilityConfig(StrictModel):
    request_timeout_seconds: float = Field(default=10, gt=0, le=120)
    bulkhead_max_calls: int = Field(default=4, ge=1, le=64)
    max_attempts: int = Field(default=3, ge=1, le=5)
    base_backoff_seconds: float = Field(default=0.25, ge=0, le=10)
    max_backoff_seconds: float = Field(default=4, gt=0, le=60)
    max_retry_after_seconds: float = Field(default=60, gt=0, le=300)
    failure_threshold: int = Field(default=3, ge=1, le=20)
    open_seconds: float = Field(default=300, gt=0, le=86_400)
    half_open_max_calls: int = Field(default=1, ge=1, le=10)
    fresh_cache_seconds: float = Field(default=300, ge=0, le=86_400)
    stale_if_error_seconds: float = Field(default=86_400, ge=0, le=604_800)
    observation_limit: int = Field(default=100, ge=10, le=10_000)

    @model_validator(mode="after")
    def validate_provider_ranges(self) -> Self:
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= base_backoff_seconds")
        if self.stale_if_error_seconds < self.fresh_cache_seconds:
            raise ValueError(
                "stale_if_error_seconds must be >= fresh_cache_seconds"
            )
        return self


class ReliabilityConfig(StrictModel):
    freshness: FreshnessConfig = Field(default_factory=FreshnessConfig)
    provider: ProviderReliabilityConfig = Field(
        default_factory=ProviderReliabilityConfig
    )


class RulesConfig(StrictModel):
    """Validated rules snapshot."""

    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    watchlist: dict[StableId, InstrumentConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_global_rule_ids(self) -> Self:
        owner_by_id: dict[str, str] = {}
        for ticker, instrument in self.watchlist.items():
            for rule in instrument.sell_rules + instrument.buy_rules:
                previous_owner = owner_by_id.setdefault(rule.id, ticker)
                if previous_owner != ticker:
                    raise ValueError(
                        f"rule id {rule.id!r} is reused by {previous_owner} and {ticker}"
                    )
        return self


class KeywordGroups(StrictModel):
    en: list[Keyword] = Field(default_factory=list)
    zh: list[Keyword] = Field(default_factory=list)


class MacroTopicConfig(StrictModel):
    label: NonEmptyText
    keywords: list[Keyword] = Field(min_length=1)


class AIFilterConfig(StrictModel):
    enabled: bool = False
    alert_threshold: int = Field(default=3, ge=1, le=5)
    max_ai_calls_per_scan: int = Field(default=20, ge=0, le=1_000)


class FinnhubSourceConfig(StrictModel):
    enabled: bool = False
    lookback_hours: int = Field(default=6, ge=1, le=168)


class NewsAPISourceConfig(StrictModel):
    enabled: bool = False
    lookback_hours: int = Field(default=12, ge=1, le=168)
    extra_queries: list[Keyword] = Field(default_factory=list)
    language: Literal[
        "ar",
        "de",
        "en",
        "es",
        "fr",
        "he",
        "it",
        "nl",
        "no",
        "pt",
        "ru",
        "sv",
        "ud",
        "zh",
    ] = "en"
    page_size: int = Field(default=20, ge=1, le=100)


class AkshareSourceConfig(StrictModel):
    enabled: bool = False


class NewsSourcesConfig(StrictModel):
    finnhub: FinnhubSourceConfig = Field(default_factory=FinnhubSourceConfig)
    newsapi: NewsAPISourceConfig = Field(default_factory=NewsAPISourceConfig)
    akshare: AkshareSourceConfig = Field(default_factory=AkshareSourceConfig)


class NewsConfig(StrictModel):
    stock_keywords: dict[StableId, KeywordGroups] = Field(default_factory=dict)
    macro_topics: list[MacroTopicConfig] = Field(default_factory=list)
    ai_filter: AIFilterConfig = Field(default_factory=AIFilterConfig)
    sources: NewsSourcesConfig = Field(default_factory=NewsSourcesConfig)

    @model_validator(mode="after")
    def validate_unique_topics(self) -> Self:
        labels = [topic.label.casefold() for topic in self.macro_topics]
        if len(labels) != len(set(labels)):
            raise ValueError("macro topic labels must be unique")
        return self


class AutoTradeInstrumentConfig(StrictModel):
    """Per-instrument policy used only by the offline order rehearsal."""

    side: Literal["buy", "sell"]
    quantity: int = Field(ge=1, le=100_000)
    order_type: Literal["limit"] = "limit"
    limit_offset_pct: float = Field(
        default=0.5, ge=0, le=10, allow_inf_nan=False
    )
    max_orders_per_day: int = Field(default=1, ge=1, le=50)
    cooldown_minutes: int = Field(default=60, ge=1, le=10_080)


class FutuTradingConfig(StrictModel):
    """Offline order-intent rehearsal; live brokerage is not supported."""

    enabled: bool = False
    mode: Literal["dry"] = "dry"
    auto_trade: dict[StableId, AutoTradeInstrumentConfig] = Field(
        default_factory=dict
    )


class Settings(BaseSettings):
    """Runtime secrets and feature switches loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        strict=True,
        case_sensitive=False,
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    notifications_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "NOTIFICATIONS_ENABLED",
            "TELEGRAM_ENABLED",
            "ALPHA_GUARD_NOTIFICATIONS_ENABLED",
        ),
    )
    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_chat_id: str | None = Field(
        default=None, validation_alias="TELEGRAM_CHAT_ID", max_length=128
    )
    whatsapp_enabled: bool = Field(
        default=False, validation_alias="WHATSAPP_ENABLED"
    )
    whatsapp_access_token: SecretStr | None = Field(
        default=None,
        validation_alias="WHATSAPP_ACCESS_TOKEN",
        max_length=4_096,
    )
    whatsapp_phone_number_id: str | None = Field(
        default=None,
        validation_alias="WHATSAPP_PHONE_NUMBER_ID",
        max_length=32,
    )
    whatsapp_default_to: str | None = Field(
        default=None,
        validation_alias="WHATSAPP_DEFAULT_TO",
        max_length=16,
    )
    whatsapp_graph_api_version: str = Field(
        default="v26.0",
        validation_alias="WHATSAPP_GRAPH_API_VERSION",
        pattern=r"^v[1-9][0-9]?\.0$",
        max_length=6,
    )
    whatsapp_timeout_seconds: float = Field(
        default=10.0,
        validation_alias="WHATSAPP_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )
    whatsapp_template_language_code: str = Field(
        default="zh_CN",
        validation_alias="WHATSAPP_TEMPLATE_LANGUAGE_CODE",
        pattern=r"^[a-z]{2,3}(?:_[A-Z]{2})?$",
        max_length=6,
    )
    whatsapp_signal_template_name: WhatsAppTemplateName | None = Field(
        default=None, validation_alias="WHATSAPP_SIGNAL_TEMPLATE_NAME"
    )
    whatsapp_incident_template_name: WhatsAppTemplateName | None = Field(
        default=None, validation_alias="WHATSAPP_INCIDENT_TEMPLATE_NAME"
    )
    whatsapp_news_template_name: WhatsAppTemplateName | None = Field(
        default=None, validation_alias="WHATSAPP_NEWS_TEMPLATE_NAME"
    )
    whatsapp_trust_template_name: WhatsAppTemplateName | None = Field(
        default=None, validation_alias="WHATSAPP_TRUST_TEMPLATE_NAME"
    )
    finnhub_api_key: SecretStr | None = Field(
        default=None, validation_alias="FINNHUB_API_KEY"
    )
    newsapi_api_key: SecretStr | None = Field(
        default=None, validation_alias="NEWSAPI_API_KEY"
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias="ANTHROPIC_API_KEY"
    )
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514",
        validation_alias="ANTHROPIC_MODEL",
        min_length=1,
        max_length=160,
    )
    http_timeout_seconds: float = Field(
        default=10.0,
        validation_alias="HTTP_TIMEOUT_SECONDS",
        gt=0,
        le=120,
        allow_inf_nan=False,
    )
    heartbeat_enabled: bool = Field(
        default=False, validation_alias="HEARTBEAT_ENABLED"
    )
    heartbeat_url: SecretStr | None = Field(
        default=None, validation_alias="HEARTBEAT_URL"
    )
    heartbeat_timeout_seconds: float = Field(
        default=5.0,
        validation_alias="HEARTBEAT_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )
    futu_enabled: bool = Field(default=False, validation_alias="FUTU_ENABLED")
    futu_opend_host: str = Field(
        default="127.0.0.1",
        validation_alias="FUTU_OPEND_HOST",
        min_length=1,
        max_length=253,
    )
    futu_opend_quote_port: int = Field(
        default=11111,
        validation_alias="FUTU_OPEND_QUOTE_PORT",
        ge=1,
        le=65_535,
    )
    @field_validator("futu_opend_host", mode="after")
    @classmethod
    def futu_opend_must_be_local(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost"}:
            raise ValueError(
                "Futu OpenD host must use 127.0.0.1 or localhost"
            )
        return value

    @field_validator(
        "telegram_bot_token",
        "whatsapp_access_token",
        "finnhub_api_key",
        "newsapi_api_key",
        "anthropic_api_key",
        "heartbeat_url",
        mode="before",
    )
    @classmethod
    def blank_secret_is_missing(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "telegram_chat_id",
        "whatsapp_phone_number_id",
        "whatsapp_default_to",
        "whatsapp_signal_template_name",
        "whatsapp_incident_template_name",
        "whatsapp_news_template_name",
        "whatsapp_trust_template_name",
        mode="before",
    )
    @classmethod
    def blank_chat_id_is_missing(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def validate_enabled_heartbeat_endpoint(self) -> Self:
        if not self.heartbeat_enabled:
            return self
        if self.heartbeat_url is None:
            raise ValueError("enabled heartbeat endpoint configuration is invalid")
        raw = self.heartbeat_url.get_secret_value()
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError:
            raise ValueError(
                "enabled heartbeat endpoint configuration is invalid"
            ) from None
        if (
            raw != raw.strip()
            or any(character.isspace() for character in raw)
            or parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65_535)
        ):
            raise ValueError("enabled heartbeat endpoint configuration is invalid")
        return self

    @model_validator(mode="after")
    def validate_enabled_whatsapp(self) -> Self:
        """Fail closed without ever reflecting credentials in an error."""

        if not self.whatsapp_enabled:
            return self

        missing: list[str] = []
        token = (
            self.whatsapp_access_token.get_secret_value()
            if self.whatsapp_access_token is not None
            else ""
        )
        if not token or token != token.strip() or any(char.isspace() for char in token):
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not self.whatsapp_phone_number_id or not self.whatsapp_phone_number_id.isdigit():
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        recipient = self.whatsapp_default_to or ""
        recipient_digits = recipient[1:] if recipient.startswith("+") else recipient
        if (
            not recipient_digits.isdigit()
            or not 7 <= len(recipient_digits) <= 15
            or recipient_digits.startswith("0")
        ):
            missing.append("WHATSAPP_DEFAULT_TO")
        required_templates = {
            "WHATSAPP_SIGNAL_TEMPLATE_NAME": self.whatsapp_signal_template_name,
            "WHATSAPP_INCIDENT_TEMPLATE_NAME": self.whatsapp_incident_template_name,
            "WHATSAPP_TRUST_TEMPLATE_NAME": self.whatsapp_trust_template_name,
        }
        missing.extend(
            name for name, value in required_templates.items() if value is None
        )
        if missing:
            raise ValueError(
                "enabled WhatsApp configuration is invalid or missing: "
                + ", ".join(missing)
            )
        return self


def get_settings(env_file: str | Path | None = ENV_PATH) -> Settings:
    """Load runtime settings centrally.

    ``None`` disables dotenv loading, which is useful for hermetic tests. A
    relative dotenv path is resolved from the project root rather than the
    caller's current working directory.
    """

    resolved_env: Path | None
    if env_file is None:
        resolved_env = None
    else:
        resolved_env = _root_relative_path(env_file)
    # BaseSettings accepts _env_file at runtime; its generated static signature
    # intentionally contains only declared environment-backed fields.
    return Settings(_env_file=resolved_env)  # type: ignore[call-arg]


def load_rules_config(path: str | Path | None = None) -> RulesConfig:
    """Load and validate a rules YAML file relative to the repository root."""

    data = _load_yaml(path or RULES_CONFIG_PATH)
    return RulesConfig.model_validate(data)


def load_news_config(path: str | Path | None = None) -> NewsConfig:
    """Load and validate a news YAML file relative to the repository root."""

    data = _load_yaml(path or NEWS_CONFIG_PATH)
    return NewsConfig.model_validate(data)


def load_futu_config(path: str | Path | None = None) -> FutuTradingConfig:
    """Load the offline order-rehearsal YAML relative to the repo root."""

    data = _load_yaml(path or TRADING_CONFIG_PATH)
    return FutuTradingConfig.model_validate(data)


def _root_relative_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def _load_yaml(path: str | Path) -> Mapping[str, Any]:
    resolved = _root_relative_path(path)
    with resolved.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise TypeError(f"configuration root must be a mapping: {resolved}")
    return data


__all__ = [
    "ENV_PATH",
    "NEWS_CONFIG_PATH",
    "PROJECT_ROOT",
    "ROOT_DIR",
    "RULES_CONFIG_PATH",
    "AIFilterConfig",
    "AkshareSourceConfig",
    "Currency",
    "FinnhubSourceConfig",
    "FreshnessConfig",
    "FreshnessField",
    "FreshnessFieldConfig",
    "AutoTradeInstrumentConfig",
    "FutuTradingConfig",
    "TRADING_CONFIG_PATH",
    "InstrumentConfig",
    "KeywordGroups",
    "MacroTopicConfig",
    "Market",
    "NewsAPISourceConfig",
    "NewsConfig",
    "NewsSourcesConfig",
    "ProviderReliabilityConfig",
    "ReliabilityConfig",
    "Rule",
    "RuleConfig",
    "RuleType",
    "RulesConfig",
    "Settings",
    "WhatsAppTemplateName",
    "get_settings",
    "load_futu_config",
    "load_news_config",
    "load_rules_config",
]
