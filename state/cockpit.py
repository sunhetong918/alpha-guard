"""Pure Reliability Cockpit receipt builder.

This module projects validated safety evidence into a small, stable receipt.
It performs no network I/O and never exposes raw persisted payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from config import MOBILE_TRUST_PROOF_MAX_AGE_SECONDS
from reliability import (
    ProviderKey,
    ProviderRuntime,
    ProviderRuntimeConfig,
    RuntimeState,
)
from scheduler import expected_market_scans_between, latest_expected_market_scan

from .blindness import ProtectionSnapshot, ProtectionState, STATE_COLORS
from .store import CorruptProtectionStateError, StateStore, instrument_set_hash


_STATE_PRIORITY = {
    ProtectionState.HEALTHY: 0,
    ProtectionState.PAUSED: 1,
    ProtectionState.UNCONFIGURED: 1,
    ProtectionState.RECOVERING: 2,
    ProtectionState.DEGRADED: 3,
    ProtectionState.BLIND: 4,
}
_MOBILE_TRUST_PROOF_MAX_AGE = timedelta(
    seconds=MOBILE_TRUST_PROOF_MAX_AGE_SECONDS
)


def _mobile_proof_expired(
    delivery: Mapping[str, Any],
    *,
    generated_at: datetime,
) -> bool:
    raw = delivery.get("last_success_at")
    if not isinstance(raw, str):
        return False
    try:
        succeeded_at = _parse_timestamp(raw)
    except (TypeError, ValueError, CorruptProtectionStateError):
        return True
    age = generated_at - succeeded_at
    return age < timedelta(0) or age > _MOBILE_TRUST_PROOF_MAX_AGE


def _empty_watchdog_projection() -> dict[str, Any]:
    return {
        "state": None,
        "generation": None,
        "active": False,
        "affected": [],
        "markets": [],
        "window_count": 0,
        "first_seen_at": None,
        "resolved_at": None,
        "delivery_status": None,
    }


def _project_watchdog_incident(
    incidents: list[dict[str, Any]],
) -> dict[str, Any]:
    if not incidents:
        return _empty_watchdog_projection()
    latest = incidents[0]
    payload = latest["payload"]
    return {
        "state": latest["state"],
        "generation": latest["generation"],
        "active": latest["active"],
        "affected": list(payload["affected_tickers"]),
        "markets": list(payload["markets"]),
        "window_count": len(payload["window_keys"]),
        "first_seen_at": latest["first_seen_at"],
        "resolved_at": latest["resolved_at"],
        "delivery_status": latest["delivery_status"],
    }


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorruptProtectionStateError("persisted safety timestamp is corrupt")
    return parsed.astimezone(UTC)


def _reject_future_protection_evidence(
    *,
    generated_at: datetime,
    scope: Mapping[str, Any] | None,
    snapshots: Mapping[str, ProtectionSnapshot],
    windows: list[dict[str, Any]],
    delivery_states: Mapping[str, Mapping[str, Any]],
) -> None:
    if scope is not None:
        scope_times = [
            _parse_timestamp(scope["activated_at"]),
            _parse_timestamp(scope["updated_at"]),
            *(
                _parse_timestamp(value)
                for value in scope["market_epochs"].values()
            ),
        ]
        if any(value > generated_at for value in scope_times):
            raise CorruptProtectionStateError(
                "persisted protection scope is future-dated"
            )
    for snapshot in snapshots.values():
        snapshot_times = (
            snapshot.state_since,
            snapshot.updated_at,
            snapshot.incident_started_at,
            snapshot.blind_started_at,
            snapshot.recovered_at,
            snapshot.last_success_at,
        )
        if any(
            value is not None and value > generated_at
            for value in snapshot_times
        ):
            raise CorruptProtectionStateError(
                "persisted protection state is future-dated"
            )
    for window in windows:
        window_times = (
            window["expected_at"],
            window["actual_at"],
            window["last_success_at"],
            window["updated_at"],
        )
        if any(
            value is not None and _parse_timestamp(value) > generated_at
            for value in window_times
        ):
            raise CorruptProtectionStateError(
                "persisted protection window is future-dated"
            )
    for delivery in delivery_states.values():
        delivery_times = (
            delivery["last_attempt_at"],
            delivery["last_success_at"],
            delivery["updated_at"],
        )
        if any(
            value is not None and _parse_timestamp(value) > generated_at
            for value in delivery_times
        ):
            raise CorruptProtectionStateError(
                "persisted delivery evidence is future-dated"
            )


def _bad_window_absorbed(
    snapshot: ProtectionSnapshot | None,
    deadline: datetime,
) -> bool:
    if snapshot is None:
        return False
    if snapshot.state is ProtectionState.RECOVERING:
        return bool(
            snapshot.recovery_has_full_scan
            and snapshot.healthy_confirmations == 1
            and snapshot.last_success_at is not None
            and snapshot.last_success_at > deadline
        )
    if snapshot.state is ProtectionState.HEALTHY:
        return bool(
            snapshot.recovery_has_full_scan
            and snapshot.healthy_confirmations >= 2
            and snapshot.recovered_at is not None
            and snapshot.recovered_at >= deadline
            and snapshot.last_success_at is not None
            and snapshot.last_success_at > deadline
        )
    return bool(
        snapshot.state is ProtectionState.DEGRADED
        and snapshot.updated_at > deadline
    )


def _provider_key(storage_key: str) -> ProviderKey:
    if not isinstance(storage_key, str):
        raise CorruptProtectionStateError(
            "persisted provider runtime evidence is corrupt"
        )
    parts = storage_key.split(":")
    if len(parts) != 3:
        raise CorruptProtectionStateError(
            "persisted provider runtime evidence is corrupt"
        )
    try:
        key = ProviderKey(
            provider=parts[0], operation=parts[1], market=parts[2]
        )
    except ValidationError:
        raise CorruptProtectionStateError(
            "persisted provider runtime evidence is corrupt"
        ) from None
    if key.storage_key != storage_key:
        raise CorruptProtectionStateError(
            "persisted provider runtime evidence is corrupt"
        )
    return key


def _provider_capabilities(
    store: StateStore,
    *,
    generated_at: datetime,
) -> list[dict[str, Any]]:
    raw = store.load_provider_runtime_state(
        strict=True,
        not_after=generated_at,
    )
    if raw is None:
        return []
    try:
        state = RuntimeState.model_validate(raw)
    except ValidationError:
        raise CorruptProtectionStateError(
            "persisted provider runtime evidence is corrupt"
        ) from None
    storage_keys = sorted(set(state.circuits) | set(state.observations))
    keys: dict[str, ProviderKey] = {}
    for storage_key in storage_keys:
        key = _provider_key(storage_key)
        keys[storage_key] = key
        circuit = state.circuits.get(storage_key)
        if circuit is not None:
            circuit_is_open = circuit.state.value in {"open", "half_open"}
            if circuit_is_open != (circuit.opened_at is not None) or (
                circuit.opened_at is not None
                and circuit.opened_at > generated_at
            ):
                raise CorruptProtectionStateError(
                    "persisted provider runtime evidence is corrupt"
                )
        for attempt in state.observations.get(storage_key, ()):
            if (
                attempt.provider != key.provider
                or attempt.operation != key.operation
                or attempt.market != key.market
                or attempt.observed_at > generated_at
            ):
                raise CorruptProtectionStateError(
                    "persisted provider runtime evidence is corrupt"
                )
    for storage_key, cache in state.caches.items():
        if not isinstance(storage_key, str) or ":" not in storage_key:
            raise CorruptProtectionStateError(
                "persisted provider runtime evidence is corrupt"
            )
        provider_storage_key, cache_digest = storage_key.rsplit(":", 1)
        _provider_key(provider_storage_key)
        if (
            len(cache_digest) != 24
            or any(character not in "0123456789abcdef" for character in cache_digest)
        ):
            raise CorruptProtectionStateError(
                "persisted provider runtime evidence is corrupt"
            )
        try:
            stored_at = datetime.fromisoformat(str(cache["stored_at"]))
        except (KeyError, TypeError, ValueError):
            raise CorruptProtectionStateError(
                "persisted provider runtime evidence is corrupt"
            ) from None
        if (
            stored_at.tzinfo is None
            or stored_at.utcoffset() is None
            or stored_at.astimezone(UTC) > generated_at
        ):
            raise CorruptProtectionStateError(
                "persisted provider runtime evidence is corrupt"
            )
    persisted_observation_limit = max(
        (len(items) for items in state.observations.values()),
        default=0,
    )
    if persisted_observation_limit > 10_000:
        raise CorruptProtectionStateError(
            "persisted provider runtime evidence is corrupt"
        )
    observation_limit = max(10, persisted_observation_limit)
    runtime = ProviderRuntime(
        ProviderRuntimeConfig(observation_limit=observation_limit)
    )
    runtime.import_state(state.model_copy(update={"caches": {}}))
    capabilities: list[dict[str, Any]] = []
    for storage_key in storage_keys:
        key = keys[storage_key]
        health = runtime.provider_health(key, minimum_samples=20)
        circuit = runtime.circuit_for(key)
        reasons: list[str] = []
        if health.grade == "insufficient_data":
            reasons.append("insufficient_samples")
        elif health.grade == "degraded":
            reasons.append("provider_degraded")
        elif health.grade == "unreliable":
            reasons.append("provider_unreliable")
        if circuit.state.value != "closed":
            reasons.append(f"circuit_{circuit.state.value}")
        capabilities.append(
            {
                "provider": key.provider,
                "operation": key.operation,
                "market": key.market,
                "sample_count": health.sample_count,
                "success_rate": health.success_rate,
                "wilson_lower_bound": health.wilson_lower_bound,
                "grade": health.grade,
                "circuit_state": circuit.state.value,
                "reasons": reasons,
            }
        )
    return capabilities


def _receipt(
    *,
    generated_at: datetime,
    state: ProtectionState,
    reasons: tuple[str, ...],
    enabled: int,
    usable: int,
    affected: tuple[str, ...],
    schedule: list[dict[str, Any]],
    delivery_states: Mapping[str, Mapping[str, Any]] | None = None,
    provider_capabilities: list[dict[str, Any]] | None = None,
    fresh_data: Mapping[str, Any] | None = None,
    trusted_decision: Mapping[str, Any] | None = None,
    recent_runs: list[dict[str, Any]] | None = None,
    watchdog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ratio = usable / enabled if enabled else None
    slo_good = sum(item.get("slo_30d", {}).get("good", 0) for item in schedule)
    slo_bad = sum(item.get("slo_30d", {}).get("bad", 0) for item in schedule)
    slo_missing = sum(
        item.get("slo_30d", {}).get("missing", 0) for item in schedule
    )
    slo_pending = sum(
        item.get("slo_30d", {}).get("pending", 0) for item in schedule
    )
    slo_expected = sum(
        item.get("slo_30d", {}).get("expected", 0) for item in schedule
    )
    slo_violations = slo_bad + slo_missing + slo_pending
    slo_error_rate = slo_violations / slo_expected if slo_expected else None
    slo_burn = slo_error_rate / 0.01 if slo_error_rate is not None else None
    channel_defaults: dict[str, Any] = {
        "configured": False,
        "mode": "PREVIEW",
        "last_attempt_at": None,
        "last_success_at": None,
        "success": None,
        "error_code": None,
    }
    delivery: dict[str, dict[str, Any]] = {}
    for public_name, persisted_name in (
        ("telegram", "telegram"),
        ("whatsapp", "whatsapp"),
        ("external_watcher", "heartbeat"),
    ):
        persisted = (
            delivery_states.get(persisted_name) if delivery_states is not None else None
        )
        projected = dict(channel_defaults)
        if persisted is not None:
            projected.update(
                {key: persisted[key] for key in channel_defaults}
            )
            projected["mode"] = str(projected["mode"]).upper()
        delivery[public_name] = projected

    persisted_modes = {
        item["mode"].upper() for item in (delivery_states or {}).values()
    }
    delivery_mode = "ACTIVE" if "ACTIVE" in persisted_modes else "PREVIEW"
    overall_state = state
    overall_reasons = list(dict.fromkeys(reasons))
    watchdog_projection = dict(watchdog or _empty_watchdog_projection())
    if watchdog_projection["active"] is True:
        overall_state = ProtectionState.BLIND
        overall_reasons.append("watchdog_incident_active")
    telegram = delivery["telegram"]
    active_configured_channels = [
        item
        for item in (telegram, delivery["whatsapp"])
        if item["mode"] == "ACTIVE" and item["configured"]
    ]
    configured_mobile_modes = {
        item["mode"]
        for item in (telegram, delivery["whatsapp"])
        if item["configured"]
    }
    if len(configured_mobile_modes) > 1:
        overall_state = ProtectionState.BLIND
        overall_reasons.append("mode_mismatch")
    if telegram["mode"] == "ACTIVE" and telegram["configured"]:
        delivery_reason: str | None = None
        if telegram["error_code"] is not None or telegram["success"] is False:
            delivery_reason = "delivery_unavailable"
        elif telegram["last_success_at"] is None:
            delivery_reason = "delivery_unproven"
        elif _mobile_proof_expired(telegram, generated_at=generated_at):
            delivery_reason = "delivery_proof_expired"
        if delivery_reason is not None:
            overall_state = ProtectionState.BLIND
            overall_reasons.append(delivery_reason)
    whatsapp = delivery["whatsapp"]
    if whatsapp["mode"] == "ACTIVE" and whatsapp["configured"]:
        whatsapp_reason: str | None = None
        if whatsapp["error_code"] is not None or whatsapp["success"] is False:
            whatsapp_reason = "whatsapp_unavailable"
        elif whatsapp["last_success_at"] is None:
            whatsapp_reason = "whatsapp_unproven"
        elif _mobile_proof_expired(whatsapp, generated_at=generated_at):
            whatsapp_reason = "whatsapp_proof_expired"
        if whatsapp_reason is not None:
            overall_state = ProtectionState.BLIND
            overall_reasons.append(whatsapp_reason)
    if delivery_mode == "ACTIVE" and not active_configured_channels:
        overall_state = ProtectionState.BLIND
        overall_reasons.append("delivery_unconfigured")
        overall_reasons.append("mode_mismatch")
    watcher = delivery["external_watcher"]
    if (
        watcher["mode"] == "ACTIVE"
        and watcher["configured"]
    ):
        watcher_reason = None
        if watcher["error_code"] is not None or watcher["success"] is False:
            watcher_reason = "watcher_unavailable"
        elif watcher["last_success_at"] is None:
            watcher_reason = "watcher_unproven"
        if watcher_reason is not None:
            overall_state = ProtectionState.BLIND
            overall_reasons.append(watcher_reason)

    projected_fresh = dict(
        fresh_data
        or {
            "enabled": enabled,
            "usable": None,
            "ratio": None,
            "affected": [],
            "known": False,
        }
    )
    projected_trusted = dict(
        trusted_decision
        or {
            "enabled": enabled,
            "usable": usable,
            "ratio": ratio,
            "affected": list(affected),
            "known": True,
        }
    )

    return {
        "generated_at": generated_at.isoformat(),
        "delivery_mode": delivery_mode,
        "overall_color": STATE_COLORS[overall_state],
        "state": overall_state.value,
        "reason_codes": list(dict.fromkeys(overall_reasons)),
        "schedule": {
            "markets": schedule,
            "slo_30d": {
                "target": 0.99,
                "good": slo_good,
                "bad": slo_bad,
                "missing": slo_missing,
                "pending": slo_pending,
                "expected": slo_expected,
                "violations": slo_violations,
                "ratio": slo_good / slo_expected if slo_expected else None,
                "error_rate": slo_error_rate,
                "burn_rate": slo_burn,
                "error_budget_consumed": slo_burn,
            },
        },
        "silence": {
            "state": state.value,
            "color": STATE_COLORS[state],
            "enabled": enabled,
            "usable": usable,
            "ratio": ratio,
            "affected": list(affected),
            "fresh_data": projected_fresh,
            "trusted_decision": projected_trusted,
        },
        "providers": {"capabilities": provider_capabilities or []},
        "delivery": delivery,
        "watchdog": watchdog_projection,
        "recent_runs": recent_runs or [],
    }


def _corrupt_receipt(
    *, generated_at: datetime, enabled: int
) -> dict[str, Any]:
    return _receipt(
        generated_at=generated_at,
        state=ProtectionState.BLIND,
        reasons=("state_corrupt",),
        enabled=enabled,
        usable=0,
        affected=(),
        schedule=[],
    )


def build_corrupt_reliability_cockpit(
    *,
    enabled_instruments: Mapping[str, str],
    generated_at: datetime,
) -> dict[str, Any]:
    """Return a fixed fail-closed receipt when SQLite cannot be read safely."""

    return _corrupt_receipt(
        generated_at=_aware_utc(generated_at),
        enabled=len(enabled_instruments),
    )


def build_reliability_cockpit(
    *,
    store: StateStore | None,
    enabled_instruments: Mapping[str, str],
    market_contract_hashes: Mapping[str, str],
    current_delivery_fingerprints: Mapping[str, str],
    generated_at: datetime,
    recent_run_limit: int = 20,
) -> dict[str, Any]:
    """Build a fail-closed, offline reliability receipt.

    ``enabled_instruments`` maps ticker to its configured market.  Protection
    responsibility is activated only by the validated global scope ledger;
    configured instruments alone never manufacture a protected baseline.
    """

    if (
        isinstance(recent_run_limit, bool)
        or not isinstance(recent_run_limit, int)
        or not 1 <= recent_run_limit <= 100
    ):
        raise ValueError("recent_run_limit must be between 1 and 100")
    now = _aware_utc(generated_at)
    if set(current_delivery_fingerprints) != {
        "telegram",
        "whatsapp",
        "heartbeat",
    }:
        raise ValueError(
            "delivery fingerprints must cover telegram, whatsapp, and heartbeat"
        )
    current_delivery: dict[str, str] = {}
    for channel, digest in current_delivery_fingerprints.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("delivery fingerprints must be lowercase SHA-256")
        current_delivery[channel] = digest
    configured = dict(enabled_instruments)
    for ticker, market in configured.items():
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("enabled instrument tickers must be non-empty strings")
        if market not in {"US", "HK"}:
            raise ValueError("enabled instrument markets must be US or HK")
    enabled = len(configured)
    configured_markets = set(configured.values())
    if set(market_contract_hashes) != configured_markets:
        raise ValueError("contract hash markets must match configured markets")
    current_contracts: dict[str, str] = {}
    for market, digest in market_contract_hashes.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("contract hashes must be lowercase SHA-256 digests")
        current_contracts[market] = digest
    delivery_states: dict[str, dict[str, Any]] = {}
    provider_capabilities: list[dict[str, Any]] = []
    safe_run_records: list[dict[str, Any]] = []
    projected_fresh_data: dict[str, Any] | None = None
    projected_trusted_decision: dict[str, Any] | None = None
    recent_runs: list[dict[str, Any]] = []
    watchdog_projection = _empty_watchdog_projection()

    if store is None:
        if enabled == 0:
            return _receipt(
                generated_at=now,
                state=ProtectionState.UNCONFIGURED,
                reasons=("unconfigured",),
                enabled=0,
                usable=0,
                affected=(),
                schedule=[],
                delivery_states=delivery_states,
                provider_capabilities=provider_capabilities,
            )
        return _receipt(
            generated_at=now,
            state=ProtectionState.RECOVERING,
            reasons=("protection_not_activated",),
            enabled=enabled,
            usable=0,
            affected=tuple(sorted(configured)),
            schedule=[],
            provider_capabilities=provider_capabilities,
        )

    try:
        # Watchdog evidence is part of the fail-closed safety ledger. Validate
        # its full history and authenticated scope generation before provider,
        # run, pause, zero-enabled or absent-scope views can return.
        scope = store.get_protection_scope("global", not_after=now)
        watchdog_incidents = store.watchdog_incidents_with_scope_proof(
            scope="global",
            not_after=now,
        )
        watchdog_projection = _project_watchdog_incident(
            watchdog_incidents
        )
        provider_capabilities = _provider_capabilities(
            store,
            generated_at=now,
        )
        safe_run_records = store.run_records(not_after=now)
        latest_stock_run = max(
            (
                record
                for record in safe_run_records
                if record["job"]
                in {"stock-scan:US", "stock-scan:HK", "stock-scan:ALL"}
            ),
            key=lambda record: (
                _parse_timestamp(record["finished_at"]),
                record["id"],
            ),
            default=None,
        )
        runtime_state = store.load_provider_runtime_state(
            strict=True,
            not_after=now,
        )
        recent_runtime_missing = latest_stock_run is not None and runtime_state is None and (
            now - _parse_timestamp(latest_stock_run["finished_at"])
            < timedelta(minutes=2)
        )
        windows = store.protection_windows()
        active_integrity = store.integrity_incidents(active_only=True)
        persisted_delivery = store.delivery_states(not_after=now)
        delivery_states = {}
        for channel, delivery_row in persisted_delivery.items():
            projected = dict(delivery_row)
            if delivery_row.get("config_fingerprint") != current_delivery[channel]:
                projected.update(
                    {
                        "last_attempt_at": None,
                        "last_success_at": None,
                        "success": None,
                        "error_code": None,
                    }
                )
            delivery_states[channel] = projected
        all_states = store.protection_states()
        _reject_future_protection_evidence(
            generated_at=now,
            scope=scope,
            snapshots=all_states,
            windows=windows,
            delivery_states=delivery_states,
        )
        if active_integrity:
            return _corrupt_receipt(generated_at=now, enabled=enabled)

        if scope is None:
            if enabled == 0:
                return _receipt(
                    generated_at=now,
                    state=ProtectionState.UNCONFIGURED,
                    reasons=("unconfigured",),
                    enabled=0,
                    usable=0,
                    affected=(),
                    schedule=[],
                    delivery_states=delivery_states,
                    provider_capabilities=provider_capabilities,
                    watchdog=watchdog_projection,
                )
            return _receipt(
                generated_at=now,
                state=ProtectionState.RECOVERING,
                reasons=("protection_not_activated",),
                enabled=enabled,
                usable=0,
                affected=tuple(sorted(configured)),
                schedule=[],
                delivery_states=delivery_states,
                provider_capabilities=provider_capabilities,
                watchdog=watchdog_projection,
            )

        if (
            recent_runtime_missing
            and scope["watchdog_generation"] is not None
            and latest_stock_run is not None
            and latest_stock_run["status"] == "success"
            and len(safe_run_records) == 1
            and set(scope["market_contract_hashes"]) == configured_markets
        ):
            # Once an authenticated protected scope has emitted a recent stock
            # run, an absent runtime row is evidence loss rather than an
            # innocent first-start state. Legacy/unit fixtures without the
            # generation proof remain compatible and cannot be heartbeat-green.
            return _corrupt_receipt(generated_at=now, enabled=enabled)

        if enabled == 0:
            return _receipt(
                generated_at=now,
                state=ProtectionState.UNCONFIGURED,
                reasons=("unconfigured",),
                enabled=0,
                usable=0,
                affected=(),
                schedule=[],
                delivery_states=delivery_states,
                provider_capabilities=provider_capabilities,
                watchdog=watchdog_projection,
            )
        if scope["watchdog_generation"] is None:
            return _receipt(
                generated_at=now,
                state=ProtectionState.RECOVERING,
                reasons=("watchdog_generation_unproven",),
                enabled=enabled,
                usable=0,
                affected=tuple(sorted(configured)),
                schedule=[],
                delivery_states=delivery_states,
                provider_capabilities=provider_capabilities,
                watchdog=watchdog_projection,
            )
        if scope["paused"]:
            return _receipt(
                generated_at=now,
                state=ProtectionState.PAUSED,
                reasons=("paused",),
                enabled=enabled,
                usable=0,
                affected=(),
                schedule=[],
                delivery_states=delivery_states,
                provider_capabilities=provider_capabilities,
                watchdog=watchdog_projection,
            )

        scoped_markets = set(scope["enabled_markets"])
        if configured_markets != scoped_markets:
            return _receipt(
                generated_at=now,
                state=ProtectionState.BLIND,
                reasons=("scope_config_mismatch",),
                enabled=enabled,
                usable=0,
                affected=tuple(sorted(configured)),
                schedule=[],
                delivery_states=delivery_states,
                provider_capabilities=provider_capabilities,
                watchdog=watchdog_projection,
            )

        snapshots: list[ProtectionSnapshot] = []
        schedule: list[dict[str, Any]] = []
        due_gap = False
        unresolved_bad = False
        coverage_mismatch = False
        missing_baseline = False
        missing_tickers: set[str] = set()
        configuration_baseline_missing = False
        identity_reasons: list[str] = []
        window_by_key = {item["window_key"]: item for item in windows}
        epochs = scope["market_epochs"]
        for market in scope["enabled_markets"]:
            snapshot = all_states.get(f"market:{market}")
            expected_count = sum(
                configured_market == market
                for configured_market in configured.values()
            )
            configured_tickers = {
                ticker
                for ticker, configured_market in configured.items()
                if configured_market == market
            }
            persisted_identity = scope["market_instrument_hashes"].get(market)
            current_identity = instrument_set_hash(tuple(configured_tickers))
            if persisted_identity is None:
                configuration_baseline_missing = True
                identity_reasons.append("identity_unproven")
                missing_tickers.update(configured_tickers)
            elif persisted_identity != current_identity:
                configuration_baseline_missing = True
                identity_reasons.append("configuration_baseline_missing")
                missing_tickers.update(configured_tickers)
            persisted_contract = scope["market_contract_hashes"].get(market)
            if persisted_contract is None:
                configuration_baseline_missing = True
                identity_reasons.append("contract_unproven")
                missing_tickers.update(configured_tickers)
            elif persisted_contract != current_contracts[market]:
                configuration_baseline_missing = True
                identity_reasons.append("configuration_baseline_missing")
                missing_tickers.update(configured_tickers)
            if snapshot is None:
                missing_baseline = True
                missing_tickers.update(
                    ticker
                    for ticker, configured_market in configured.items()
                    if configured_market == market
                )
            else:
                snapshots.append(snapshot)
                if snapshot.coverage.enabled_instruments != expected_count:
                    coverage_mismatch = True
                if not set(snapshot.coverage.unusable_tickers).issubset(
                    configured_tickers
                ):
                    raise CorruptProtectionStateError(
                        "persisted protection impact evidence is corrupt"
                    )
            expected = latest_expected_market_scan(market, now)
            activation = _parse_timestamp(epochs[market])
            if (
                snapshot is not None
                and snapshot.state
                not in {ProtectionState.PAUSED, ProtectionState.UNCONFIGURED}
                and (
                    snapshot.last_success_at is None
                    or snapshot.last_success_at < activation
                )
            ):
                configuration_baseline_missing = True
                identity_reasons.append("configuration_baseline_missing")
                missing_tickers.update(configured_tickers)
            due_windows = tuple(
                item
                for item in expected_market_scans_between(
                    market,
                    max(now - timedelta(days=30), activation),
                    now,
                )
                if item.expected_at.astimezone(UTC) >= activation
                and item.deadline_at.astimezone(UTC) <= now
            )
            due_statuses = [
                (
                    window_by_key[item.key]["status"]
                    if item.key in window_by_key
                    else "missing"
                )
                for item in due_windows
            ]
            market_good = due_statuses.count("good")
            market_bad = due_statuses.count("bad")
            market_missing = due_statuses.count("missing")
            market_pending = due_statuses.count("pending")
            market_violations = market_bad + market_missing + market_pending
            market_error_rate = (
                market_violations / len(due_windows)
                if due_windows
                else None
            )
            market_burn = (
                market_error_rate / 0.01
                if market_error_rate is not None
                else None
            )
            for due_window in due_windows:
                persisted = window_by_key.get(due_window.key)
                if persisted is None or persisted["status"] == "pending":
                    due_gap = True
                elif persisted["status"] == "bad" and not _bad_window_absorbed(
                    snapshot,
                    due_window.deadline_at.astimezone(UTC),
                ):
                    unresolved_bad = True
            applicable = expected.expected_at.astimezone(UTC) >= activation
            window = window_by_key.get(expected.key) if applicable else None
            if not applicable:
                deadline_state = "outside_activation"
            elif now <= expected.deadline_at.astimezone(UTC):
                deadline_state = (
                    "completed"
                    if window is not None and window["status"] == "good"
                    else "within_grace"
                )
            elif window is None:
                deadline_state = "missing"
                due_gap = True
            elif window["status"] == "pending":
                deadline_state = "pending"
                due_gap = True
            else:
                deadline_state = (
                    "completed" if window["status"] == "good" else "bad"
                )
                if window["status"] == "bad":
                    deadline = expected.deadline_at.astimezone(UTC)
                    unresolved_bad = unresolved_bad or not _bad_window_absorbed(
                        snapshot, deadline
                    )
            schedule.append(
                {
                    "market": market,
                    "expected_at": expected.expected_at.astimezone(UTC).isoformat(),
                    "deadline_at": expected.deadline_at.astimezone(UTC).isoformat(),
                    "deadline_state": deadline_state,
                    "slo_30d": {
                        "good": market_good,
                        "bad": market_bad,
                        "missing": market_missing,
                        "pending": market_pending,
                        "expected": len(due_windows),
                        "violations": market_violations,
                        "ratio": (
                            market_good / len(due_windows) if due_windows else None
                        ),
                        "error_rate": market_error_rate,
                        "burn_rate": market_burn,
                        "error_budget_consumed": market_burn,
                    },
                }
            )

        market_candidates: dict[
            str, list[tuple[tuple[datetime, int], dict[str, Any], dict[str, Any]]]
        ] = {}
        legacy_all_runs: list[dict[str, Any]] = []
        enabled_market_set = set(scope["enabled_markets"])
        global_activation = max(
            _parse_timestamp(epochs[market]) for market in enabled_market_set
        )
        for record in safe_run_records:
            if record["job"] not in {
                "stock-scan:US",
                "stock-scan:HK",
                "stock-scan:ALL",
            }:
                continue
            market = record["job"].rsplit(":", 1)[1]
            detail = record["detail"]
            if detail is None:
                raise CorruptProtectionStateError(
                    "persisted run evidence is corrupt"
                )
            run_activation: datetime | None
            if market == "ALL":
                run_activation = None
            elif market in enabled_market_set:
                run_activation = _parse_timestamp(epochs[market])
            else:
                continue
            finished_at = _parse_timestamp(record["finished_at"])
            if run_activation is not None and finished_at < run_activation:
                continue
            candidate_key = (finished_at, record["id"])
            if market == "ALL":
                by_market = detail["by_market"]
                if by_market is None:
                    if finished_at >= global_activation:
                        legacy_all_runs.append(record)
                else:
                    for current_market in enabled_market_set:
                        market_activation = _parse_timestamp(
                            epochs[current_market]
                        )
                        if (
                            finished_at >= market_activation
                            and current_market not in by_market
                        ):
                            raise CorruptProtectionStateError(
                                "persisted run market slices are corrupt"
                            )
                    for slice_market, market_slice in by_market.items():
                        if slice_market not in enabled_market_set or finished_at < (
                            _parse_timestamp(epochs[slice_market])
                        ):
                            continue
                        market_candidates.setdefault(slice_market, []).append(
                            (candidate_key, record, market_slice)
                        )
            else:
                market_candidates.setdefault(market, []).append(
                    (candidate_key, record, detail)
                )
            recent_runs.append(
                {
                    "job": record["job"],
                    "market": market,
                    "status": record["status"],
                    "started_at": record["started_at"],
                    "finished_at": record["finished_at"],
                    "selected": detail["selected"],
                    "evaluated": detail["evaluated"],
                    "notified": detail["notified"],
                }
            )

        def project_market_candidates(
            candidates: Mapping[
                str,
                tuple[tuple[datetime, int], dict[str, Any], dict[str, Any]],
            ],
        ) -> tuple[dict[str, Any], dict[str, Any]] | None:
            if set(candidates) != enabled_market_set:
                return None
            fresh_enabled = 0
            fresh_usable = 0
            trusted_enabled = 0
            trusted_usable = 0
            fresh_affected: set[str] = set()
            trusted_affected: set[str] = set()
            for market, (_key, record, market_detail) in candidates.items():
                fresh = market_detail["fresh_data"]
                trusted = market_detail["trusted_decision"]
                configured_tickers = {
                    ticker
                    for ticker, configured_market in configured.items()
                    if configured_market == market
                }
                snapshot = all_states.get(f"market:{market}")
                finished_at = _parse_timestamp(record["finished_at"])
                if snapshot is None or finished_at < snapshot.updated_at:
                    return None
                if (
                    market_detail["selected"] != len(configured_tickers)
                    or not set(fresh["affected"]).issubset(configured_tickers)
                    or not set(trusted["affected"]).issubset(configured_tickers)
                    or trusted["enabled"]
                    != snapshot.coverage.enabled_instruments
                    or trusted["usable"]
                    != snapshot.coverage.usable_instruments
                    or set(trusted["affected"])
                    != set(snapshot.coverage.unusable_tickers)
                ):
                    raise CorruptProtectionStateError(
                        "persisted run coverage is corrupt"
                    )
                fresh_enabled += fresh["enabled"]
                fresh_usable += fresh["usable"]
                trusted_enabled += trusted["enabled"]
                trusted_usable += trusted["usable"]
                fresh_affected.update(fresh["affected"])
                trusted_affected.update(trusted["affected"])
            return (
                {
                    "enabled": fresh_enabled,
                    "usable": fresh_usable,
                    "ratio": fresh_usable / fresh_enabled if fresh_enabled else None,
                    "affected": sorted(fresh_affected),
                    "known": True,
                },
                {
                    "enabled": trusted_enabled,
                    "usable": trusted_usable,
                    "ratio": (
                        trusted_usable / trusted_enabled
                        if trusted_enabled
                        else None
                    ),
                    "affected": sorted(trusted_affected),
                    "known": True,
                },
            )

        latest_market_candidates = {
            market: max(candidates, key=lambda item: item[0])
            for market, candidates in market_candidates.items()
        }
        market_projection = project_market_candidates(latest_market_candidates)
        legacy_all_run = max(
            legacy_all_runs,
            key=lambda record: (
                _parse_timestamp(record["finished_at"]),
                record["id"],
            ),
            default=None,
        )
        legacy_projection: tuple[dict[str, Any], dict[str, Any]] | None = None
        if legacy_all_run is not None:
            detail = legacy_all_run["detail"]
            assert detail is not None
            fresh = detail["fresh_data"]
            trusted = detail["trusted_decision"]
            global_snapshot = all_states.get("global")
            finished_at = _parse_timestamp(legacy_all_run["finished_at"])
            if (
                global_snapshot is not None
                and finished_at >= global_snapshot.updated_at
            ):
                if (
                    detail["selected"] != enabled
                    or not set(fresh["affected"]).issubset(configured)
                    or not set(trusted["affected"]).issubset(configured)
                    or trusted["enabled"]
                    != global_snapshot.coverage.enabled_instruments
                    or trusted["usable"]
                    != global_snapshot.coverage.usable_instruments
                    or set(trusted["affected"])
                    != set(global_snapshot.coverage.unusable_tickers)
                ):
                    raise CorruptProtectionStateError(
                        "persisted run coverage is corrupt"
                    )
                legacy_projection = (dict(fresh), dict(trusted))

        if legacy_all_run is None:
            if market_projection is not None:
                projected_fresh_data, projected_trusted_decision = market_projection
        else:
            legacy_key = (
                _parse_timestamp(legacy_all_run["finished_at"]),
                legacy_all_run["id"],
            )
            candidate_keys = {
                market: candidate[0]
                for market, candidate in latest_market_candidates.items()
            }
            if (
                market_projection is not None
                and set(candidate_keys) == enabled_market_set
                and all(key > legacy_key for key in candidate_keys.values())
            ):
                projected_fresh_data, projected_trusted_decision = market_projection
            elif not any(key > legacy_key for key in candidate_keys.values()):
                if legacy_projection is not None:
                    projected_fresh_data, projected_trusted_decision = (
                        legacy_projection
                    )
    except CorruptProtectionStateError:
        return _corrupt_receipt(generated_at=now, enabled=enabled)

    if enabled == 0:
        return _receipt(
            generated_at=now,
            state=ProtectionState.UNCONFIGURED,
            reasons=("unconfigured",),
            enabled=0,
            usable=0,
            affected=(),
            schedule=schedule,
            delivery_states=delivery_states,
            provider_capabilities=provider_capabilities,
            watchdog=watchdog_projection,
        )
    if due_gap:
        return _receipt(
            generated_at=now,
            state=ProtectionState.BLIND,
            reasons=("deadline_missed",),
            enabled=enabled,
            usable=0,
            affected=tuple(sorted(configured)),
            schedule=schedule,
            delivery_states=delivery_states,
            provider_capabilities=provider_capabilities,
            watchdog=watchdog_projection,
        )
    if coverage_mismatch:
        return _receipt(
            generated_at=now,
            state=ProtectionState.BLIND,
            reasons=("scope_coverage_mismatch",),
            enabled=enabled,
            usable=0,
            affected=tuple(sorted(configured)),
            schedule=schedule,
            delivery_states=delivery_states,
            provider_capabilities=provider_capabilities,
            watchdog=watchdog_projection,
        )
    if unresolved_bad:
        return _receipt(
            generated_at=now,
            state=ProtectionState.BLIND,
            reasons=("deadline_evidence_unresolved",),
            enabled=enabled,
            usable=0,
            affected=tuple(sorted(configured)),
            schedule=schedule,
            delivery_states=delivery_states,
            provider_capabilities=provider_capabilities,
            watchdog=watchdog_projection,
        )

    candidate_states = [item.state for item in snapshots]
    if missing_baseline or configuration_baseline_missing:
        candidate_states.append(ProtectionState.RECOVERING)
    state = max(candidate_states, key=_STATE_PRIORITY.__getitem__)
    usable = min(enabled, sum(item.coverage.usable_instruments for item in snapshots))
    affected = tuple(
        sorted(
            {
                ticker
                for item in snapshots
                for ticker in item.coverage.unusable_tickers
            }
            | missing_tickers
        )
    )
    reasons = tuple(reason for item in snapshots for reason in item.reason_codes)
    if missing_baseline:
        reasons += ("protection_baseline_missing",)
    reasons += tuple(identity_reasons)
    return _receipt(
        generated_at=now,
        state=state,
        reasons=reasons,
        enabled=enabled,
        usable=usable,
        affected=affected,
        schedule=schedule,
        delivery_states=delivery_states,
        provider_capabilities=provider_capabilities,
        fresh_data=projected_fresh_data,
        trusted_decision=projected_trusted_decision,
        recent_runs=recent_runs[:recent_run_limit],
        watchdog=watchdog_projection,
    )
