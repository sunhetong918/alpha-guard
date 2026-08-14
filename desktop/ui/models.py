"""Typed, redacted DTOs shared by the Qt UI and Guardian adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class PayloadError(ValueError):
    """A low-cardinality payload error safe to surface in the UI."""


class StatusColor(StrEnum):
    GRAY = "GRAY"
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"
    BLUE = "BLUE"


class GuardianState(StrEnum):
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


class IncidentState(StrEnum):
    OPEN = "OPEN"
    RECOVERING = "RECOVERING"
    RESOLVED = "RESOLVED"


class Decision(StrEnum):
    NONE = "NONE"
    BUY_REVIEW = "BUY_REVIEW"
    SELL_REVIEW = "SELL_REVIEW"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class ChannelKind(StrEnum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    EXTERNAL_WATCHER = "external_watcher"


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError(f"invalid_payload:{field}:object")
    return value


def _sequence(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise PayloadError(f"invalid_payload:{field}:array")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"invalid_payload:{field}:string")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PayloadError(f"invalid_payload:{field}:integer")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadError(f"invalid_payload:{field}:number")
    return float(value)


def _optional_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    return _number(value, field)


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise PayloadError(f"invalid_payload:{field}:boolean")
    return value


def _timestamp(value: object, field: str) -> datetime:
    raw = _string(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise PayloadError(f"invalid_payload:{field}:timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PayloadError(f"invalid_payload:{field}:timestamp_offset")
    return parsed


def _optional_timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value, field)


def _enum(enum_type: type[StrEnum], value: object, field: str) -> Any:
    raw = _string(value, field)
    try:
        return enum_type(raw)
    except ValueError:
        raise PayloadError(f"invalid_payload:{field}:enum") from None


def _strings(value: object, field: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{field}[]") for item in _sequence(value, field)
    )


@dataclass(frozen=True, slots=True)
class GuardianHealth:
    state: GuardianState
    color: StatusColor
    version: str
    pid: int | None
    started_at: datetime | None
    last_heartbeat_at: datetime | None
    launch_at_login: bool
    source: str

    @classmethod
    def from_payload(cls, payload: object) -> GuardianHealth:
        root = _mapping(payload, "health")
        data = _mapping(root.get("guardian", root), "health.guardian")
        return cls(
            state=_enum(
                GuardianState, data.get("state"), "health.guardian.state"
            ),
            color=_enum(
                StatusColor, data.get("color"), "health.guardian.color"
            ),
            version=_string(
                data.get("version", "unknown"), "health.guardian.version"
            ),
            pid=_optional_integer(data.get("pid"), "health.guardian.pid"),
            started_at=_optional_timestamp(
                data.get("started_at"), "health.guardian.started_at"
            ),
            last_heartbeat_at=_optional_timestamp(
                data.get("last_heartbeat_at"),
                "health.guardian.last_heartbeat_at",
            ),
            launch_at_login=_boolean(
                data.get("launch_at_login", False),
                "health.guardian.launch_at_login",
            ),
            source=_string(root.get("source", "guardian"), "health.source"),
        )


@dataclass(frozen=True, slots=True)
class Coverage:
    enabled: int
    usable: int | None
    ratio: float | None
    affected: tuple[str, ...]
    known: bool

    @classmethod
    def from_payload(cls, payload: object, field: str) -> Coverage:
        data = _mapping(payload, field)
        ratio = _optional_number(data.get("ratio"), f"{field}.ratio")
        if ratio is not None and not 0 <= ratio <= 1:
            raise PayloadError(f"invalid_payload:{field}.ratio:range")
        return cls(
            enabled=_integer(data.get("enabled", 0), f"{field}.enabled"),
            usable=_optional_integer(data.get("usable"), f"{field}.usable"),
            ratio=ratio,
            affected=_strings(data.get("affected", []), f"{field}.affected"),
            known=_boolean(data.get("known", False), f"{field}.known"),
        )


@dataclass(frozen=True, slots=True)
class SloWindow:
    target: float | None
    good: int
    bad: int
    missing: int
    pending: int
    expected: int
    violations: int
    ratio: float | None
    burn_rate: float | None

    @classmethod
    def from_payload(cls, payload: object, field: str) -> SloWindow:
        data = _mapping(payload, field)
        return cls(
            target=_optional_number(data.get("target"), f"{field}.target"),
            good=_integer(data.get("good", 0), f"{field}.good"),
            bad=_integer(data.get("bad", 0), f"{field}.bad"),
            missing=_integer(data.get("missing", 0), f"{field}.missing"),
            pending=_integer(data.get("pending", 0), f"{field}.pending"),
            expected=_integer(data.get("expected", 0), f"{field}.expected"),
            violations=_integer(
                data.get("violations", 0), f"{field}.violations"
            ),
            ratio=_optional_number(data.get("ratio"), f"{field}.ratio"),
            burn_rate=_optional_number(
                data.get("burn_rate"), f"{field}.burn_rate"
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketSchedule:
    market: str
    expected_at: datetime
    deadline_at: datetime
    deadline_state: str
    slo_30d: SloWindow

    @classmethod
    def from_payload(cls, payload: object, index: int) -> MarketSchedule:
        field = f"cockpit.schedule.markets[{index}]"
        data = _mapping(payload, field)
        return cls(
            market=_string(data.get("market"), f"{field}.market"),
            expected_at=_timestamp(
                data.get("expected_at"), f"{field}.expected_at"
            ),
            deadline_at=_timestamp(
                data.get("deadline_at"), f"{field}.deadline_at"
            ),
            deadline_state=_string(
                data.get("deadline_state"), f"{field}.deadline_state"
            ),
            slo_30d=SloWindow.from_payload(
                data.get("slo_30d", {}), f"{field}.slo_30d"
            ),
        )


@dataclass(frozen=True, slots=True)
class DeliveryChannel:
    kind: ChannelKind
    configured: bool
    mode: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    success: bool | None
    error_code: str | None
    label: str
    recipient_hint: str

    @property
    def color(self) -> StatusColor:
        if self.mode == "PREVIEW" or not self.configured:
            return StatusColor.GRAY
        if self.error_code is not None or self.success is False:
            return StatusColor.RED
        if self.last_success_at is None:
            return StatusColor.BLUE
        return StatusColor.GREEN

    @classmethod
    def from_payload(
        cls,
        kind: ChannelKind,
        payload: object,
        *,
        settings: object | None = None,
    ) -> DeliveryChannel:
        field = f"cockpit.delivery.{kind.value}"
        data = _mapping(payload, field)
        public = _mapping(settings or {}, f"config.channels.{kind.value}")
        success = data.get("success")
        if success is not None and not isinstance(success, bool):
            raise PayloadError(f"invalid_payload:{field}.success:boolean")
        defaults = {
            ChannelKind.TELEGRAM: "Telegram Bot",
            ChannelKind.WHATSAPP: "WhatsApp Cloud API",
            ChannelKind.EXTERNAL_WATCHER: "外部 Guardian Watcher",
        }
        return cls(
            kind=kind,
            configured=_boolean(
                data.get("configured", False), f"{field}.configured"
            ),
            mode=_string(data.get("mode", "PREVIEW"), f"{field}.mode"),
            last_attempt_at=_optional_timestamp(
                data.get("last_attempt_at"), f"{field}.last_attempt_at"
            ),
            last_success_at=_optional_timestamp(
                data.get("last_success_at"), f"{field}.last_success_at"
            ),
            success=success,
            error_code=_optional_string(
                data.get("error_code"), f"{field}.error_code"
            ),
            label=_string(
                public.get("label", defaults[kind]),
                f"config.channels.{kind.value}.label",
            ),
            recipient_hint=_string(
                public.get("recipient_hint") or "未公开",
                f"config.channels.{kind.value}.recipient_hint",
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    provider: str
    operation: str
    market: str
    sample_count: int
    success_rate: float | None
    wilson_lower_bound: float | None
    grade: str
    circuit_state: str
    reasons: tuple[str, ...]

    @property
    def color(self) -> StatusColor:
        if self.circuit_state == "open" or self.grade == "unreliable":
            return StatusColor.RED
        if self.circuit_state == "half_open":
            return StatusColor.BLUE
        if self.grade == "degraded":
            return StatusColor.AMBER
        if self.grade == "insufficient_data":
            return StatusColor.GRAY
        return StatusColor.GREEN

    @classmethod
    def from_payload(cls, payload: object, index: int) -> ProviderCapability:
        field = f"providers.capabilities[{index}]"
        data = _mapping(payload, field)
        return cls(
            provider=_string(data.get("provider"), f"{field}.provider"),
            operation=_string(data.get("operation"), f"{field}.operation"),
            market=_string(data.get("market"), f"{field}.market"),
            sample_count=_integer(
                data.get("sample_count", 0), f"{field}.sample_count"
            ),
            success_rate=_optional_number(
                data.get("success_rate"), f"{field}.success_rate"
            ),
            wilson_lower_bound=_optional_number(
                data.get("wilson_lower_bound"),
                f"{field}.wilson_lower_bound",
            ),
            grade=_string(data.get("grade"), f"{field}.grade"),
            circuit_state=_string(
                data.get("circuit_state"), f"{field}.circuit_state"
            ),
            reasons=_strings(data.get("reasons", []), f"{field}.reasons"),
        )


@dataclass(frozen=True, slots=True)
class AssetStatus:
    symbol: str
    name: str
    market: str
    color: StatusColor
    enabled: bool
    evidence_coverage: float | None
    decision: Decision
    reason: str
    observed_at: datetime | None
    next_scan_at: datetime | None

    @classmethod
    def from_payload(cls, payload: object, index: int) -> AssetStatus:
        field = f"config.assets[{index}]"
        data = _mapping(payload, field)
        coverage = _optional_number(
            data.get("evidence_coverage"), f"{field}.evidence_coverage"
        )
        if coverage is not None and not 0 <= coverage <= 1:
            raise PayloadError(f"invalid_payload:{field}.evidence_coverage:range")
        return cls(
            symbol=_string(data.get("symbol"), f"{field}.symbol"),
            name=_string(data.get("name", data.get("symbol")), f"{field}.name"),
            market=_string(data.get("market"), f"{field}.market"),
            color=_enum(
                StatusColor,
                data.get("color", data.get("status", "GRAY")),
                f"{field}.color",
            ),
            enabled=_boolean(data.get("enabled", True), f"{field}.enabled"),
            evidence_coverage=coverage,
            decision=_enum(
                Decision, data.get("decision", "UNKNOWN"), f"{field}.decision"
            ),
            reason=_string(
                data.get("reason", "暂无新鲜评估证据"), f"{field}.reason"
            ),
            observed_at=_optional_timestamp(
                data.get("observed_at"), f"{field}.observed_at"
            ),
            next_scan_at=_optional_timestamp(
                data.get("next_scan_at"), f"{field}.next_scan_at"
            ),
        )


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str
    color: StatusColor
    state: IncidentState
    title: str
    scope: str
    opened_at: datetime
    updated_at: datetime
    summary: str
    reason_codes: tuple[str, ...]
    next_actions: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: object, index: int) -> Incident:
        field = f"incidents.items[{index}]"
        data = _mapping(payload, field)
        return cls(
            incident_id=_string(
                data.get("id", data.get("incident_id")), f"{field}.id"
            ),
            color=_enum(StatusColor, data.get("color"), f"{field}.color"),
            state=_enum(
                IncidentState,
                data.get("status", data.get("state")),
                f"{field}.state",
            ),
            title=_string(data.get("title"), f"{field}.title"),
            scope=_string(data.get("scope"), f"{field}.scope"),
            opened_at=_timestamp(data.get("opened_at"), f"{field}.opened_at"),
            updated_at=_timestamp(
                data.get("updated_at"), f"{field}.updated_at"
            ),
            summary=_string(data.get("summary"), f"{field}.summary"),
            reason_codes=_strings(
                data.get("reason_codes", []), f"{field}.reason_codes"
            ),
            next_actions=_strings(
                data.get("next_actions", []), f"{field}.next_actions"
            ),
        )


@dataclass(frozen=True, slots=True)
class Preferences:
    timezone: str
    language: str
    launch_at_login: bool
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str

    @classmethod
    def from_payload(cls, payload: object) -> Preferences:
        data = _mapping(payload, "config.preferences")
        return cls(
            timezone=_string(
                data.get("timezone", "Asia/Shanghai"),
                "config.preferences.timezone",
            ),
            language=_string(
                data.get("language", "zh-CN"), "config.preferences.language"
            ),
            launch_at_login=_boolean(
                data.get("launch_at_login", False),
                "config.preferences.launch_at_login",
            ),
            quiet_hours_enabled=_boolean(
                data.get("quiet_hours_enabled", False),
                "config.preferences.quiet_hours_enabled",
            ),
            quiet_hours_start=_string(
                data.get("quiet_hours_start", "23:00"),
                "config.preferences.quiet_hours_start",
            ),
            quiet_hours_end=_string(
                data.get("quiet_hours_end", "07:00"),
                "config.preferences.quiet_hours_end",
            ),
        )

    def with_updates(
        self,
        *,
        launch_at_login: bool | None = None,
        quiet_hours_enabled: bool | None = None,
        quiet_hours_start: str | None = None,
        quiet_hours_end: str | None = None,
    ) -> Preferences:
        return Preferences(
            timezone=self.timezone,
            language=self.language,
            launch_at_login=(
                self.launch_at_login
                if launch_at_login is None
                else launch_at_login
            ),
            quiet_hours_enabled=(
                self.quiet_hours_enabled
                if quiet_hours_enabled is None
                else quiet_hours_enabled
            ),
            quiet_hours_start=quiet_hours_start or self.quiet_hours_start,
            quiet_hours_end=quiet_hours_end or self.quiet_hours_end,
        )


@dataclass(frozen=True, slots=True)
class CockpitReceipt:
    receipt_id: str
    generated_at: datetime
    delivery_mode: str
    overall_color: StatusColor
    state: str
    reason_codes: tuple[str, ...]
    markets: tuple[MarketSchedule, ...]
    slo_30d: SloWindow
    silence_state: str
    silence_color: StatusColor
    fresh_data: Coverage
    trusted_decision: Coverage

    @classmethod
    def from_payload(cls, payload: object) -> CockpitReceipt:
        data = _mapping(payload, "cockpit")
        schedule = _mapping(data.get("schedule"), "cockpit.schedule")
        silence = _mapping(data.get("silence"), "cockpit.silence")
        markets_raw = _sequence(
            schedule.get("markets", []), "cockpit.schedule.markets"
        )
        return cls(
            receipt_id=_string(
                data.get("receipt_id", "LOCAL-RECEIPT"), "cockpit.receipt_id"
            ),
            generated_at=_timestamp(
                data.get("generated_at"), "cockpit.generated_at"
            ),
            delivery_mode=_string(
                data.get("delivery_mode", "PREVIEW"), "cockpit.delivery_mode"
            ),
            overall_color=_enum(
                StatusColor, data.get("overall_color"), "cockpit.overall_color"
            ),
            state=_string(data.get("state"), "cockpit.state"),
            reason_codes=_strings(
                data.get("reason_codes", []), "cockpit.reason_codes"
            ),
            markets=tuple(
                MarketSchedule.from_payload(item, index)
                for index, item in enumerate(markets_raw)
            ),
            slo_30d=SloWindow.from_payload(
                schedule.get("slo_30d", {}), "cockpit.schedule.slo_30d"
            ),
            silence_state=_string(
                silence.get("state"), "cockpit.silence.state"
            ),
            silence_color=_enum(
                StatusColor, silence.get("color"), "cockpit.silence.color"
            ),
            fresh_data=Coverage.from_payload(
                silence.get("fresh_data", {}), "cockpit.silence.fresh_data"
            ),
            trusted_decision=Coverage.from_payload(
                silence.get("trusted_decision", {}),
                "cockpit.silence.trusted_decision",
            ),
        )


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    health: GuardianHealth
    cockpit: CockpitReceipt
    channels: tuple[DeliveryChannel, ...]
    providers: tuple[ProviderCapability, ...]
    assets: tuple[AssetStatus, ...]
    incidents: tuple[Incident, ...]
    preferences: Preferences
    config_revision: int

    @classmethod
    def from_payloads(
        cls,
        *,
        health: object,
        cockpit: object,
        config: object,
        incidents: object,
        providers: object,
    ) -> DashboardSnapshot:
        health_dto = GuardianHealth.from_payload(health)
        cockpit_data = _mapping(cockpit, "cockpit")
        config_data = _mapping(config, "config")
        incident_data = _mapping(incidents, "incidents")
        provider_data = _mapping(providers, "providers")
        delivery_data = _mapping(
            cockpit_data.get("delivery", {}), "cockpit.delivery"
        )
        channel_settings = _mapping(
            config_data.get("channels", {}), "config.channels"
        )
        assets_raw = _sequence(
            config_data.get("assets", cockpit_data.get("assets", [])),
            "config.assets",
        )
        incidents_raw = _sequence(
            incident_data.get("items", incident_data.get("incidents", [])),
            "incidents.items",
        )
        capabilities_raw = _sequence(
            provider_data.get("capabilities", []), "providers.capabilities"
        )
        channels: list[DeliveryChannel] = []
        for kind in ChannelKind:
            raw = delivery_data.get(kind.value)
            if raw is None:
                raw = {
                    "configured": False,
                    "mode": "PREVIEW",
                    "last_attempt_at": None,
                    "last_success_at": None,
                    "success": None,
                    "error_code": None,
                }
            channels.append(
                DeliveryChannel.from_payload(
                    kind,
                    raw,
                    settings=channel_settings.get(kind.value, {}),
                )
            )
        return cls(
            health=health_dto,
            cockpit=CockpitReceipt.from_payload(cockpit_data),
            channels=tuple(channels),
            providers=tuple(
                ProviderCapability.from_payload(item, index)
                for index, item in enumerate(capabilities_raw)
            ),
            assets=tuple(
                AssetStatus.from_payload(item, index)
                for index, item in enumerate(assets_raw)
            ),
            incidents=tuple(
                Incident.from_payload(item, index)
                for index, item in enumerate(incidents_raw)
            ),
            preferences=Preferences.from_payload(
                config_data.get("preferences", {})
            ),
            config_revision=_integer(
                config_data.get("revision", 0), "config.revision"
            ),
        )


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    action: str
    accepted: bool
    message: str
    request_id: str

    @classmethod
    def from_payload(cls, payload: object) -> ActionReceipt:
        data = _mapping(payload, "action")
        return cls(
            action=_string(data.get("action"), "action.action"),
            accepted=_boolean(data.get("accepted"), "action.accepted"),
            message=_string(data.get("message"), "action.message"),
            request_id=_string(data.get("request_id"), "action.request_id"),
        )


def public_config_update(preferences: Preferences, revision: int) -> dict[str, Any]:
    """Build the only configuration payload the UI is allowed to write."""

    return {
        "revision": revision,
        "preferences": {
            "timezone": preferences.timezone,
            "language": preferences.language,
            "launch_at_login": preferences.launch_at_login,
            "quiet_hours_enabled": preferences.quiet_hours_enabled,
            "quiet_hours_start": preferences.quiet_hours_start,
            "quiet_hours_end": preferences.quiet_hours_end,
        },
    }
