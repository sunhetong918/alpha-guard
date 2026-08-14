"""Concrete, secret-redacted backend for the desktop Guardian API."""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast

from pydantic import JsonValue

from config import Settings, get_settings, load_rules_config
from main import (
    STATE_PATH,
    _cockpit_configuration,
    run_stock_scan,
)
from notifier.heartbeat import delivery_config_fingerprints
from state import CorruptProtectionStateError, StateStore
from state.cockpit import (
    build_corrupt_reliability_cockpit,
    build_reliability_cockpit,
)

from .application import (
    GuardianBackend,
    GuardianDispatchError,
    RequestContext,
)
from .protocol import RpcErrorCode
from .preferences import (
    DesktopPreferences,
    PreferencesConflictError,
    PreferencesDocument,
    PreferencesStore,
    PreferencesStoreError,
)
from .runtime import GuardianRuntimeError, GuardianSupervisor


def _version() -> str:
    try:
        return importlib.metadata.version("alpha-guard")
    except importlib.metadata.PackageNotFoundError:
        return "0.3.0-dev"


def _configuration(
    settings: Settings,
    *,
    revision: int,
    preferences: DesktopPreferences,
) -> dict[str, JsonValue]:
    """Return public configuration only; never serialize secret fields."""

    rules = load_rules_config()
    assets: list[dict[str, JsonValue]] = []
    for ticker, instrument in sorted(rules.watchlist.items()):
        assets.append(
            {
                "symbol": ticker,
                "name": instrument.name,
                "market": instrument.market,
                "enabled": instrument.enabled,
                "currency": instrument.currency,
                "rule_count": len(instrument.buy_rules + instrument.sell_rules),
            }
        )
    return {
        "revision": revision,
        "assets": cast(list[JsonValue], assets),
        "channels": {
            "telegram": {
                "configured": bool(
                    settings.notifications_enabled
                    and settings.telegram_bot_token is not None
                    and settings.telegram_chat_id
                ),
                "recipient_hint": "configured" if settings.telegram_chat_id else None,
            },
            "whatsapp": {
                "configured": bool(settings.whatsapp_enabled),
                "recipient_hint": "configured" if settings.whatsapp_default_to else None,
            },
            "external_watcher": {
                "configured": bool(settings.heartbeat_enabled),
                "recipient_hint": "configured" if settings.heartbeat_url else None,
            },
        },
        "preferences": preferences.model_dump(mode="json"),
    }


def _public_incidents(store: StateStore, *, now: datetime) -> list[dict[str, JsonValue]]:
    incidents: list[dict[str, JsonValue]] = []
    for item in store.watchdog_incidents(not_after=now)[:20]:
        payload = item["payload"]
        incidents.append(
            {
                "id": f"watchdog-{item['id']}",
                "color": "RED" if item["active"] else "BLUE",
                "status": "OPEN" if item["active"] else "RESOLVED",
                "title": "预期扫描窗口失联" if item["active"] else "扫描保护已恢复",
                "scope": "global",
                "opened_at": item["first_seen_at"],
                "updated_at": item["last_seen_at"],
                "summary": f"影响 {len(payload['affected_tickers'])} 个标的",
                "reason_codes": [
                    "watchdog_incident_active" if item["active"] else "watchdog_recovered"
                ],
                "next_actions": ["打开可靠性回执核验影响范围"],
            }
        )
    for item in store.integrity_incidents(active_only=False)[:20]:
        incidents.append(
            {
                "id": f"integrity-{item['id']}",
                "color": "RED" if item["active"] else "BLUE",
                "status": "OPEN" if item["active"] else "RESOLVED",
                "title": "本地安全账本需要人工修复",
                "scope": item["scope"],
                "opened_at": item["first_seen_at"],
                "updated_at": item["last_seen_at"],
                "summary": item["component"],
                "reason_codes": [item["reason_code"]],
                "next_actions": ["使用 CLI repair-state 并保留数据库备份"],
            }
        )
    incidents.sort(key=lambda item: str(item["updated_at"]), reverse=True)
    return incidents[:50]


class AlphaGuardBackend(GuardianBackend):
    """Expose the existing reliability kernel through strict Guardian DTOs."""

    def __init__(
        self,
        *,
        settings_loader: Callable[[], Settings] = get_settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        request_stop: Callable[[], None] | None = None,
        request_restart: Callable[[], None] | None = None,
        runtime: GuardianSupervisor | None = None,
        preferences_store: PreferencesStore | None = None,
        autostart_apply: Callable[[bool], None] | None = None,
    ) -> None:
        self._settings_loader = settings_loader
        self._clock = clock
        self._request_stop = request_stop
        self._request_restart = request_restart
        self._runtime = runtime
        self._preferences_store = preferences_store or PreferencesStore()
        self._autostart_apply = autostart_apply
        self._started_at = clock().astimezone(UTC)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(UTC)

    def _cockpit(self, *, limit: int = 20) -> dict[str, Any]:
        now = self._now()
        rules = load_rules_config()
        enabled, contracts = _cockpit_configuration(rules)
        fingerprints = delivery_config_fingerprints(self._settings_loader())
        try:
            if STATE_PATH.exists():
                with StateStore(STATE_PATH) as store:
                    return build_reliability_cockpit(
                        store=store,
                        enabled_instruments=enabled,
                        market_contract_hashes=contracts,
                        current_delivery_fingerprints=fingerprints,
                        generated_at=now,
                        recent_run_limit=limit,
                    )
            return build_reliability_cockpit(
                store=None,
                enabled_instruments=enabled,
                market_contract_hashes=contracts,
                current_delivery_fingerprints=fingerprints,
                generated_at=now,
                recent_run_limit=limit,
            )
        except (OSError, sqlite3.Error, CorruptProtectionStateError):
            return build_corrupt_reliability_cockpit(
                enabled_instruments=enabled,
                generated_at=now,
            )

    def health_get(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del context
        _require_no_params(params)
        now = self._now()
        runtime_status = self._runtime.status if self._runtime is not None else None
        preferences = self._load_preferences()
        raw_runtime_state = (
            runtime_status.state if runtime_status is not None else "RUNNING"
        )
        guardian_state = (
            "DEGRADED" if raw_runtime_state == "STARTING" else raw_runtime_state
        )
        guardian_color = (
            "GREEN"
            if guardian_state == "RUNNING"
            else ("BLUE" if raw_runtime_state == "STARTING" else "RED")
        )
        return {
            "source": "guardian",
            "generated_at": now.isoformat(),
            "guardian": {
                "state": guardian_state,
                "color": guardian_color,
                "version": _version(),
                "pid": os.getpid(),
                "started_at": self._started_at.isoformat(),
                "last_heartbeat_at": now.isoformat(),
                "launch_at_login": preferences.preferences.launch_at_login,
            },
        }

    def cockpit_get(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del context
        limit = _bounded_limit(params, default=20)
        return self._cockpit(limit=limit)

    def runs_list(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del context
        limit = _bounded_limit(params, default=20)
        return {"items": list(self._cockpit(limit=limit).get("recent_runs", []))}

    def incidents_list(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del context
        limit = _bounded_limit(params, default=50)
        now = self._now()
        if not STATE_PATH.exists():
            return {"items": []}
        try:
            with StateStore(STATE_PATH) as store:
                return {
                    "items": cast(
                        list[JsonValue],
                        _public_incidents(store, now=now)[:limit],
                    )
                }
        except (OSError, CorruptProtectionStateError):
            raise GuardianDispatchError(
                code=RpcErrorCode.SERVICE_UNAVAILABLE,
                kind="service_unavailable",
                message="Incident ledger is unavailable",
            ) from None

    def providers_list(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del context
        _require_no_params(params)
        return self._cockpit(limit=1).get("providers", {"capabilities": []})

    def config_get(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del context
        _require_no_params(params)
        document = self._load_preferences()
        return _configuration(
            self._settings_loader(),
            revision=document.revision,
            preferences=document.preferences,
        )

    def config_validate(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del context
        # V1 only validates public preferences. Secret credentials stay in the
        # OS credential store/environment and never cross this API.
        _validated_preferences(params)
        return {"valid": True}

    def config_apply(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        del context
        revision, preferences = _validated_preferences(params)
        previous = self._load_preferences()
        if previous.revision != revision:
            raise GuardianDispatchError(
                code=RpcErrorCode.CONFLICT,
                kind="conflict",
                message="Configuration revision changed",
                retryable=True,
            )
        autostart_changed = (
            previous.preferences.launch_at_login != preferences.launch_at_login
        )
        if self._autostart_apply is not None and (
            autostart_changed
        ):
            try:
                self._autostart_apply(preferences.launch_at_login)
            except OSError:
                raise GuardianDispatchError(
                    code=RpcErrorCode.SERVICE_UNAVAILABLE,
                    kind="service_unavailable",
                    message="Launch-at-login could not be updated",
                    retryable=True,
                ) from None
        try:
            document = self._preferences_store.apply(
                preferences,
                expected_revision=revision,
            )
        except PreferencesConflictError:
            raise GuardianDispatchError(
                code=RpcErrorCode.CONFLICT,
                kind="conflict",
                message="Configuration revision changed",
                retryable=True,
            ) from None
        except PreferencesStoreError:
            if self._autostart_apply is not None and autostart_changed:
                try:
                    self._autostart_apply(previous.preferences.launch_at_login)
                except OSError:
                    pass
            raise GuardianDispatchError(
                code=RpcErrorCode.SERVICE_UNAVAILABLE,
                kind="service_unavailable",
                message="Configuration could not be saved",
                retryable=True,
            ) from None
        return {
            "applied": True,
            "revision": document.revision,
            "preferences": document.preferences.model_dump(mode="json"),
        }

    def scan_trigger(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        market = params.get("market")
        if market is not None and market not in {"US", "HK"}:
            _invalid_params()
        if set(params) - {"market"}:
            _invalid_params()
        if self._runtime is not None:
            try:
                job_id = self._runtime.submit_scan(
                    cast(Literal["US", "HK"] | None, market)
                )
            except GuardianRuntimeError:
                raise GuardianDispatchError(
                    code=RpcErrorCode.SERVICE_UNAVAILABLE,
                    kind="service_unavailable",
                    message="Guardian runtime is unavailable",
                    retryable=True,
                ) from None
            return {
                "action": "scan",
                "accepted": True,
                "message": "扫描已交给 Guardian",
                "request_id": context.request_id,
                "job_id": job_id,
            }
        outcome = asyncio.run(run_stock_scan(market=market, notify=False))
        return {
            "action": "scan",
            "accepted": True,
            "message": "扫描已完成",
            "request_id": context.request_id,
            "status": str(outcome.get("status", "unknown")),
        }

    def delivery_test(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        channel = params.get("channel")
        if channel not in {"telegram", "whatsapp"} or set(params) != {"channel"}:
            _invalid_params()
        if self._runtime is None:
            raise GuardianDispatchError(
                code=RpcErrorCode.METHOD_NOT_IMPLEMENTED,
                kind="method_not_implemented",
                message="Channel tests require the Guardian runtime",
            )
        try:
            job_id = self._runtime.submit_delivery_test(
                cast(Literal["telegram", "whatsapp"], channel)
            )
        except GuardianRuntimeError:
            raise GuardianDispatchError(
                code=RpcErrorCode.SERVICE_UNAVAILABLE,
                kind="service_unavailable",
                message="Guardian runtime is unavailable",
                retryable=True,
            ) from None
        return {
            "action": "test-channel",
            "accepted": True,
            "message": "通道测试已排队；结果将在通道状态中显示",
            "request_id": context.request_id,
            "job_id": job_id,
            "status": "queued",
        }

    def guardian_stop(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        _require_no_params(params)
        if self._request_stop is None:
            raise GuardianDispatchError.not_implemented(context.method)
        self._request_stop()
        return {"accepted": True}

    def _load_preferences(self) -> PreferencesDocument:
        try:
            return self._preferences_store.load()
        except PreferencesStoreError:
            raise GuardianDispatchError(
                code=RpcErrorCode.SERVICE_UNAVAILABLE,
                kind="service_unavailable",
                message="Public preferences are unavailable",
            ) from None

    def guardian_restart(
        self, params: dict[str, JsonValue], context: RequestContext
    ) -> JsonValue:
        _require_no_params(params)
        if self._request_restart is None:
            raise GuardianDispatchError.not_implemented(context.method)
        self._request_restart()
        return {"accepted": True}


def _require_no_params(params: Mapping[str, JsonValue]) -> None:
    if params:
        _invalid_params()


def _bounded_limit(params: Mapping[str, JsonValue], *, default: int) -> int:
    if set(params) - {"limit"}:
        _invalid_params()
    value = params.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        _invalid_params()
    return value


def _validated_preferences(
    params: Mapping[str, JsonValue],
) -> tuple[int, DesktopPreferences]:
    if set(params) != {"revision", "preferences"}:
        _invalid_params()
    revision = params["revision"]
    preferences = params["preferences"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        _invalid_params()
    if not isinstance(preferences, Mapping) or set(preferences) - {
        "timezone",
        "language",
        "launch_at_login",
        "quiet_hours_enabled",
        "quiet_hours_start",
        "quiet_hours_end",
    }:
        _invalid_params()
    try:
        validated = DesktopPreferences.model_validate(dict(preferences))
    except Exception:
        _invalid_params()
    return revision, validated


def _invalid_params() -> NoReturn:
    raise GuardianDispatchError(
        code=RpcErrorCode.INVALID_PARAMS,
        kind="invalid_params",
        message="Guardian method parameters are invalid",
    )


__all__ = ["AlphaGuardBackend"]
