"""SQLite-backed notification state and audit history.

The store deliberately separates deciding whether to notify from marking a
successful notification.  A failed send therefore leaves ``last_sent_at``
unset and is eligible for retry on the next scan.
"""

from __future__ import annotations

import json
import hashlib
import math
import secrets
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator, Literal
from zoneinfo import ZoneInfo

from reliability import ProviderKey, RuntimeState

from .blindness import (
    BlindnessObservation,
    ProtectionSnapshot,
    ProtectionTransition,
    protection_observation_identity,
    transition_protection,
)

_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}
_SUPPORTED_PROTECTION_SCOPES = {"global", "market:US", "market:HK"}

_PROVIDER_RUNTIME_MAX_KEYS = 256
_PROVIDER_RUNTIME_MAX_OBSERVATIONS = 50_000
_PROVIDER_RUNTIME_MAX_CACHES = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_state (
    signal_key TEXT PRIMARY KEY,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    fingerprint TEXT,
    activated_at TEXT,
    last_seen_at TEXT NOT NULL,
    last_sent_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    active INTEGER CHECK (active IS NULL OR active IN (0, 1)),
    fingerprint TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (signal_key) REFERENCES signal_state(signal_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signal_events_key_time
    ON signal_events(signal_key, occurred_at DESC);

CREATE TABLE IF NOT EXISTS news_seen (
    fingerprint TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_claims (
    claim_key TEXT PRIMARY KEY,
    business_key TEXT NOT NULL,
    claim_token TEXT NOT NULL UNIQUE,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_claims_business_token
    ON notification_claims(business_key, claim_token);

CREATE INDEX IF NOT EXISTS idx_notification_claims_expiry
    ON notification_claims(expires_at);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_log_job_finished
    ON run_log(job, finished_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS run_log_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_state (
    scope_key TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_observations (
    scope_key TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (scope_key, observation_id)
);

CREATE TABLE IF NOT EXISTS protection_observation_collisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    original_sha256 TEXT NOT NULL,
    conflicting_sha256 TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE (scope_key, observation_id, conflicting_sha256)
);

CREATE INDEX IF NOT EXISTS idx_protection_observation_collisions_scope_time
    ON protection_observation_collisions(scope_key, detected_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS protection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_state TEXT,
    current_state TEXT NOT NULL,
    incident_id TEXT,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    notified_at TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (delivery_status IN ('pending', 'sent', 'suppressed'))
);

CREATE INDEX IF NOT EXISTS idx_protection_events_scope_time
    ON protection_events(scope_key, occurred_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS protection_state_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_event_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_scope (
    scope_key TEXT PRIMARY KEY,
    activated_at TEXT NOT NULL,
    enabled_markets_json TEXT NOT NULL,
    market_epochs_json TEXT NOT NULL DEFAULT '{}',
    market_instrument_hashes_json TEXT NOT NULL DEFAULT '{}',
    market_contract_hashes_json TEXT NOT NULL DEFAULT '{}',
    watchdog_generation TEXT,
    paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_scope_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    generation TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    superseded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_protection_scope_generations_time
    ON protection_scope_generations(scope_key, activated_at, generation);

CREATE TABLE IF NOT EXISTS protection_scope_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_windows (
    window_key TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    expected_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'good', 'bad')),
    actual_at TEXT,
    last_success_at TEXT,
    enabled_instruments INTEGER NOT NULL CHECK (enabled_instruments >= 0),
    usable_instruments INTEGER NOT NULL CHECK (usable_instruments >= 0),
    coverage_ratio REAL,
    affected_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_protection_windows_deadline
    ON protection_windows(deadline_at DESC, window_key);

CREATE TABLE IF NOT EXISTS delivery_state (
    channel TEXT PRIMARY KEY,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    configured INTEGER NOT NULL CHECK (configured IN (0, 1)),
    mode TEXT NOT NULL,
    config_fingerprint TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_deliveries (
    business_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent')),
    last_attempt_at TEXT,
    sent_at TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (business_key, channel)
);

CREATE INDEX IF NOT EXISTS idx_outbound_deliveries_status_time
    ON outbound_deliveries(status, updated_at DESC, business_key, channel);

CREATE TABLE IF NOT EXISTS provider_runtime_state (
    scope_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_runtime_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integrity_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    component TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation > 0),
    reason_code TEXT NOT NULL,
    evidence_sha256 TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    delivery_kind TEXT NOT NULL
        CHECK (delivery_kind IN ('detected', 'activation_sync')),
    delivery_status TEXT NOT NULL
        CHECK (delivery_status IN ('pending', 'sent', 'suppressed')),
    notified_at TEXT,
    UNIQUE (scope_key, component, generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_integrity_incidents_active
    ON integrity_incidents(scope_key, component)
    WHERE active = 1;

CREATE INDEX IF NOT EXISTS idx_integrity_incidents_scope_time
    ON integrity_incidents(scope_key, last_seen_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS watchdog_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation INTEGER NOT NULL UNIQUE CHECK (generation > 0),
    state TEXT NOT NULL CHECK (state IN ('BLIND', 'RECOVERED')),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    evidence_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    delivery_kind TEXT NOT NULL
        CHECK (delivery_kind IN ('detected', 'activation_sync', 'recovery')),
    delivery_status TEXT NOT NULL
        CHECK (delivery_status IN ('pending', 'sent', 'suppressed')),
    detected_notified_at TEXT,
    notified_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_watchdog_incidents_active
    ON watchdog_incidents(active)
    WHERE active = 1;

CREATE INDEX IF NOT EXISTS idx_watchdog_incidents_generation
    ON watchdog_incidents(generation DESC, id DESC);
"""


def _normalise_time(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _timestamp(value: datetime | None) -> str:
    return _normalise_time(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_persisted_aware_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("persisted timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _prepare_signal_observation(
    key: str,
    active: bool | None,
    fingerprint: str,
    cooldown_hours: float,
    now: datetime | None,
) -> tuple[str, float, datetime, str] | None:
    signal_key = _require_text(key, "key")
    if active is not None and not isinstance(active, bool):
        raise TypeError("active must be bool or None")
    if active is None:
        return None
    if not isinstance(fingerprint, str):
        raise TypeError("fingerprint must be a string")
    try:
        cooldown = float(cooldown_hours)
    except (TypeError, ValueError) as exc:
        raise ValueError("cooldown_hours must be a finite non-negative number") from exc
    if not math.isfinite(cooldown) or cooldown < 0:
        raise ValueError("cooldown_hours must be a finite non-negative number")
    observed_at = _normalise_time(now)
    return signal_key, cooldown, observed_at, _timestamp(observed_at)


def _lease_duration(lease_seconds: float) -> timedelta:
    try:
        seconds = float(lease_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("lease_seconds must be a finite positive number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("lease_seconds must be a finite positive number")
    return timedelta(seconds=seconds)


def _json_payload(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _sqlite_quarantine_envelope(row: Mapping[str, Any]) -> str:
    """Encode arbitrary SQLite storage classes without lossy coercion."""

    fields: dict[str, dict[str, Any]] = {}
    for key in sorted(row):
        value = row[key]
        encoded: dict[str, Any]
        if value is None:
            encoded = {"type": "null", "value": None}
        elif type(value) is int:
            encoded = {"type": "integer", "value": value}
        elif type(value) is float:
            encoded = {"type": "real", "value": value.hex()}
        elif isinstance(value, str):
            encoded = {"type": "text", "value": value}
        elif isinstance(value, (bytes, bytearray, memoryview)):
            encoded = {"type": "blob", "value": bytes(value).hex()}
        else:  # pragma: no cover - SQLite exposes only the classes above
            raise TypeError("unsupported SQLite storage class")
        fields[key] = encoded
    return _json_payload({"version": 1, "fields": fields})


def _low_cardinality_code(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    code = _require_text(value, label).strip().lower()
    if len(code) > 64 or not all(
        character.isalnum() or character in "_.:-" for character in code
    ):
        raise ValueError(f"{label} must be a low-cardinality identifier")
    return code


def _optional_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("evidence_sha256 must be text")
    digest = value.strip().lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("evidence_sha256 must be a lowercase SHA-256 digest")
    return digest


def _outbound_business_key(value: str) -> str:
    key = _require_text(value, "business_key").strip()
    if len(key) > 240 or not all(
        character.isascii()
        and (character.isalnum() or character in "_.:-")
        for character in key
    ):
        raise ValueError("business_key must be a stable ASCII identifier")
    return key


def _outbound_claim_key(business_key: str, channel: str) -> str:
    identity = f"{business_key}\0{channel}".encode("ascii")
    return "outbound:" + hashlib.sha256(identity).hexdigest()


def _watchdog_incident_payload(
    *,
    scope_generation: str,
    enabled_instruments: int,
    affected_tickers: Sequence[str],
    markets: Sequence[str],
    window_keys: Sequence[str],
    first_seen_at: datetime,
) -> tuple[str, str, dict[str, Any]]:
    """Build one canonical, low-cardinality watchdog incident payload."""

    digest = _optional_sha256(scope_generation)
    if digest is None:
        raise ValueError("scope_generation is required")
    if (
        isinstance(enabled_instruments, bool)
        or not isinstance(enabled_instruments, int)
        or enabled_instruments <= 0
    ):
        raise ValueError("enabled_instruments must be a positive integer")
    if isinstance(affected_tickers, (str, bytes)):
        raise TypeError("affected_tickers must be a sequence")
    affected = tuple(affected_tickers)
    instrument_set_hash(affected)
    if affected != tuple(sorted(affected)) or len(affected) > enabled_instruments:
        raise ValueError("affected_tickers must be sorted within enabled coverage")
    if isinstance(markets, (str, bytes)):
        raise TypeError("markets must be a sequence")
    normalized_markets = tuple(markets)
    if (
        not normalized_markets
        or normalized_markets != tuple(sorted(set(normalized_markets)))
        or any(market not in _MARKET_TIMEZONES for market in normalized_markets)
    ):
        raise ValueError("markets must be sorted unique US/HK identifiers")
    if isinstance(window_keys, (str, bytes)):
        raise TypeError("window_keys must be a sequence")
    normalized_keys = tuple(window_keys)
    if (
        not normalized_keys
        or normalized_keys != tuple(sorted(set(normalized_keys)))
    ):
        raise ValueError("window_keys must be sorted and unique")
    key_markets: set[str] = set()
    for key in normalized_keys:
        if not isinstance(key, str) or key.count(":") != 1:
            raise ValueError("watchdog window key is invalid")
        market, session_date = key.split(":", 1)
        if market not in _MARKET_TIMEZONES:
            raise ValueError("watchdog window market is invalid")
        try:
            parsed_session = datetime.fromisoformat(session_date)
        except ValueError:
            raise ValueError("watchdog session date is invalid") from None
        if parsed_session.time() != datetime.min.time() or (
            parsed_session.date().isoformat() != session_date
        ):
            raise ValueError("watchdog session date is not canonical")
        key_markets.add(market)
    if key_markets != set(normalized_markets):
        raise ValueError("watchdog markets do not match window keys")
    if first_seen_at.tzinfo is None or first_seen_at.utcoffset() is None:
        raise ValueError("first_seen_at must be timezone-aware")
    first_seen = first_seen_at.astimezone(timezone.utc)
    payload = {
        "version": 1,
        "scope_generation": digest,
        "enabled_instruments": enabled_instruments,
        "affected_tickers": list(affected),
        "markets": list(normalized_markets),
        "window_keys": list(normalized_keys),
        "first_seen_at": _timestamp(first_seen),
        "reason_codes": ["expected_window_missing"],
    }
    encoded = _json_payload(payload)
    return encoded, hashlib.sha256(encoded.encode()).hexdigest(), payload


def _validated_watchdog_incident(
    row: Mapping[str, Any],
    *,
    not_after: datetime | None = None,
) -> dict[str, Any]:
    try:
        incident_id = row["id"]
        generation = row["generation"]
        state = row["state"]
        active_raw = row["active"]
        evidence_sha256 = row["evidence_sha256"]
        raw_payload = row["payload_json"]
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id <= 0
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
            or state not in {"BLIND", "RECOVERED"}
            or type(active_raw) is not int
            or active_raw not in {0, 1}
            or not isinstance(raw_payload, str)
        ):
            raise ValueError("watchdog incident identity is invalid")
        persisted_digest = _optional_sha256(evidence_sha256)
        if persisted_digest is None:
            raise ValueError("watchdog incident digest is missing")
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "scope_generation",
            "enabled_instruments",
            "affected_tickers",
            "markets",
            "window_keys",
            "first_seen_at",
            "reason_codes",
        }:
            raise ValueError("watchdog incident payload has invalid shape")
        if payload["version"] != 1 or payload["reason_codes"] != [
            "expected_window_missing"
        ]:
            raise ValueError("watchdog incident payload version is invalid")
        first_seen_payload = _parse_persisted_aware_timestamp(
            payload["first_seen_at"]
        )
        encoded, expected_digest, canonical_payload = _watchdog_incident_payload(
            scope_generation=payload["scope_generation"],
            enabled_instruments=payload["enabled_instruments"],
            affected_tickers=payload["affected_tickers"],
            markets=payload["markets"],
            window_keys=payload["window_keys"],
            first_seen_at=first_seen_payload,
        )
        if encoded != raw_payload or expected_digest != persisted_digest:
            raise ValueError("watchdog incident payload digest is inconsistent")
        first_seen = _parse_persisted_aware_timestamp(row["first_seen_at"])
        last_seen = _parse_persisted_aware_timestamp(row["last_seen_at"])
        resolved = (
            _parse_persisted_aware_timestamp(row["resolved_at"])
            if row.get("resolved_at") is not None
            else None
        )
        detected_notified = (
            _parse_persisted_aware_timestamp(row["detected_notified_at"])
            if row.get("detected_notified_at") is not None
            else None
        )
        notified = (
            _parse_persisted_aware_timestamp(row["notified_at"])
            if row.get("notified_at") is not None
            else None
        )
        if first_seen != first_seen_payload or first_seen > last_seen:
            raise ValueError("watchdog incident timestamps are inconsistent")
        active = bool(active_raw)
        if not (
            active == (state == "BLIND")
            and active == (resolved is None)
        ):
            raise ValueError("watchdog lifecycle state is inconsistent")
        if resolved is not None and resolved < last_seen:
            raise ValueError("watchdog resolution predates its evidence")
        if detected_notified is not None and (
            detected_notified < first_seen
            or (resolved is not None and detected_notified > resolved)
        ):
            raise ValueError("watchdog detection notification is out of order")
        delivery_kind = row["delivery_kind"]
        if delivery_kind not in {"detected", "activation_sync", "recovery"}:
            raise ValueError("watchdog delivery kind is invalid")
        if (state == "RECOVERED") != (delivery_kind == "recovery"):
            raise ValueError("watchdog delivery kind does not match lifecycle")
        delivery_status = row["delivery_status"]
        if delivery_status not in {"pending", "sent", "suppressed"} or (
            (delivery_status == "sent") != (notified is not None)
        ):
            raise ValueError("watchdog delivery evidence is inconsistent")
        if state == "BLIND" and detected_notified is not None:
            raise ValueError("active watchdog incident cannot archive detection")
        if not_after is not None:
            if not_after.tzinfo is None or not_after.utcoffset() is None:
                raise ValueError("not_after must be timezone-aware")
            cutoff = not_after.astimezone(timezone.utc)
            if any(
                value is not None and value > cutoff
                for value in (
                    first_seen,
                    last_seen,
                    resolved,
                    detected_notified,
                    notified,
                )
            ):
                raise ValueError("watchdog incident evidence is future-dated")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise CorruptProtectionStateError(
            "persisted watchdog incident evidence is corrupt"
        ) from None
    return {
        "id": incident_id,
        "generation": generation,
        "state": state,
        "active": active,
        "evidence_sha256": persisted_digest,
        "payload": canonical_payload,
        "first_seen_at": _timestamp(first_seen),
        "last_seen_at": _timestamp(last_seen),
        "resolved_at": _timestamp(resolved) if resolved is not None else None,
        "delivery_kind": delivery_kind,
        "delivery_status": delivery_status,
        "detected_notified_at": (
            _timestamp(detected_notified)
            if detected_notified is not None
            else None
        ),
        "notified_at": _timestamp(notified) if notified is not None else None,
    }


class CorruptProtectionStateError(RuntimeError):
    """Raised when persisted safety evidence cannot be validated."""


class ProtectionObservationCollisionError(CorruptProtectionStateError):
    """Raised after a reused observation id with different evidence is sealed RED."""


def instrument_set_hash(tickers: Sequence[str]) -> str:
    """Return a secret-free identity digest for one market's enabled tickers."""

    if isinstance(tickers, (str, bytes)):
        raise TypeError("ticker identities must be a sequence, not text")
    canonical: list[str] = []
    for ticker in tickers:
        if (
            not isinstance(ticker, str)
            or not ticker
            or ticker != ticker.strip()
            or not ticker.isascii()
            or len(ticker) > 64
            or not ticker[0].isalnum()
            or not all(character.isalnum() or character in "._-" for character in ticker)
        ):
            raise ValueError("ticker must be a canonical identifier")
        canonical.append(ticker)
    if not canonical:
        raise ValueError("an enabled market requires at least one ticker identity")
    normalized = sorted(set(canonical))
    if len(normalized) != len(canonical):
        raise ValueError("ticker identities must be unique")
    payload = _json_payload(normalized)
    return hashlib.sha256(payload.encode()).hexdigest()


def _market_digest_map(
    values: Mapping[str, str],
    markets: Sequence[str],
    *,
    label: str,
) -> dict[str, str]:
    if set(values) != set(markets):
        raise ValueError(f"{label} markets must match enabled markets")
    result: dict[str, str] = {}
    for market in markets:
        digest = values[market]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"{label} must contain lowercase SHA-256 digests")
        result[market] = digest
    return result


def watchdog_scope_generation(scope_record: Mapping[str, Any]) -> str:
    """Return the domain-separated generation for one validated scope."""

    try:
        payload = {
            "version": 1,
            "enabled_markets": scope_record["enabled_markets"],
            "market_epochs": scope_record["market_epochs"],
            "market_instrument_hashes": scope_record[
                "market_instrument_hashes"
            ],
            "market_contract_hashes": scope_record[
                "market_contract_hashes"
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("scope generation evidence is invalid") from None
    return hashlib.sha256(
        b"alpha-guard:watchdog-scope:v1\x00" + encoded.encode()
    ).hexdigest()


def _validated_scope_row(
    row: Mapping[str, Any],
    *,
    expected_scope: str,
) -> dict[str, Any]:
    if expected_scope not in _SUPPORTED_PROTECTION_SCOPES:
        raise ValueError("scope row key is unsupported")
    if row.get("scope_key") != expected_scope:
        raise ValueError("scope row key mismatch")
    markets_raw = json.loads(row["enabled_markets_json"])
    epochs_raw = json.loads(row["market_epochs_json"])
    hashes_raw = json.loads(row["market_instrument_hashes_json"])
    contracts_raw = json.loads(row["market_contract_hashes_json"])
    if (
        not isinstance(markets_raw, list)
        or not isinstance(epochs_raw, dict)
        or not isinstance(hashes_raw, dict)
        or not isinstance(contracts_raw, dict)
    ):
        raise ValueError("scope market evidence has invalid shape")
    markets: list[str] = []
    for market in markets_raw:
        if (
            not isinstance(market, str)
            or not market
            or len(market) > 16
            or market != market.strip().upper()
            or not all(character.isalnum() or character in "_.-" for character in market)
        ):
            raise ValueError("scope market is invalid")
        markets.append(market)
    if markets != sorted(set(markets)):
        raise ValueError("scope markets must be sorted and unique")
    if set(epochs_raw) != set(markets):
        raise ValueError("scope market epochs do not match enabled markets")
    if not set(hashes_raw).issubset(markets):
        raise ValueError("scope instrument identities do not match enabled markets")
    if not set(contracts_raw).issubset(markets):
        raise ValueError("scope contracts do not match enabled markets")
    hashes: dict[str, str] = {}
    for market, digest in hashes_raw.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("scope instrument identity is invalid")
        hashes[market] = digest
    contracts: dict[str, str] = {}
    for market, digest in contracts_raw.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("scope contract identity is invalid")
        contracts[market] = digest
    epochs = {
        market: _parse_persisted_aware_timestamp(epochs_raw[market])
        for market in markets
    }
    activated_at = _parse_persisted_aware_timestamp(row["activated_at"])
    updated_at = _parse_persisted_aware_timestamp(row["updated_at"])
    if epochs and activated_at != min(epochs.values()):
        raise ValueError("scope activation does not match market epochs")
    if activated_at > updated_at or any(epoch > updated_at for epoch in epochs.values()):
        raise ValueError("scope evidence is future-dated relative to its update")
    paused_raw = row["paused"]
    if paused_raw not in (0, 1):
        raise ValueError("scope paused flag is invalid")
    validated = {
        "scope": expected_scope,
        "activated_at": _timestamp(activated_at),
        "enabled_markets": markets,
        "market_epochs": {
            market: _timestamp(epoch) for market, epoch in epochs.items()
        },
        "market_instrument_hashes": hashes,
        "market_contract_hashes": contracts,
        "paused": bool(paused_raw),
        "updated_at": _timestamp(updated_at),
    }
    persisted_generation = _optional_sha256(row.get("watchdog_generation"))
    if (
        persisted_generation is not None
        and persisted_generation != watchdog_scope_generation(validated)
    ):
        raise ValueError("scope watchdog generation is inconsistent")
    validated["watchdog_generation"] = persisted_generation
    return validated


def _validated_scope_generation_row(
    row: Mapping[str, Any],
    *,
    not_after: datetime | None = None,
) -> dict[str, Any]:
    try:
        generation_id = row["id"]
        scope_key = _require_text(row["scope_key"], "scope_key")
        if (
            len(scope_key) > 80
            or not scope_key[0].isascii()
            or not scope_key[0].isalnum()
            or not all(
                character.isascii()
                and (character.isalnum() or character in "._:-")
                for character in scope_key
            )
        ):
            raise ValueError("scope generation key is invalid")
        generation = _optional_sha256(row["generation"])
        if generation is None:
            raise ValueError("scope generation is missing")
        activated_at = _parse_persisted_aware_timestamp(row["activated_at"])
        superseded_at = (
            _parse_persisted_aware_timestamp(row["superseded_at"])
            if row.get("superseded_at") is not None
            else None
        )
        if (
            isinstance(generation_id, bool)
            or not isinstance(generation_id, int)
            or generation_id <= 0
            or (superseded_at is not None and superseded_at < activated_at)
        ):
            raise ValueError("scope generation chronology is invalid")
        if not_after is not None:
            cutoff = _normalise_time(not_after)
            if activated_at > cutoff or (
                superseded_at is not None and superseded_at > cutoff
            ):
                raise ValueError("scope generation is future-dated")
    except (KeyError, TypeError, ValueError):
        raise CorruptProtectionStateError(
            "persisted protection scope generation is corrupt"
        ) from None
    return {
        "id": generation_id,
        "scope": scope_key,
        "generation": generation,
        "activated_at": _timestamp(activated_at),
        "superseded_at": (
            _timestamp(superseded_at) if superseded_at is not None else None
        ),
    }


def _validated_protection_window(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        item = dict(row)
        if not isinstance(item["window_key"], str):
            raise ValueError("window_key must be text")
        if not isinstance(item["market"], str):
            raise ValueError("market must be text")
        key = _require_text(item["window_key"], "window_key")
        market = _require_text(item["market"], "market")
        status = item["status"]
        expected = _parse_persisted_aware_timestamp(item["expected_at"])
        deadline = _parse_persisted_aware_timestamp(item["deadline_at"])
        actual = (
            _parse_persisted_aware_timestamp(item["actual_at"])
            if item.get("actual_at") is not None
            else None
        )
        success = (
            _parse_persisted_aware_timestamp(item["last_success_at"])
            if item.get("last_success_at") is not None
            else None
        )
        updated = _parse_persisted_aware_timestamp(item["updated_at"])
        enabled = item["enabled_instruments"]
        usable = item["usable_instruments"]
        ratio = item["coverage_ratio"]
        affected = json.loads(item["affected_json"])
        reasons = json.loads(item["reasons_json"])
        if (
            market not in {"US", "HK"}
            or status not in {"pending", "good", "bad"}
        ):
            raise ValueError("invalid protection window identity")
        if deadline < expected:
            raise ValueError("window deadline predates expected time")
        expected_key = (
            f"{market}:"
            f"{expected.astimezone(_MARKET_TIMEZONES[market]).date().isoformat()}"
        )
        if key != expected_key:
            raise ValueError("window key does not match market session date")
        if (
            isinstance(enabled, bool)
            or not isinstance(enabled, int)
            or isinstance(usable, bool)
            or not isinstance(usable, int)
            or enabled < 0
            or not 0 <= usable <= enabled
        ):
            raise ValueError("window coverage counts are inconsistent")
        expected_ratio = usable / enabled if enabled else None
        if expected_ratio is None:
            if ratio is not None:
                raise ValueError("zero-enabled window must have null ratio")
        elif (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(float(ratio))
            or not math.isclose(float(ratio), expected_ratio, abs_tol=1e-12)
        ):
            raise ValueError("window coverage ratio is inconsistent")
        if not isinstance(affected, list) or not isinstance(reasons, list):
            raise ValueError("window impact evidence must be arrays")
        if any(
            not isinstance(ticker, str)
            or not ticker.strip()
            or len(ticker) > 64
            for ticker in affected
        ):
            raise ValueError("window affected ticker is invalid")
        if len(affected) != len(set(affected)) or len(affected) > enabled - usable:
            raise ValueError("window affected tickers are inconsistent")
        validated_reasons = [
            _low_cardinality_code(reason, "reason_code") for reason in reasons
        ]
        if any(reason is None for reason in validated_reasons):
            raise ValueError("window reason is invalid")
        if len(validated_reasons) != len(set(validated_reasons)):
            raise ValueError("window reasons must be unique")
        if actual is not None and actual > updated:
            raise ValueError("window actual time exceeds update time")
        if success is not None and success > updated:
            raise ValueError("window success time exceeds update time")
        if status == "good":
            if not (
                enabled > 0
                and usable == enabled
                and float(ratio) == 1.0
                and actual is not None
                and expected <= actual <= deadline
                and success == actual
                and not affected
            ):
                raise ValueError("good window invariants are violated")
        elif success is not None:
            raise ValueError("non-good window cannot claim success")
        if status == "pending" and updated > deadline:
            raise ValueError("past-deadline window cannot remain pending")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise CorruptProtectionStateError(
            "persisted protection window evidence is corrupt"
        ) from None
    return {
        "window_key": key,
        "market": market,
        "expected_at": _timestamp(expected),
        "deadline_at": _timestamp(deadline),
        "status": status,
        "actual_at": _timestamp(actual) if actual is not None else None,
        "last_success_at": _timestamp(success) if success is not None else None,
        "enabled_instruments": enabled,
        "usable_instruments": usable,
        "coverage_ratio": float(ratio) if ratio is not None else None,
        "affected": affected,
        "reasons": reasons,
        "updated_at": _timestamp(updated),
    }


def _validated_delivery_state(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        item = dict(row)
        channel = item["channel"]
        generation = item["generation"]
        configured = item["configured"]
        mode = item["mode"]
        if not isinstance(channel, str) or channel not in {
            "telegram",
            "whatsapp",
            "heartbeat",
        }:
            raise ValueError("delivery channel is invalid")
        if type(configured) is not int or configured not in {0, 1}:
            raise ValueError("delivery configured flag is invalid")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ValueError("delivery generation is invalid")
        if not isinstance(mode, str) or mode not in {"active", "preview"}:
            raise ValueError("delivery mode is invalid")
        raw_fingerprint = item.get("config_fingerprint")
        fingerprint = _optional_sha256(raw_fingerprint)
        if raw_fingerprint is not None and raw_fingerprint != fingerprint:
            raise ValueError("delivery fingerprint is not canonical")
        updated = _parse_persisted_aware_timestamp(item["updated_at"])
        attempt = (
            _parse_persisted_aware_timestamp(item["last_attempt_at"])
            if item.get("last_attempt_at") is not None
            else None
        )
        success_at = (
            _parse_persisted_aware_timestamp(item["last_success_at"])
            if item.get("last_success_at") is not None
            else None
        )
        raw_error = item.get("error_code")
        if raw_error is not None and not isinstance(raw_error, str):
            raise ValueError("delivery error code must be text")
        error = _low_cardinality_code(raw_error, "error_code")
        if raw_error is not None and raw_error != error:
            raise ValueError("delivery error code is not canonical")
        if attempt is not None and attempt > updated:
            raise ValueError("delivery attempt exceeds update time")
        if success_at is not None and (
            attempt is None or success_at > attempt or success_at > updated
        ):
            raise ValueError("delivery success chronology is invalid")
        if error is not None and attempt is None:
            raise ValueError("delivery error requires an attempt")
        if error is not None and success_at == attempt:
            raise ValueError("one delivery attempt cannot both succeed and fail")
        latest_succeeded = bool(
            attempt is not None and success_at == attempt and error is None
        )
    except (KeyError, TypeError, ValueError):
        raise CorruptProtectionStateError(
            "persisted delivery evidence is corrupt"
        ) from None
    return {
        "generation": generation,
        "configured": bool(configured),
        "mode": mode,
        "config_fingerprint": fingerprint,
        "last_attempt_at": _timestamp(attempt) if attempt is not None else None,
        "last_success_at": (
            _timestamp(success_at) if success_at is not None else None
        ),
        "success": latest_succeeded if attempt is not None else None,
        "error_code": error,
        "updated_at": _timestamp(updated),
    }


def _validated_outbound_delivery(
    row: Mapping[str, Any],
    *,
    not_after: datetime | None = None,
) -> dict[str, Any]:
    try:
        item = dict(row)
        business_key = _outbound_business_key(item["business_key"])
        channel = _low_cardinality_code(item["channel"], "channel")
        if channel not in {"telegram", "whatsapp"}:
            raise ValueError("outbound channel is invalid")
        fingerprint = _optional_sha256(item["config_fingerprint"])
        if fingerprint is None or fingerprint != item["config_fingerprint"]:
            raise ValueError("outbound fingerprint is invalid")
        status = item["status"]
        if status not in {"pending", "sent"}:
            raise ValueError("outbound status is invalid")
        updated = _parse_persisted_aware_timestamp(item["updated_at"])
        attempted = (
            _parse_persisted_aware_timestamp(item["last_attempt_at"])
            if item.get("last_attempt_at") is not None
            else None
        )
        sent_at = (
            _parse_persisted_aware_timestamp(item["sent_at"])
            if item.get("sent_at") is not None
            else None
        )
        raw_error = item.get("error_code")
        if raw_error is not None and not isinstance(raw_error, str):
            raise ValueError("outbound error must be text")
        error = _low_cardinality_code(raw_error, "error_code")
        if raw_error is not None and raw_error != error:
            raise ValueError("outbound error is not canonical")
        if attempted is not None and attempted > updated:
            raise ValueError("outbound attempt exceeds update time")
        if sent_at is not None and (
            attempted is None or sent_at != attempted or sent_at > updated
        ):
            raise ValueError("outbound sent chronology is invalid")
        if status == "sent" and (sent_at is None or error is not None):
            raise ValueError("sent outbound evidence is inconsistent")
        if status == "pending" and sent_at is not None:
            raise ValueError("pending outbound evidence cannot be sent")
        if error is not None and attempted is None:
            raise ValueError("outbound error requires an attempt")
        cutoff = _normalise_time(not_after) if not_after is not None else None
        if cutoff is not None and updated > cutoff:
            raise ValueError("outbound evidence is future-dated")
    except (KeyError, TypeError, ValueError):
        raise CorruptProtectionStateError(
            "persisted outbound delivery evidence is corrupt"
        ) from None
    return {
        "business_key": business_key,
        "channel": channel,
        "config_fingerprint": fingerprint,
        "status": status,
        "last_attempt_at": (
            _timestamp(attempted) if attempted is not None else None
        ),
        "sent_at": _timestamp(sent_at) if sent_at is not None else None,
        "error_code": error,
        "updated_at": _timestamp(updated),
    }


def _provider_key_from_storage_key(storage_key: Any) -> ProviderKey:
    if not isinstance(storage_key, str):
        raise ValueError("provider runtime storage key must be text")
    parts = storage_key.split(":")
    if len(parts) != 3:
        raise ValueError("provider runtime storage key is invalid")
    key = ProviderKey(
        provider=parts[0],
        operation=parts[1],
        market=parts[2],
    )
    if key.storage_key != storage_key:
        raise ValueError("provider runtime storage key is not canonical")
    return key


def _validated_provider_runtime_payload(
    payload: Mapping[str, Any],
    *,
    not_after: datetime | None = None,
) -> dict[str, Any]:
    """Validate every provider runtime identity and chronology boundary."""

    cutoff = _normalise_time(not_after) if not_after is not None else None
    try:
        state = RuntimeState.model_validate(payload)
        storage_keys = set(state.circuits) | set(state.observations)
        cache_provider_keys: set[str] = set()
        if len(state.caches) > _PROVIDER_RUNTIME_MAX_CACHES:
            raise ValueError("provider cache ledger exceeds safety limit")
        total_observations = sum(len(items) for items in state.observations.values())
        if total_observations > _PROVIDER_RUNTIME_MAX_OBSERVATIONS:
            raise ValueError("provider observation ledger exceeds safety limit")
        for storage_key in storage_keys:
            key = _provider_key_from_storage_key(storage_key)
            circuit = state.circuits.get(storage_key)
            if circuit is not None:
                circuit_is_open = circuit.state.value in {"open", "half_open"}
                if circuit_is_open != (circuit.opened_at is not None):
                    raise ValueError("provider circuit state is inconsistent")
                if (
                    cutoff is not None
                    and circuit.opened_at is not None
                    and circuit.opened_at > cutoff
                ):
                    raise ValueError("provider circuit is future-dated")
            attempts = state.observations.get(storage_key, ())
            if len(attempts) > 10_000:
                raise ValueError("provider observation window exceeds runtime limit")
            for attempt in attempts:
                if (
                    attempt.provider != key.provider
                    or attempt.operation != key.operation
                    or attempt.market != key.market
                ):
                    raise ValueError("provider attempt identity mismatches storage key")
                if cutoff is not None and attempt.observed_at > cutoff:
                    raise ValueError("provider attempt is future-dated")
                if attempt.error_type != attempt.failure_class:
                    raise ValueError("provider attempt error identity is inconsistent")
                if attempt.outcome == "success":
                    valid_attempt = (
                        attempt.failure_class == "none"
                        and attempt.cache_state.value == "miss"
                        and attempt.attempt_index >= 1
                        and attempt.circuit_state.value == "closed"
                    )
                elif attempt.outcome == "cache_hit":
                    valid_attempt = (
                        attempt.failure_class == "none"
                        and attempt.cache_state.value == "fresh"
                        and attempt.attempt_index == 0
                    )
                elif attempt.outcome == "stale_fallback":
                    valid_attempt = (
                        attempt.failure_class == "none"
                        and attempt.cache_state.value == "stale_if_error"
                        and attempt.attempt_index == 0
                    )
                elif attempt.outcome == "circuit_open":
                    valid_attempt = (
                        attempt.failure_class == "circuit_open"
                        and attempt.cache_state.value == "miss"
                        and attempt.attempt_index == 0
                        and attempt.circuit_state.value in {"open", "half_open"}
                    )
                else:
                    valid_attempt = (
                        attempt.failure_class not in {"none", "circuit_open"}
                        and attempt.cache_state.value == "miss"
                        and attempt.attempt_index >= 1
                    )
                if not valid_attempt:
                    raise ValueError("provider attempt outcome is inconsistent")
        for cache_key, cache in state.caches.items():
            if not isinstance(cache_key, str) or ":" not in cache_key:
                raise ValueError("provider cache key is invalid")
            provider_storage_key, digest = cache_key.rsplit(":", 1)
            _provider_key_from_storage_key(provider_storage_key)
            cache_provider_keys.add(provider_storage_key)
            if len(digest) != 24 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("provider cache key digest is invalid")
            if set(cache) != {"stored_at", "value"}:
                raise ValueError("provider cache evidence has invalid shape")
            stored_at_raw = cache["stored_at"]
            if not isinstance(stored_at_raw, str):
                raise ValueError("provider cache timestamp must be text")
            stored_at = _parse_persisted_aware_timestamp(stored_at_raw)
            if cutoff is not None and stored_at > cutoff:
                raise ValueError("provider cache evidence is future-dated")
        if len(storage_keys | cache_provider_keys) > _PROVIDER_RUNTIME_MAX_KEYS:
            raise ValueError("provider key ledger exceeds safety limit")
    except (KeyError, TypeError, ValueError):
        raise CorruptProtectionStateError(
            "persisted provider runtime evidence is corrupt"
        ) from None
    return state.model_dump(mode="json")


_STOCK_RUN_DETAIL_KEYS = {
    "selected",
    "evaluated",
    "notified",
    "error_tickers",
    "notification_error_tickers",
    "incident_attempted",
    "incident_notified",
    "incident_notification_error",
    "integrity_incident_attempted",
    "integrity_incident_notified",
    "integrity_notification_errors",
    "integrity_ledger_error",
    "telegram_probe_attempted",
    "telegram_probe_success",
    "telegram_probe_error",
    "protection_event_id",
    "protection_event_ids",
    "reliability",
}


def _canonical_run_job(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 80
        or not value[0].isascii()
        or not value[0].isalnum()
        or not all(
            character.isascii()
            and (character.isalnum() or character in "._:-")
            for character in value
        )
    ):
        raise ValueError("run job must be a canonical identifier")
    return value


def _validated_run_coverage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "enabled_instruments",
        "usable_instruments",
        "fresh_coverage",
        "unusable_tickers",
    }:
        raise ValueError("run coverage has invalid shape")
    enabled = value["enabled_instruments"]
    usable = value["usable_instruments"]
    ratio = value["fresh_coverage"]
    affected_raw = value["unusable_tickers"]
    if (
        type(enabled) is not int
        or type(usable) is not int
        or enabled < 0
        or usable < 0
        or usable > enabled
        or not isinstance(affected_raw, list)
    ):
        raise ValueError("run coverage counts are invalid")
    expected_ratio = usable / enabled if enabled else None
    if ratio is None:
        if expected_ratio is not None:
            raise ValueError("run coverage ratio is missing")
    elif (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not math.isfinite(float(ratio))
        or float(ratio) != expected_ratio
    ):
        raise ValueError("run coverage ratio is inconsistent")
    affected: list[str] = []
    for ticker in affected_raw:
        instrument_set_hash((ticker,))
        affected.append(ticker)
    if (
        len(set(affected)) != len(affected)
        or len(affected) != enabled - usable
    ):
        raise ValueError("run coverage affected instruments are invalid")
    if usable == enabled and affected:
        raise ValueError("full run coverage cannot list affected instruments")
    return {
        "enabled": enabled,
        "usable": usable,
        "ratio": float(ratio) if ratio is not None else None,
        "affected": sorted(affected),
        "known": True,
    }


def _validated_stock_run_detail(
    value: Any,
    *,
    status: str,
    job: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _STOCK_RUN_DETAIL_KEYS:
        raise ValueError("stock run detail contains unknown fields")
    selected = value["selected"]
    evaluated = value["evaluated"]
    notified = value["notified"]
    if (
        type(selected) is not int
        or type(evaluated) is not int
        or type(notified) is not int
        or selected < 0
        or evaluated < 0
        or notified < 0
        or evaluated > selected
        or notified > evaluated
    ):
        raise ValueError("stock run counts are invalid")
    reliability = value["reliability"]
    if not isinstance(reliability, dict):
        raise ValueError("stock run reliability detail is invalid")
    allowed_reliability = {
        "enabled_instruments",
        "usable_instruments",
        "fresh_coverage",
        "unusable_tickers",
        "coverage_semantics",
        "fresh_data_coverage",
        "trusted_decision_coverage",
        "provider_capability_failures",
        "by_market",
    }
    if not set(reliability).issubset(allowed_reliability) or not {
        "fresh_data_coverage",
        "trusted_decision_coverage",
    }.issubset(reliability):
        raise ValueError("stock run reliability fields are invalid")
    fresh = _validated_run_coverage(reliability["fresh_data_coverage"])
    trusted = _validated_run_coverage(
        reliability["trusted_decision_coverage"]
    )
    if (
        fresh["enabled"] != selected
        or trusted["enabled"] != selected
        or fresh["usable"] < trusted["usable"]
        or trusted["usable"] > evaluated
    ):
        raise ValueError("stock run coverage does not match workflow counts")
    if "coverage_semantics" in reliability and (
        reliability["coverage_semantics"] != "trusted_decision_coverage"
    ):
        raise ValueError("stock run coverage semantics are invalid")
    direct_coverage_keys = {
        "enabled_instruments",
        "usable_instruments",
        "fresh_coverage",
        "unusable_tickers",
    }
    present_direct_keys = direct_coverage_keys & set(reliability)
    if present_direct_keys and present_direct_keys != direct_coverage_keys:
        raise ValueError("stock run aggregate coverage is incomplete")
    if present_direct_keys:
        direct = _validated_run_coverage(
            {key: reliability[key] for key in direct_coverage_keys}
        )
        if direct != trusted:
            raise ValueError("stock run aggregate coverage is inconsistent")
    raw_by_market = reliability.get("by_market")
    by_market: dict[str, dict[str, Any]] | None = None
    if raw_by_market is not None:
        if (
            job != "stock-scan:ALL"
            or not isinstance(raw_by_market, dict)
            or not raw_by_market
            or not set(raw_by_market).issubset({"US", "HK"})
        ):
            raise ValueError("stock run market slices are invalid")
        by_market = {}
        for market, raw_slice in raw_by_market.items():
            if not isinstance(raw_slice, dict) or set(raw_slice) != {
                "selected",
                "evaluated",
                "fresh_data_coverage",
                "trusted_decision_coverage",
            }:
                raise ValueError("stock run market slice has invalid shape")
            slice_selected = raw_slice["selected"]
            slice_evaluated = raw_slice["evaluated"]
            if (
                type(slice_selected) is not int
                or type(slice_evaluated) is not int
                or slice_selected < 0
                or slice_evaluated < 0
                or slice_evaluated > slice_selected
            ):
                raise ValueError("stock run market slice counts are invalid")
            slice_fresh = _validated_run_coverage(
                raw_slice["fresh_data_coverage"]
            )
            slice_trusted = _validated_run_coverage(
                raw_slice["trusted_decision_coverage"]
            )
            if (
                slice_fresh["enabled"] != slice_selected
                or slice_trusted["enabled"] != slice_selected
                or slice_fresh["usable"] < slice_trusted["usable"]
                or slice_trusted["usable"] > slice_evaluated
            ):
                raise ValueError("stock run market slice coverage is invalid")
            by_market[market] = {
                "selected": slice_selected,
                "evaluated": slice_evaluated,
                "fresh_data": slice_fresh,
                "trusted_decision": slice_trusted,
            }
        if (
            sum(item["selected"] for item in by_market.values()) != selected
            or sum(item["evaluated"] for item in by_market.values()) != evaluated
        ):
            raise ValueError("stock run market slice counts do not sum")
        for coverage_name, aggregate in (
            ("fresh_data", fresh),
            ("trusted_decision", trusted),
        ):
            slices = [item[coverage_name] for item in by_market.values()]
            if (
                sum(item["enabled"] for item in slices) != aggregate["enabled"]
                or sum(item["usable"] for item in slices) != aggregate["usable"]
                or sorted(
                    ticker for item in slices for ticker in item["affected"]
                )
                != aggregate["affected"]
            ):
                raise ValueError("stock run market slice coverage does not sum")
    raw_provider_failures = reliability.get("provider_capability_failures", [])
    if not isinstance(raw_provider_failures, list):
        raise ValueError("stock run provider failures are invalid")
    for failure in raw_provider_failures:
        if not isinstance(failure, dict) or set(failure) != {
            "provider",
            "operation",
            "market",
            "reason",
            "circuit",
        }:
            raise ValueError("stock run provider failure has invalid shape")
        if failure["market"] not in {"US", "HK"}:
            raise ValueError("stock run provider market is invalid")
        for key in ("provider", "operation", "reason", "circuit"):
            raw = failure[key]
            if (
                not isinstance(raw, str)
                or _low_cardinality_code(raw, key) != raw
            ):
                raise ValueError("stock run provider failure is invalid")
    for key in ("error_tickers", "notification_error_tickers"):
        raw = value.get(key, [])
        if not isinstance(raw, list):
            raise ValueError("stock run ticker evidence is invalid")
        for ticker in raw:
            instrument_set_hash((ticker,))
    for key in ("incident_attempted", "incident_notified"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError("stock run incident evidence is invalid")
    for key in ("integrity_incident_attempted", "integrity_incident_notified"):
        if key in value and (
            type(value[key]) is not int or value[key] < 0
        ):
            raise ValueError("stock run integrity count is invalid")
    for key in ("incident_notification_error", "integrity_ledger_error"):
        raw = value.get(key)
        if raw is not None and (
            not isinstance(raw, str)
            or _low_cardinality_code(raw, key) != raw
        ):
            raise ValueError("stock run error evidence is invalid")
    probe_attempted = value["telegram_probe_attempted"]
    probe_success = value["telegram_probe_success"]
    probe_error_raw = value["telegram_probe_error"]
    if not isinstance(probe_attempted, bool) or (
        probe_success is not None and not isinstance(probe_success, bool)
    ):
        raise ValueError("stock run probe evidence is invalid")
    probe_error = _low_cardinality_code(probe_error_raw, "probe error")
    if probe_error_raw is not None and (
        not isinstance(probe_error_raw, str) or probe_error_raw != probe_error
    ):
        raise ValueError("stock run probe error is invalid")
    if (
        (not probe_attempted and (probe_success is not None or probe_error is not None))
        or (probe_success is True and probe_error is not None)
        or (probe_success is False and probe_error is None)
    ):
        raise ValueError("stock run probe result is inconsistent")
    raw_integrity_errors = value.get("integrity_notification_errors", {})
    if not isinstance(raw_integrity_errors, dict):
        raise ValueError("stock run integrity errors are invalid")
    for key, error in raw_integrity_errors.items():
        if (
            not isinstance(key, str)
            or not isinstance(error, str)
            or _low_cardinality_code(key, "integrity key") != key
            or _low_cardinality_code(error, "integrity error") != error
        ):
            raise ValueError("stock run integrity errors are invalid")
    event_id = value.get("protection_event_id")
    if event_id is not None and (type(event_id) is not int or event_id <= 0):
        raise ValueError("stock run event id is invalid")
    event_ids = value.get("protection_event_ids", [])
    if not isinstance(event_ids, list) or any(
        type(item) is not int or item <= 0 for item in event_ids
    ):
        raise ValueError("stock run event ids are invalid")
    failed = bool(
        value["error_tickers"]
        or value["notification_error_tickers"]
        or value["incident_notification_error"]
        or value["integrity_notification_errors"]
        or value["integrity_ledger_error"]
        or probe_error
        or evaluated < selected
    )
    status_is_consistent = (
        (status == "success" and not failed and evaluated == selected)
        or (status == "partial" and failed and evaluated > 0)
        or (status == "error" and failed and evaluated == 0)
    )
    if not status_is_consistent:
        raise ValueError("stock run status is inconsistent with failure evidence")
    return {
        "selected": selected,
        "evaluated": evaluated,
        "notified": notified,
        "fresh_data": fresh,
        "trusted_decision": trusted,
        "by_market": by_market,
    }


def _validated_run_record(
    row: Mapping[str, Any],
    *,
    not_after: datetime | None = None,
) -> dict[str, Any]:
    try:
        run_id = row["id"]
        job = _canonical_run_job(row["job"])
        status = row["status"]
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("run id is invalid")
        if not isinstance(status, str) or status not in {
            "success",
            "partial",
            "error",
        }:
            raise ValueError("run status is invalid")
        if not isinstance(row["started_at"], str) or not isinstance(
            row["finished_at"], str
        ):
            raise ValueError("run timestamps must be text")
        started = _parse_persisted_aware_timestamp(row["started_at"])
        finished = _parse_persisted_aware_timestamp(row["finished_at"])
        if started > finished:
            raise ValueError("run chronology is invalid")
        cutoff = _normalise_time(not_after) if not_after is not None else None
        if cutoff is not None and finished > cutoff:
            raise ValueError("run evidence is future-dated")
        recognized_stock = job in {
            "stock-scan:US",
            "stock-scan:HK",
            "stock-scan:ALL",
        }
        if job.startswith("stock-scan:") and not recognized_stock:
            raise ValueError("reserved stock run identity is invalid")
        if recognized_stock:
            raw_detail = row.get("detail")
            if not isinstance(raw_detail, str):
                raise ValueError("run detail must be a JSON object")
            detail = json.loads(raw_detail)
            if not isinstance(detail, dict):
                raise ValueError("run detail must be a JSON object")
            projected_detail = _validated_stock_run_detail(
                detail,
                status=status,
                job=job,
            )
        elif job == "news-scan":
            raw_detail = row.get("detail")
            if not isinstance(raw_detail, str):
                raise ValueError("run detail must be a JSON object")
            detail = json.loads(raw_detail)
            if not isinstance(detail, dict):
                raise ValueError("run detail must be a JSON object")
            if set(detail) != {
                "fetched",
                "review_items",
                "notified",
                "notification_failures",
            } or any(type(value) is not int or value < 0 for value in detail.values()):
                raise ValueError("news run detail is invalid")
            projected_detail = None
        else:
            projected_detail = None
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise CorruptProtectionStateError(
            "persisted run evidence is corrupt"
        ) from None
    return {
        "id": run_id,
        "job": job,
        "status": status,
        "started_at": _timestamp(started),
        "finished_at": _timestamp(finished),
        "detail": projected_detail,
    }


def _corrupt_run_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    not_after: datetime,
) -> list[dict[str, Any]]:
    corrupt: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(row)
        try:
            _validated_run_record(raw, not_after=not_after)
        except CorruptProtectionStateError:
            corrupt.append(raw)
    return corrupt


def _derived_run_log_markets(
    corrupt_rows: Sequence[Mapping[str, Any]],
    enabled_markets: set[str],
) -> set[str]:
    explicit: set[str] = set()
    needs_all = False
    for row in corrupt_rows:
        raw_job = row.get("job")
        if raw_job == "stock-scan:US":
            explicit.add("US")
        elif raw_job == "stock-scan:HK":
            explicit.add("HK")
        elif raw_job == "stock-scan:ALL":
            needs_all = True
        elif raw_job == "news-scan":
            continue
        else:
            try:
                canonical_job = _canonical_run_job(raw_job)
            except (TypeError, ValueError):
                needs_all = True
            else:
                if canonical_job.startswith("stock-scan:"):
                    needs_all = True
    derived = explicit & enabled_markets
    if needs_all:
        derived.update(enabled_markets)
    return derived


class StateStore:
    """Persist minimal notification state in a local SQLite database.

    One connection is safe to share between threads in this process.  A
    process-local re-entrant lock protects that connection, while
    ``BEGIN IMMEDIATE`` and SQLite's busy timeout serialize writers across
    independently opened ``StateStore`` instances and processes.
    """

    def __init__(
        self,
        path: str | Path = "alpha_guard.db",
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")

        self.path = str(path)
        if self.path != ":memory:" and not self.path.startswith("file:"):
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.path,
            timeout=busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
            uri=self.path.startswith("file:"),
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(_SCHEMA)
        generation_indexes = self._connection.execute(
            "PRAGMA index_list(protection_scope_generations)"
        ).fetchall()
        if any(row[2] == 1 and row[3] == "u" for row in generation_indexes):
            # Early generation-ledger builds made the content digest unique.
            # A legitimate add/remove cycle can return to the same digest; the
            # monotonically increasing row id, not the digest, orders distinct
            # responsibility generations.
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    ALTER TABLE protection_scope_generations
                    RENAME TO protection_scope_generations_legacy
                    """
                )
                self._connection.execute(
                    """
                    CREATE TABLE protection_scope_generations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope_key TEXT NOT NULL,
                        generation TEXT NOT NULL,
                        activated_at TEXT NOT NULL,
                        superseded_at TEXT
                    )
                    """
                )
                self._connection.execute(
                    """
                    INSERT INTO protection_scope_generations (
                        id, scope_key, generation, activated_at, superseded_at
                    )
                    SELECT id, scope_key, generation, activated_at, superseded_at
                    FROM protection_scope_generations_legacy
                    ORDER BY id
                    """
                )
                self._connection.execute(
                    "DROP TABLE protection_scope_generations_legacy"
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_protection_scope_generations_time
                    ON protection_scope_generations(
                        scope_key, activated_at, generation
                    )
                    """
                )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
        event_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(protection_events)"
            ).fetchall()
        }
        if "delivery_status" not in event_columns:
            # Legacy rows predate the ACTIVE/PREVIEW disposition contract.  A
            # historical row is never assumed deliverable: rows already marked
            # notified are ``sent`` and every other row is ``suppressed``.  The
            # ALTER and backfill share one transaction so a process crash cannot
            # expose a legacy event as a new pending incident.
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    ALTER TABLE protection_events
                    ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'pending'
                    """
                )
                self._connection.execute(
                    """
                    UPDATE protection_events
                    SET delivery_status = CASE
                        WHEN notified_at IS NOT NULL THEN 'sent'
                        ELSE 'suppressed'
                    END
                    """
                )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
        scope_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(protection_scope)"
            ).fetchall()
        }
        if "market_epochs_json" not in scope_columns:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    ALTER TABLE protection_scope
                    ADD COLUMN market_epochs_json TEXT NOT NULL DEFAULT '{}'
                    """
                )
                rows = self._connection.execute(
                    """
                    SELECT scope_key, activated_at, enabled_markets_json
                    FROM protection_scope
                    """
                ).fetchall()
                for row in rows:
                    try:
                        markets = json.loads(row["enabled_markets_json"])
                        activated = _parse_persisted_aware_timestamp(
                            row["activated_at"]
                        )
                        if (
                            not isinstance(markets, list)
                            or any(not isinstance(item, str) for item in markets)
                        ):
                            continue
                    except (TypeError, ValueError, json.JSONDecodeError):
                        # Leave an invalid epoch payload in place.  Reads and
                        # scans then fail closed through the typed validator;
                        # only the explicit repair API may replace it.
                        continue
                    epochs = {
                        market: _timestamp(activated) for market in markets
                    }
                    self._connection.execute(
                        """
                        UPDATE protection_scope
                        SET market_epochs_json = ?
                        WHERE scope_key = ?
                        """,
                        (_json_payload(epochs), row["scope_key"]),
                    )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
        scope_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(protection_scope)"
            ).fetchall()
        }
        if "market_instrument_hashes_json" not in scope_columns:
            self._connection.execute(
                """
                ALTER TABLE protection_scope
                ADD COLUMN market_instrument_hashes_json TEXT NOT NULL DEFAULT '{}'
                """
            )
        if "market_contract_hashes_json" not in scope_columns:
            self._connection.execute(
                """
                ALTER TABLE protection_scope
                ADD COLUMN market_contract_hashes_json TEXT NOT NULL DEFAULT '{}'
                """
            )
        if "watchdog_generation" not in scope_columns:
            # A legacy row has no authenticated link between scope identity and
            # the watchdog outbox.  Keep it explicitly unproven until the next
            # real set/repair operation establishes the generation.
            self._connection.execute(
                """
                ALTER TABLE protection_scope
                ADD COLUMN watchdog_generation TEXT
                """
            )
        delivery_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(delivery_state)"
            ).fetchall()
        }
        if "config_fingerprint" not in delivery_columns:
            # Legacy success belongs to an unproven configuration generation.
            # Keep the evidence for audit but never infer its secret identity.
            self._connection.execute(
                """
                ALTER TABLE delivery_state
                ADD COLUMN config_fingerprint TEXT
                """
            )
        if "generation" not in delivery_columns:
            self._connection.execute(
                """
                ALTER TABLE delivery_state
                ADD COLUMN generation INTEGER NOT NULL DEFAULT 1
                """
            )
        watchdog_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(watchdog_incidents)"
            ).fetchall()
        }
        if "detected_notified_at" not in watchdog_columns:
            self._connection.execute(
                """
                ALTER TABLE watchdog_incidents
                ADD COLUMN detected_notified_at TEXT
                """
            )
        if "delivery_kind" not in watchdog_columns:
            self._connection.execute(
                """
                ALTER TABLE watchdog_incidents
                ADD COLUMN delivery_kind TEXT NOT NULL DEFAULT 'detected'
                """
            )

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for read-only diagnostics and migrations."""

        self._ensure_open()
        return self._connection

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("StateStore is closed")

    def _suppress_pending_incident_events_tx(
        self,
        connection: sqlite3.Connection,
        scope: str,
        *,
        except_state: str | None = None,
        except_incident_id: str | None = None,
    ) -> None:
        query = """
            SELECT id FROM protection_events
            WHERE scope_key = ? AND delivery_status = 'pending'
        """
        parameters: list[Any] = [scope]
        if except_state is not None:
            query += """
                AND NOT (
                    current_state = ?
                    AND (
                        incident_id = ?
                        OR (incident_id IS NULL AND ? IS NULL)
                    )
                )
            """
            parameters.extend(
                (except_state, except_incident_id, except_incident_id)
            )
        rows = connection.execute(query, parameters).fetchall()
        if not rows:
            return
        event_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in event_ids)
        connection.execute(
            f"""
            UPDATE protection_events
            SET delivery_status = 'suppressed'
            WHERE id IN ({placeholders})
            """,  # noqa: S608 - placeholders are generated, never user input
            event_ids,
        )
        for event_id in event_ids:
            self._delete_notification_claim(
                connection, self._incident_claim_key(event_id)
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._ensure_open()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def should_notify_signal(
        self,
        key: str,
        active: bool | None,
        fingerprint: str,
        cooldown_hours: float,
        now: datetime | None = None,
    ) -> bool:
        """Apply a signal observation and return whether it may be notified.

        ``None`` is the fail-safe UNKNOWN state and performs no write at all.
        ``False`` explicitly resets an active signal.  ``True`` is eligible on
        first activation, after a reset, when evidence changes, after a failed
        send, or once a positive cooldown has elapsed.
        """

        prepared = _prepare_signal_observation(
            key,
            active,
            fingerprint,
            cooldown_hours,
            now,
        )
        if prepared is None:
            return False
        signal_key, cooldown, observed_at, observed_ts = prepared
        with self._transaction() as connection:
            return self._observe_signal(
                connection,
                signal_key,
                bool(active),
                fingerprint,
                cooldown,
                observed_at,
                observed_ts,
            )

    def claim_signal_notification(
        self,
        key: str,
        active: bool | None,
        fingerprint: str,
        cooldown_hours: float,
        *,
        lease_seconds: float = 300,
        now: datetime | None = None,
    ) -> str | None:
        """Atomically observe a signal and lease its notification eligibility.

        The returned opaque token grants one sender the right to complete the
        notification before the lease expires.  ``None`` means the observation
        is UNKNOWN, inactive, in cooldown, or already leased by another worker.
        """

        prepared = _prepare_signal_observation(
            key,
            active,
            fingerprint,
            cooldown_hours,
            now,
        )
        if prepared is None:
            return None
        signal_key, cooldown, observed_at, observed_ts = prepared
        lease = _lease_duration(lease_seconds)

        with self._transaction() as connection:
            eligible = self._observe_signal(
                connection,
                signal_key,
                bool(active),
                fingerprint,
                cooldown,
                observed_at,
                observed_ts,
            )
            if not eligible:
                return None
            return self._acquire_notification_claim(
                connection,
                self._signal_claim_key(signal_key),
                signal_key,
                lease,
                observed_at,
            )

    def _observe_signal(
        self,
        connection: sqlite3.Connection,
        signal_key: str,
        active: bool,
        fingerprint: str,
        cooldown: float,
        observed_at: datetime,
        observed_ts: str,
    ) -> bool:
        row = connection.execute(
            "SELECT * FROM signal_state WHERE signal_key = ?",
            (signal_key,),
        ).fetchone()

        if not active:
            self._reset_signal(connection, signal_key, row, observed_ts)
            return False

        if row is None:
            self._delete_notification_claim(
                connection, self._signal_claim_key(signal_key)
            )
            connection.execute(
                """
                INSERT INTO signal_state (
                    signal_key, active, fingerprint, activated_at,
                    last_seen_at, last_sent_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, NULL, ?)
                """,
                (signal_key, fingerprint, observed_ts, observed_ts, observed_ts),
            )
            self._record_signal_event(
                connection,
                signal_key,
                "activated",
                True,
                fingerprint,
                observed_ts,
            )
            return True

        if not bool(row["active"]):
            self._delete_notification_claim(
                connection, self._signal_claim_key(signal_key)
            )
            connection.execute(
                """
                UPDATE signal_state
                SET active = 1,
                    fingerprint = ?,
                    activated_at = ?,
                    last_seen_at = ?,
                    last_sent_at = NULL,
                    updated_at = ?
                WHERE signal_key = ?
                """,
                (fingerprint, observed_ts, observed_ts, observed_ts, signal_key),
            )
            self._record_signal_event(
                connection,
                signal_key,
                "activated",
                True,
                fingerprint,
                observed_ts,
            )
            return True

        if row["fingerprint"] != fingerprint:
            self._delete_notification_claim(
                connection, self._signal_claim_key(signal_key)
            )
            connection.execute(
                """
                UPDATE signal_state
                SET fingerprint = ?,
                    last_seen_at = ?,
                    last_sent_at = NULL,
                    updated_at = ?
                WHERE signal_key = ?
                """,
                (fingerprint, observed_ts, observed_ts, signal_key),
            )
            self._record_signal_event(
                connection,
                signal_key,
                "evidence_changed",
                True,
                fingerprint,
                observed_ts,
            )
            return True

        connection.execute(
            """
            UPDATE signal_state
            SET last_seen_at = ?, updated_at = ?
            WHERE signal_key = ?
            """,
            (observed_ts, observed_ts, signal_key),
        )
        if row["last_sent_at"] is None:
            return True
        if cooldown == 0:
            return False
        next_allowed = _parse_timestamp(row["last_sent_at"]) + timedelta(hours=cooldown)
        return observed_at >= next_allowed

    @staticmethod
    def _signal_claim_key(signal_key: str) -> str:
        return f"signal:{signal_key}"

    @staticmethod
    def _news_claim_key(fingerprint: str) -> str:
        return f"news:{fingerprint}"

    @staticmethod
    def _delete_notification_claim(
        connection: sqlite3.Connection,
        claim_key: str,
    ) -> None:
        connection.execute(
            "DELETE FROM notification_claims WHERE claim_key = ?",
            (claim_key,),
        )

    @staticmethod
    def _acquire_notification_claim(
        connection: sqlite3.Connection,
        claim_key: str,
        business_key: str,
        lease: timedelta,
        claimed_at: datetime,
    ) -> str | None:
        existing = connection.execute(
            """
            SELECT claim_token, expires_at
            FROM notification_claims
            WHERE claim_key = ?
            """,
            (claim_key,),
        ).fetchone()
        if (
            existing is not None
            and _parse_timestamp(existing["expires_at"]) > claimed_at
        ):
            return None

        token = secrets.token_urlsafe(32)
        claimed_ts = _timestamp(claimed_at)
        expires_ts = _timestamp(claimed_at + lease)
        connection.execute(
            """
            INSERT INTO notification_claims (
                claim_key, business_key, claim_token, claimed_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(claim_key) DO UPDATE SET
                business_key = excluded.business_key,
                claim_token = excluded.claim_token,
                claimed_at = excluded.claimed_at,
                expires_at = excluded.expires_at
            """,
            (claim_key, business_key, token, claimed_ts, expires_ts),
        )
        return token

    @staticmethod
    def _require_notification_claim(
        connection: sqlite3.Connection,
        claim_key: str,
        claim_token: str,
        completed_at: datetime,
    ) -> None:
        claim = connection.execute(
            """
            SELECT claim_token, claimed_at, expires_at
            FROM notification_claims
            WHERE claim_key = ?
            """,
            (claim_key,),
        ).fetchone()
        valid = (
            claim is not None
            and claim["claim_token"] == claim_token
            and _parse_timestamp(claim["claimed_at"]) <= completed_at
            and completed_at < _parse_timestamp(claim["expires_at"])
        )
        if not valid:
            raise ValueError("invalid or expired notification claim")

    def release_notification_claim(self, key: str, claim_token: str) -> bool:
        """Release a failed send's claim using its public business key and token."""

        business_key = _require_text(key, "key")
        token = _require_text(claim_token, "claim_token")
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM notification_claims
                WHERE business_key = ? AND claim_token = ?
                """,
                (business_key, token),
            )
            return cursor.rowcount == 1

    def _reset_signal(
        self,
        connection: sqlite3.Connection,
        signal_key: str,
        row: sqlite3.Row | None,
        observed_ts: str,
    ) -> None:
        self._delete_notification_claim(connection, self._signal_claim_key(signal_key))
        if row is None:
            connection.execute(
                """
                INSERT INTO signal_state (
                    signal_key, active, fingerprint, activated_at,
                    last_seen_at, last_sent_at, updated_at
                ) VALUES (?, 0, NULL, NULL, ?, NULL, ?)
                """,
                (signal_key, observed_ts, observed_ts),
            )
            return

        was_active = bool(row["active"])
        connection.execute(
            """
            UPDATE signal_state
            SET active = 0,
                fingerprint = NULL,
                activated_at = NULL,
                last_seen_at = ?,
                last_sent_at = NULL,
                updated_at = ?
            WHERE signal_key = ?
            """,
            (observed_ts, observed_ts, signal_key),
        )
        if was_active:
            self._record_signal_event(
                connection,
                signal_key,
                "reset",
                False,
                row["fingerprint"],
                observed_ts,
            )

    @staticmethod
    def _record_signal_event(
        connection: sqlite3.Connection,
        signal_key: str,
        event_type: str,
        active: bool | None,
        fingerprint: str | None,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO signal_events (
                signal_key, event_type, active, fingerprint, occurred_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                signal_key,
                event_type,
                None if active is None else int(active),
                fingerprint,
                occurred_at,
            ),
        )

    def mark_signal_notified(
        self,
        key: str,
        fingerprint: str | datetime | None = None,
        now: datetime | None = None,
        *,
        claim_token: str | None = None,
    ) -> None:
        """Mark a successful signal send.

        Supplying ``fingerprint`` protects against a stale in-flight send
        marking newer evidence as delivered.  ``claim_token`` additionally
        proves that this worker owns an unexpired send lease.  For convenience,
        a datetime as the second positional argument is treated as ``now``.
        """

        signal_key = _require_text(key, "key")
        if isinstance(fingerprint, datetime):
            if now is not None:
                raise TypeError("now was supplied twice")
            now = fingerprint
            fingerprint = None
        if fingerprint is not None and not isinstance(fingerprint, str):
            raise TypeError("fingerprint must be a string or None")
        if claim_token is not None:
            claim_token = _require_text(claim_token, "claim_token")

        sent_at = _normalise_time(now)
        sent_ts = _timestamp(sent_at)
        claim_key = self._signal_claim_key(signal_key)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT active, fingerprint FROM signal_state WHERE signal_key = ?",
                (signal_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown signal key: {signal_key}")
            if not bool(row["active"]):
                raise ValueError(
                    f"cannot mark inactive signal as notified: {signal_key}"
                )
            if fingerprint is not None and row["fingerprint"] != fingerprint:
                raise ValueError(
                    f"signal fingerprint changed before send: {signal_key}"
                )
            if claim_token is not None:
                self._require_notification_claim(
                    connection,
                    claim_key,
                    claim_token,
                    sent_at,
                )

            connection.execute(
                """
                UPDATE signal_state
                SET last_sent_at = ?, updated_at = ?
                WHERE signal_key = ?
                """,
                (sent_ts, sent_ts, signal_key),
            )
            self._record_signal_event(
                connection,
                signal_key,
                "notified",
                True,
                row["fingerprint"],
                sent_ts,
            )
            self._delete_notification_claim(connection, claim_key)

    def is_news_new(self, fingerprint: str, now: datetime | None = None) -> bool:
        """Return whether a news fingerprint has never been successfully sent."""

        del (
            now
        )  # Kept for a clock-compatible public API; this read is side-effect free.
        news_fingerprint = _require_text(fingerprint, "fingerprint")
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM news_seen WHERE fingerprint = ?",
                (news_fingerprint,),
            ).fetchone()
        return row is None

    def claim_news_notification(
        self,
        fingerprint: str,
        *,
        lease_seconds: float = 300,
        now: datetime | None = None,
    ) -> str | None:
        """Atomically lease an unseen news item to one notification worker."""

        news_fingerprint = _require_text(fingerprint, "fingerprint")
        lease = _lease_duration(lease_seconds)
        claimed_at = _normalise_time(now)
        claim_key = self._news_claim_key(news_fingerprint)
        with self._transaction() as connection:
            seen = connection.execute(
                "SELECT 1 FROM news_seen WHERE fingerprint = ?",
                (news_fingerprint,),
            ).fetchone()
            if seen is not None:
                self._delete_notification_claim(connection, claim_key)
                return None
            return self._acquire_notification_claim(
                connection,
                claim_key,
                news_fingerprint,
                lease,
                claimed_at,
            )

    def mark_news_notified(
        self,
        fingerprint: str,
        now: datetime | None = None,
        *,
        claim_token: str | None = None,
    ) -> None:
        """Record a successfully sent news item idempotently."""

        news_fingerprint = _require_text(fingerprint, "fingerprint")
        if claim_token is not None:
            claim_token = _require_text(claim_token, "claim_token")
        sent_at = _normalise_time(now)
        sent_ts = _timestamp(sent_at)
        claim_key = self._news_claim_key(news_fingerprint)
        with self._transaction() as connection:
            if claim_token is not None:
                self._require_notification_claim(
                    connection,
                    claim_key,
                    claim_token,
                    sent_at,
                )
            connection.execute(
                """
                INSERT INTO news_seen (
                    fingerprint, first_seen_at, last_seen_at, notified_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    notified_at = excluded.notified_at
                """,
                (news_fingerprint, sent_ts, sent_ts, sent_ts),
            )
            self._delete_notification_claim(connection, claim_key)

    def record_run(
        self,
        job: str,
        status: str,
        detail: Any = None,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        """Append an immutable scheduler/workflow run record and return its id."""

        job_name = _require_text(job, "job")
        run_status = _require_text(status, "status")
        if now is not None and (started_at is not None or finished_at is not None):
            raise TypeError("now cannot be combined with started_at or finished_at")

        if now is not None:
            start = finish = _normalise_time(now)
        else:
            start = _normalise_time(started_at)
            finish = _normalise_time(finished_at) if finished_at else start
        if finish < start:
            raise ValueError("finished_at cannot be earlier than started_at")

        encoded_detail: str | None
        if detail is None or isinstance(detail, str):
            encoded_detail = detail
        else:
            encoded_detail = json.dumps(
                detail,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )

        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO run_log (
                    job, status, started_at, finished_at, detail
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_name,
                    run_status,
                    _timestamp(start),
                    _timestamp(finish),
                    encoded_detail,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite contract guard
                raise RuntimeError("SQLite did not return a run-log row id")
            return int(cursor.lastrowid)

    def recent_status(
        self,
        job: str | int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return newest run records, optionally filtered by job name."""

        if isinstance(job, int):
            if limit != 20:
                raise TypeError("limit was supplied twice")
            limit = job
            job = None
        if job is not None:
            job = _require_text(job, "job")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")

        query = """
            SELECT id, job, status, started_at, finished_at, detail
            FROM run_log
        """
        parameters: list[Any] = []
        if job is not None:
            query += " WHERE job = ?"
            parameters.append(job)
        query += " ORDER BY finished_at DESC, id DESC LIMIT ?"
        parameters.append(limit)

        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()

        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            detail = record["detail"]
            if isinstance(detail, str):
                try:
                    record["detail"] = json.loads(detail)
                except json.JSONDecodeError:
                    pass
            records.append(record)
        return records

    def run_records(
        self,
        *,
        not_after: datetime | None = None,
        job: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return safe run projections after validating the complete ledger."""

        job_name = _canonical_run_job(job) if job is not None else None
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer")
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, job, status, started_at, finished_at, detail
                FROM run_log
                """
            ).fetchall()
        # Full-table validation deliberately precedes every caller view.  An
        # old malformed row cannot hide behind a job filter or result limit.
        records = [
            _validated_run_record(dict(row), not_after=not_after)
            for row in rows
        ]
        if job_name is not None:
            records = [item for item in records if item["job"] == job_name]
        records.sort(
            key=lambda item: (item["finished_at"], item["id"]),
            reverse=True,
        )
        return records[:limit] if limit is not None else records

    def repair_corrupt_run_log(
        self,
        *,
        affected_markets: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Quarantine malformed run rows and invalidate affected baselines."""

        markets = (
            None
            if affected_markets is None
            else tuple(
                sorted(
                    dict.fromkeys(
                        _require_text(item, "market").strip().upper()
                        for item in affected_markets
                    )
                )
            )
        )
        if markets is not None and any(
            market not in {"US", "HK"} for market in markets
        ):
            raise ValueError("affected_markets must contain only US or HK")
        repaired_at = _normalise_time(now)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT rowid AS physical_rowid, id, job, status,
                       started_at, finished_at, detail
                FROM run_log
                ORDER BY rowid
                """
            ).fetchall()
            corrupt_rows = _corrupt_run_rows(rows, not_after=repaired_at)
            if not corrupt_rows:
                raise ValueError("run log is valid and must not be repaired")
            protection_scope: dict[str, Any] | None = None
            enabled_markets: set[str] = set()
            if corrupt_rows:
                scope_row = connection.execute(
                    "SELECT * FROM protection_scope WHERE scope_key = 'global'"
                ).fetchone()
                if scope_row is not None:
                    try:
                        protection_scope = _validated_scope_row(
                            dict(scope_row),
                            expected_scope="global",
                        )
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        raise CorruptProtectionStateError(
                            "persisted protection scope is corrupt"
                        ) from None
                    enabled_markets = set(protection_scope["enabled_markets"])
            derived_markets = _derived_run_log_markets(
                corrupt_rows, enabled_markets
            )
            if markets is not None and set(markets) != derived_markets:
                raise ValueError(
                    "affected_markets must exactly match corrupt run responsibility"
                )
            if derived_markets:
                assert protection_scope is not None
                previous_updated = _parse_persisted_aware_timestamp(
                    protection_scope["updated_at"]
                )
                if repaired_at < previous_updated:
                    raise ValueError("run log repair is out of order")

            digests: list[str] = []
            for row in corrupt_rows:
                raw_payload = _sqlite_quarantine_envelope(row)
                digest = hashlib.sha256(raw_payload.encode()).hexdigest()
                connection.execute(
                    """
                    INSERT INTO run_log_quarantine (
                        run_id, payload_sha256, raw_payload, quarantined_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        row["physical_rowid"],
                        digest,
                        raw_payload,
                        _timestamp(repaired_at),
                    ),
                )
                connection.execute(
                    "DELETE FROM run_log WHERE rowid = ?",
                    (row["physical_rowid"],),
                )
                digests.append(digest)

            if derived_markets:
                assert protection_scope is not None
                market_epochs = {
                    market: (
                        repaired_at
                        if market in derived_markets
                        else _parse_persisted_aware_timestamp(epoch)
                    )
                    for market, epoch in protection_scope[
                        "market_epochs"
                    ].items()
                }
                activated_at = min(market_epochs.values())
                next_scope = {
                    **protection_scope,
                    "activated_at": _timestamp(activated_at),
                    "market_epochs": {
                        market: _timestamp(epoch)
                        for market, epoch in market_epochs.items()
                    },
                    "updated_at": _timestamp(repaired_at),
                }
                generation = self._transition_scope_generation_tx(
                    connection,
                    current_scope=protection_scope,
                    next_scope=next_scope,
                    changed_at=repaired_at,
                )
                connection.execute(
                    """
                    UPDATE protection_scope
                    SET activated_at = ?, market_epochs_json = ?,
                        watchdog_generation = ?, updated_at = ?
                    WHERE scope_key = 'global'
                    """,
                    (
                        _timestamp(activated_at),
                        _json_payload(
                            {
                                market: _timestamp(epoch)
                                for market, epoch in market_epochs.items()
                            }
                        ),
                        generation,
                        _timestamp(repaired_at),
                    ),
                )
            self._resolve_integrity_incident_tx(
                connection,
                "global",
                "run_log",
                repaired_at,
            )
            return tuple(digests)

    def corrupt_run_log_affected_markets(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Return current responsibility derived from malformed run evidence."""

        cutoff = _normalise_time(now)
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT rowid AS physical_rowid, id, job, status,
                       started_at, finished_at, detail
                FROM run_log
                ORDER BY rowid
                """
            ).fetchall()
            corrupt_rows = _corrupt_run_rows(rows, not_after=cutoff)
            if not corrupt_rows:
                raise ValueError("run log is valid and must not be repaired")
            scope_row = self._connection.execute(
                "SELECT * FROM protection_scope WHERE scope_key = 'global'"
            ).fetchone()
        enabled_markets: set[str] = set()
        if scope_row is not None:
            try:
                scope = _validated_scope_row(
                    dict(scope_row), expected_scope="global"
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise CorruptProtectionStateError(
                    "persisted protection scope is corrupt"
                ) from None
            enabled_markets = set(scope["enabled_markets"])
        return tuple(
            sorted(_derived_run_log_markets(corrupt_rows, enabled_markets))
        )

    def load_protection_state(
        self, scope: str = "global"
    ) -> ProtectionSnapshot | None:
        """Load a validated protection snapshot; corrupt evidence fails closed."""

        scope_key = _require_text(scope, "scope")
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = ProtectionSnapshot.model_validate_json(row["snapshot_json"])
            if snapshot.scope != scope_key:
                raise ValueError("snapshot scope does not match row key")
            return snapshot
        except (TypeError, ValueError):
            raise CorruptProtectionStateError(
                "persisted protection state is corrupt"
            ) from None

    def protection_states(self) -> dict[str, ProtectionSnapshot]:
        """Return all scope snapshots; malformed safety evidence raises."""

        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                "SELECT scope_key, snapshot_json FROM protection_state ORDER BY scope_key"
            ).fetchall()
        result: dict[str, ProtectionSnapshot] = {}
        for row in rows:
            try:
                snapshot = ProtectionSnapshot.model_validate_json(
                    row["snapshot_json"]
                )
                if snapshot.scope != row["scope_key"]:
                    raise ValueError("snapshot scope does not match row key")
                result[row["scope_key"]] = snapshot
            except (TypeError, ValueError):
                raise CorruptProtectionStateError(
                    "persisted protection state is corrupt"
                ) from None
        return result

    def observe_protection(
        self,
        observation: BlindnessObservation,
        *,
        delivery_status: Literal["pending", "suppressed"] = "suppressed",
    ) -> tuple[ProtectionTransition, int | None]:
        """Atomically transition state and append an edge with its disposition.

        ``suppressed`` is the fail-safe default.  ACTIVE callers must opt an
        edge into delivery with ``pending`` in the same transaction that creates
        it; a PREVIEW process can therefore crash immediately after this call
        without leaving deliverable history behind.
        """

        if not isinstance(observation, BlindnessObservation):
            observation = BlindnessObservation.model_validate(observation)
        if delivery_status not in {"pending", "suppressed"}:
            raise ValueError("delivery_status must be pending or suppressed")
        observation_id = protection_observation_identity(observation)
        observation_payload = _json_payload(observation.model_dump(mode="json"))
        observation_digest = hashlib.sha256(observation_payload.encode()).hexdigest()
        collision_detected = False
        result_transition: ProtectionTransition | None = None
        result_event_id: int | None = None
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = ?",
                (observation.scope,),
            ).fetchone()
            current: ProtectionSnapshot | None = None
            if row is not None:
                try:
                    current = ProtectionSnapshot.model_validate_json(
                        row["snapshot_json"]
                    )
                    if current.scope != observation.scope:
                        raise ValueError("snapshot scope does not match row key")
                except (TypeError, ValueError):
                    raise CorruptProtectionStateError(
                        "persisted protection state is corrupt"
                    ) from None
            ledger_row = connection.execute(
                """
                SELECT payload_sha256
                FROM protection_observations
                WHERE scope_key = ? AND observation_id = ?
                """,
                (observation.scope, observation_id),
            ).fetchone()
            if ledger_row is not None:
                if ledger_row["payload_sha256"] == observation_digest:
                    if current is None:
                        raise CorruptProtectionStateError(
                            "observation ledger exists without protection state"
                        )
                    return (
                        ProtectionTransition(
                            previous_state=current.state,
                            snapshot=current,
                            edge=False,
                            event_type=None,
                        ),
                        None,
                    )

                collision_detected = True
                detected_at = (
                    max(observation.observed_at, current.updated_at)
                    if current is not None
                    else observation.observed_at
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO protection_observation_collisions (
                        scope_key, observation_id, original_sha256,
                        conflicting_sha256, detected_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        observation.scope,
                        observation_id,
                        ledger_row["payload_sha256"],
                        observation_digest,
                        _timestamp(detected_at),
                    ),
                )
                enabled = max(
                    observation.enabled_instruments,
                    (
                        current.coverage.enabled_instruments
                        if current is not None
                        else 0
                    ),
                )
                if enabled == 0:
                    # The collision ledger itself is persistent fail-closed
                    # evidence.  No fake instrument count is invented merely
                    # to fit a configured BLIND snapshot.
                    result_transition = None
                    result_event_id = None
                    transition = None
                else:
                    collision_observation = BlindnessObservation(
                        scope=observation.scope,
                        observation_id=(
                            "collision:"
                            + hashlib.sha256(
                                (
                                    str(ledger_row["payload_sha256"])
                                    + observation_digest
                                ).encode()
                            ).hexdigest()[:64]
                        ),
                        observed_at=detected_at,
                        enabled_instruments=enabled,
                        usable_instruments=0,
                        reason_codes=("observation_id_collision",),
                    )
                    transition = transition_protection(
                        current, collision_observation
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO protection_observations (
                        scope_key, observation_id, payload_sha256, observed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        observation.scope,
                        observation_id,
                        observation_digest,
                        _timestamp(observation.observed_at),
                    ),
                )
                transition = transition_protection(current, observation)

            if transition is None:
                # Only possible for an unconfigured collision.  The collision
                # row is committed below and the typed error is raised after
                # leaving the transaction.
                pass
            else:
                snapshot = transition.snapshot
                payload = snapshot.model_dump(mode="json")
                connection.execute(
                    """
                    INSERT INTO protection_state (scope_key, snapshot_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(scope_key) DO UPDATE SET
                        snapshot_json = excluded.snapshot_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        observation.scope,
                        _json_payload(payload),
                        _timestamp(snapshot.updated_at),
                    ),
                )
                event_id: int | None = None
                if transition.edge and transition.event_type is not None:
                    self._suppress_pending_incident_events_tx(
                        connection, observation.scope
                    )
                    effective_delivery_status = (
                        delivery_status
                        if transition.event_type
                        in {"blind", "degraded", "recovering", "recovered"}
                        else "suppressed"
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO protection_events (
                            scope_key, event_type, previous_state, current_state,
                            incident_id, occurred_at, payload_json, notified_at,
                            delivery_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                        """,
                        (
                            observation.scope,
                            transition.event_type,
                            (
                                transition.previous_state.value
                                if transition.previous_state is not None
                                else None
                            ),
                            snapshot.state.value,
                            snapshot.incident_id,
                            _timestamp(snapshot.updated_at),
                            _json_payload(payload),
                            effective_delivery_status,
                        ),
                    )
                    if cursor.lastrowid is None:  # pragma: no cover - SQLite contract
                        raise RuntimeError(
                            "SQLite did not return a protection event id"
                        )
                    event_id = int(cursor.lastrowid)
                result_transition = transition
                result_event_id = event_id
                if not collision_detected:
                    self._resolve_integrity_incident_tx(
                        connection,
                        observation.scope,
                        "protection_state",
                        snapshot.updated_at,
                    )
        if collision_detected:
            raise ProtectionObservationCollisionError(
                "protection observation id collision"
            )
        assert result_transition is not None
        return result_transition, result_event_id

    def repair_corrupt_protection_state(
        self,
        *,
        enabled_instruments: int,
        scope: str = "global",
        now: datetime | None = None,
    ) -> tuple[ProtectionSnapshot, str, int]:
        """Quarantine corrupt raw state and install an explicit BLIND sentinel.

        This API never accepts caller-provided raw state and never returns the
        quarantined payload. The digest is safe to expose as an audit handle.
        """

        if (
            isinstance(enabled_instruments, bool)
            or not isinstance(enabled_instruments, int)
            or enabled_instruments < 0
        ):
            raise ValueError("enabled_instruments must be a non-negative integer")
        scope_key = _require_text(scope, "scope")
        repaired_at = _normalise_time(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no protection state exists for scope: {scope_key}")
            raw_payload = str(row["snapshot_json"])
            try:
                persisted = ProtectionSnapshot.model_validate_json(raw_payload)
                if persisted.scope != scope_key:
                    raise ValueError("snapshot scope does not match row key")
            except (TypeError, ValueError):
                pass
            else:
                raise ValueError("protection state is valid and must not be repaired")
            digest = hashlib.sha256(raw_payload.encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO protection_state_quarantine (
                    scope_key, payload_sha256, raw_payload, quarantined_at
                ) VALUES (?, ?, ?, ?)
                """,
                (scope_key, digest, raw_payload, _timestamp(repaired_at)),
            )
            transition = transition_protection(
                None,
                BlindnessObservation(
                    scope=scope_key,
                    observation_id=f"repair:{digest}",
                    observed_at=repaired_at,
                    enabled_instruments=enabled_instruments,
                    usable_instruments=0,
                    reason_codes=("state_repaired",),
                ),
            )
            snapshot = transition.snapshot
            payload = _json_payload(snapshot.model_dump(mode="json"))
            connection.execute(
                """
                UPDATE protection_state
                SET snapshot_json = ?, updated_at = ?
                WHERE scope_key = ?
                """,
                (payload, _timestamp(repaired_at), scope_key),
            )
            cursor = connection.execute(
                """
                INSERT INTO protection_events (
                    scope_key, event_type, previous_state, current_state,
                    incident_id, occurred_at, payload_json, notified_at,
                    delivery_status
                ) VALUES (?, ?, 'CORRUPT', ?, ?, ?, ?, NULL, 'suppressed')
                """,
                (
                    scope_key,
                    transition.event_type,
                    snapshot.state.value,
                    snapshot.incident_id,
                    _timestamp(repaired_at),
                    payload,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite contract
                raise RuntimeError("SQLite did not return a repair event id")
            self._resolve_integrity_incident_tx(
                connection,
                scope_key,
                "protection_state",
                repaired_at,
            )
            return snapshot, digest, int(cursor.lastrowid)

    def protection_events(
        self,
        *,
        scope: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if scope is not None:
            scope = _require_text(scope, "scope")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        query = """
            SELECT id, scope_key, event_type, previous_state, current_state,
                   incident_id, occurred_at, payload_json, notified_at,
                   delivery_status
            FROM protection_events
        """
        parameters: list[Any] = []
        if scope is not None:
            query += " WHERE scope_key = ?"
            parameters.append(scope)
        query += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        parameters.append(limit)
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except (json.JSONDecodeError, TypeError):
                item["payload"] = None
                item.pop("payload_json", None)
            events.append(item)
        return events

    def protection_observation_collisions(
        self,
        *,
        scope: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return hash-only collision evidence; raw observations are never stored."""

        if scope is not None:
            scope = _require_text(scope, "scope")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        query = """
            SELECT id, scope_key, observation_id, original_sha256,
                   conflicting_sha256, detected_at
            FROM protection_observation_collisions
        """
        parameters: list[Any] = []
        if scope is not None:
            query += " WHERE scope_key = ?"
            parameters.append(scope)
        query += " ORDER BY detected_at DESC, id DESC LIMIT ?"
        parameters.append(limit)
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _incident_claim_key(event_id: int) -> str:
        return f"incident:{event_id}"

    @staticmethod
    def _integrity_claim_key(incident_id: int) -> str:
        return f"integrity:{incident_id}"

    @staticmethod
    def _watchdog_claim_key(incident_id: int) -> str:
        return f"watchdog:{incident_id}"

    @staticmethod
    def _watchdog_incidents_tx(
        connection: sqlite3.Connection,
        *,
        not_after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM watchdog_incidents"
        ).fetchall()
        incidents = [
            _validated_watchdog_incident(dict(row), not_after=not_after)
            for row in rows
        ]
        if sum(
            item["delivery_status"] == "pending"
            and item["notified_at"] is None
            for item in incidents
        ) > 1:
            raise CorruptProtectionStateError(
                "persisted watchdog delivery evidence is corrupt"
            )
        incidents.sort(key=lambda item: (item["generation"], item["id"]))
        return incidents

    @classmethod
    def _watchdog_claim_is_live_tx(
        cls,
        connection: sqlite3.Connection,
        incident_id: int,
        observed_at: datetime,
    ) -> bool:
        claim_key = cls._watchdog_claim_key(incident_id)
        row = connection.execute(
            """
            SELECT claimed_at, expires_at
            FROM notification_claims
            WHERE claim_key = ?
            """,
            (claim_key,),
        ).fetchone()
        if row is None:
            return False
        try:
            claimed_at = _parse_timestamp(row["claimed_at"])
            expires_at = _parse_timestamp(row["expires_at"])
        except (TypeError, ValueError):
            raise CorruptProtectionStateError(
                "persisted watchdog delivery claim is corrupt"
            ) from None
        if claimed_at > observed_at:
            raise CorruptProtectionStateError(
                "persisted watchdog delivery claim is future-dated"
            )
        if observed_at >= expires_at:
            cls._delete_notification_claim(connection, claim_key)
            return False
        return True

    @classmethod
    def _suppress_watchdog_pending_tx(
        cls,
        connection: sqlite3.Connection,
        incidents: Sequence[Mapping[str, Any]],
    ) -> None:
        for incident in incidents:
            if (
                incident["delivery_status"] == "pending"
                and incident["notified_at"] is None
            ):
                connection.execute(
                    """
                    UPDATE watchdog_incidents
                    SET delivery_status = 'suppressed'
                    WHERE id = ?
                    """,
                    (incident["id"],),
                )
                cls._delete_notification_claim(
                    connection,
                    cls._watchdog_claim_key(int(incident["id"])),
                )

    @classmethod
    def _retire_watchdog_scope_generation_tx(
        cls,
        connection: sqlite3.Connection,
        incidents: Sequence[Mapping[str, Any]],
        *,
        expected_generation: str | None,
        retired_at: datetime,
    ) -> bool:
        """Retire unclaimed outbox debt from one authenticated scope generation.

        A live claim is the notification linearization barrier.  In that case
        scope mutation may proceed but the exact claimed edge is left intact;
        the follow-up transition gate binds and retires it after completion.
        """

        outstanding = [
            incident
            for incident in incidents
            if incident["active"]
            or (
                incident["delivery_status"] == "pending"
                and incident["notified_at"] is None
            )
        ]
        if not outstanding:
            return False
        if expected_generation is None or any(
            incident["payload"]["scope_generation"] != expected_generation
            for incident in outstanding
        ):
            raise CorruptProtectionStateError(
                "watchdog incident does not match protection scope generation"
            )
        live_claim = any(
            cls._watchdog_claim_is_live_tx(
                connection,
                int(incident["id"]),
                retired_at,
            )
            for incident in outstanding
        )
        if live_claim:
            return True
        retired_ts = _timestamp(retired_at)
        for incident in outstanding:
            incident_id = int(incident["id"])
            if incident["active"]:
                connection.execute(
                    """
                    UPDATE watchdog_incidents
                    SET state = 'RECOVERED', active = 0, resolved_at = ?,
                        delivery_kind = 'recovery',
                        delivery_status = 'suppressed',
                        detected_notified_at = notified_at,
                        notified_at = NULL
                    WHERE id = ?
                    """,
                    (retired_ts, incident_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE watchdog_incidents
                    SET delivery_status = 'suppressed', notified_at = NULL
                    WHERE id = ?
                    """,
                    (incident_id,),
                )
            cls._delete_notification_claim(
                connection,
                cls._watchdog_claim_key(incident_id),
            )
        return False

    @staticmethod
    def _validated_integrity_incident(row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            incident_id = int(row["id"])
            generation = int(row["generation"])
            scope = _require_text(str(row["scope_key"]), "scope")
            component = _low_cardinality_code(
                str(row["component"]), "component"
            )
            reason = _low_cardinality_code(
                str(row["reason_code"]), "reason_code"
            )
            digest = _optional_sha256(row.get("evidence_sha256"))
            first_seen = _parse_persisted_aware_timestamp(row["first_seen_at"])
            last_seen = _parse_persisted_aware_timestamp(row["last_seen_at"])
            resolved = (
                _parse_persisted_aware_timestamp(row["resolved_at"])
                if row.get("resolved_at") is not None
                else None
            )
            notified = (
                _parse_persisted_aware_timestamp(row["notified_at"])
                if row.get("notified_at") is not None
                else None
            )
            active_raw = row["active"]
            delivery_kind = row["delivery_kind"]
            delivery_status = row["delivery_status"]
            if incident_id <= 0 or generation <= 0 or active_raw not in (0, 1):
                raise ValueError("invalid integrity identity")
            if component is None or reason is None:
                raise ValueError("missing integrity code")
            if first_seen > last_seen:
                raise ValueError("integrity timestamps are out of order")
            active = bool(active_raw)
            if active == (resolved is not None):
                raise ValueError("integrity resolution state is inconsistent")
            if resolved is not None and resolved < last_seen:
                raise ValueError("integrity resolution predates last sighting")
            if delivery_kind not in {"detected", "activation_sync"}:
                raise ValueError("invalid integrity delivery kind")
            if delivery_status not in {"pending", "sent", "suppressed"}:
                raise ValueError("invalid integrity delivery status")
            if (delivery_status == "sent") != (notified is not None):
                raise ValueError("integrity notification state is inconsistent")
        except (KeyError, TypeError, ValueError):
            raise CorruptProtectionStateError(
                "persisted integrity incident evidence is corrupt"
            ) from None
        return {
            "id": incident_id,
            "scope": scope,
            "component": component,
            "generation": generation,
            "reason_code": reason,
            "evidence_sha256": digest,
            "first_seen_at": _timestamp(first_seen),
            "last_seen_at": _timestamp(last_seen),
            "resolved_at": _timestamp(resolved) if resolved is not None else None,
            "active": active,
            "delivery_kind": delivery_kind,
            "delivery_status": delivery_status,
            "notified_at": _timestamp(notified) if notified is not None else None,
        }

    def _resolve_integrity_incident_tx(
        self,
        connection: sqlite3.Connection,
        scope: str,
        component: str,
        resolved_at: datetime,
    ) -> bool:
        row = connection.execute(
            """
            SELECT * FROM integrity_incidents
            WHERE scope_key = ? AND component = ? AND active = 1
            """,
            (scope, component),
        ).fetchone()
        if row is None:
            return False
        current = self._validated_integrity_incident(dict(row))
        if resolved_at < _parse_timestamp(current["last_seen_at"]):
            raise ValueError("integrity resolution predates last sighting")
        status = (
            "suppressed"
            if current["delivery_status"] == "pending"
            else current["delivery_status"]
        )
        connection.execute(
            """
            UPDATE integrity_incidents
            SET active = 0, resolved_at = ?, delivery_status = ?
            WHERE id = ?
            """,
            (_timestamp(resolved_at), status, current["id"]),
        )
        self._delete_notification_claim(
            connection, self._integrity_claim_key(current["id"])
        )
        return True

    def observe_integrity_incident(
        self,
        scope: str,
        component: str,
        reason_code: str,
        *,
        evidence_sha256: str | None = None,
        delivery_status: Literal["pending", "suppressed"] = "suppressed",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Open or refresh one active integrity generation without storing raw data."""

        scope_key = _require_text(scope, "scope")
        component_code = _low_cardinality_code(component, "component")
        reason = _low_cardinality_code(reason_code, "reason_code")
        assert component_code is not None and reason is not None
        digest = _optional_sha256(evidence_sha256)
        if delivery_status not in {"pending", "suppressed"}:
            raise ValueError("delivery_status must be pending or suppressed")
        observed_at = _normalise_time(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM integrity_incidents
                WHERE scope_key = ? AND component = ? AND active = 1
                """,
                (scope_key, component_code),
            ).fetchone()
            if row is not None:
                current = self._validated_integrity_incident(dict(row))
                if observed_at < _parse_timestamp(current["last_seen_at"]):
                    raise ValueError("integrity observation is out of order")
                next_status = current["delivery_status"]
                next_kind = current["delivery_kind"]
                if delivery_status == "pending" and next_status == "suppressed":
                    next_status = "pending"
                    next_kind = "activation_sync"
                connection.execute(
                    """
                    UPDATE integrity_incidents
                    SET reason_code = ?, evidence_sha256 = ?, last_seen_at = ?,
                        delivery_kind = ?, delivery_status = ?
                    WHERE id = ?
                    """,
                    (
                        reason,
                        digest,
                        _timestamp(observed_at),
                        next_kind,
                        next_status,
                        current["id"],
                    ),
                )
                incident_id = current["id"]
            else:
                generation = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(generation), 0) + 1
                        FROM integrity_incidents
                        WHERE scope_key = ? AND component = ?
                        """,
                        (scope_key, component_code),
                    ).fetchone()[0]
                )
                cursor = connection.execute(
                    """
                    INSERT INTO integrity_incidents (
                        scope_key, component, generation, reason_code,
                        evidence_sha256, first_seen_at, last_seen_at,
                        resolved_at, active, delivery_kind, delivery_status,
                        notified_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1, 'detected', ?, NULL)
                    """,
                    (
                        scope_key,
                        component_code,
                        generation,
                        reason,
                        digest,
                        _timestamp(observed_at),
                        _timestamp(observed_at),
                        delivery_status,
                    ),
                )
                if cursor.lastrowid is None:  # pragma: no cover - SQLite contract
                    raise RuntimeError("SQLite did not return an integrity incident id")
                incident_id = int(cursor.lastrowid)
            persisted = connection.execute(
                "SELECT * FROM integrity_incidents WHERE id = ?",
                (incident_id,),
            ).fetchone()
            assert persisted is not None
            return self._validated_integrity_incident(dict(persisted))

    def resolve_integrity_incident(
        self,
        scope: str,
        component: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Atomically resolve the active generation and suppress any pending send."""

        scope_key = _require_text(scope, "scope")
        component_code = _low_cardinality_code(component, "component")
        assert component_code is not None
        resolved_at = _normalise_time(now)
        with self._transaction() as connection:
            return self._resolve_integrity_incident_tx(
                connection,
                scope_key,
                component_code,
                resolved_at,
            )

    def integrity_incidents(
        self,
        *,
        scope: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return validated hash-only integrity generations."""

        scope_key = _require_text(scope, "scope") if scope is not None else None
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM integrity_incidents"
            ).fetchall()
        # Validate the full integrity ledger before applying a caller's view;
        # corrupt resolved/history rows must never disappear behind a filter.
        incidents = [
            self._validated_integrity_incident(dict(row)) for row in rows
        ]
        if scope_key is not None:
            incidents = [item for item in incidents if item["scope"] == scope_key]
        if active_only:
            incidents = [item for item in incidents if item["active"]]
        incidents.sort(key=lambda item: (item["first_seen_at"], item["id"]), reverse=True)
        return incidents

    def pending_integrity_incidents(
        self,
        *,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        incidents = self.integrity_incidents(scope=scope, active_only=True)
        return [
            item
            for item in incidents
            if item["delivery_status"] == "pending"
            and item["notified_at"] is None
        ]

    def claim_integrity_notification(
        self,
        incident_id: int,
        *,
        lease_seconds: float = 300,
        now: datetime | None = None,
    ) -> str | None:
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id <= 0
        ):
            raise ValueError("incident_id must be a positive integer")
        claimed_at = _normalise_time(now)
        lease = _lease_duration(lease_seconds)
        claim_key = self._integrity_claim_key(incident_id)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM integrity_incidents WHERE id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown integrity incident: {incident_id}")
            current = self._validated_integrity_incident(dict(row))
            if (
                not current["active"]
                or current["delivery_status"] != "pending"
                or current["notified_at"] is not None
            ):
                self._delete_notification_claim(connection, claim_key)
                return None
            return self._acquire_notification_claim(
                connection,
                claim_key,
                claim_key,
                lease,
                claimed_at,
            )

    def mark_integrity_notified(
        self,
        incident_id: int,
        claim_token: str,
        *,
        now: datetime | None = None,
    ) -> None:
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id <= 0
        ):
            raise ValueError("incident_id must be a positive integer")
        token = _require_text(claim_token, "claim_token")
        completed_at = _normalise_time(now)
        claim_key = self._integrity_claim_key(incident_id)
        with self._transaction() as connection:
            self._require_notification_claim(
                connection, claim_key, token, completed_at
            )
            cursor = connection.execute(
                """
                UPDATE integrity_incidents
                SET delivery_status = 'sent', notified_at = ?
                WHERE id = ? AND active = 1 AND delivery_status = 'pending'
                """,
                (_timestamp(completed_at), incident_id),
            )
            if cursor.rowcount != 1:
                raise CorruptProtectionStateError(
                    "integrity incident changed before notification completion"
                )
            self._delete_notification_claim(connection, claim_key)

    def observe_watchdog_incident(
        self,
        *,
        scope_generation: str,
        enabled_instruments: int,
        affected_tickers: Sequence[str],
        markets: Sequence[str],
        window_keys: Sequence[str],
        first_seen_at: datetime,
        delivery_status: Literal["pending", "suppressed"] = "suppressed",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Open one durable global deadline generation without touching data state."""

        if delivery_status not in {"pending", "suppressed"}:
            raise ValueError("delivery_status must be pending or suppressed")
        payload_json, evidence_sha256, incoming_payload = _watchdog_incident_payload(
            scope_generation=scope_generation,
            enabled_instruments=enabled_instruments,
            affected_tickers=affected_tickers,
            markets=markets,
            window_keys=window_keys,
            first_seen_at=first_seen_at,
        )
        observed_at = _normalise_time(now)
        first_seen = _normalise_time(first_seen_at)
        if observed_at < first_seen:
            raise ValueError("watchdog observation predates first evidence")
        with self._transaction() as connection:
            incidents = self._watchdog_incidents_tx(
                connection, not_after=observed_at
            )
            active = next(
                (item for item in incidents if item["active"]),
                None,
            )
            if active is not None:
                if observed_at < _parse_timestamp(active["last_seen_at"]):
                    raise ValueError("watchdog observation is out of order")
                if (
                    active["evidence_sha256"] != evidence_sha256
                    and self._watchdog_claim_is_live_tx(
                        connection,
                        int(active["id"]),
                        observed_at,
                    )
                ):
                    # The claimed edge is immutable from the sender's
                    # linearization point through mark/release. Reconciliation
                    # retries the monotonic union or recovery on the next tick.
                    return active
                current_payload = active["payload"]
                if (
                    current_payload["scope_generation"]
                    == incoming_payload["scope_generation"]
                ):
                    if (
                        current_payload["enabled_instruments"]
                        != incoming_payload["enabled_instruments"]
                        or current_payload["first_seen_at"]
                        != incoming_payload["first_seen_at"]
                    ):
                        raise ValueError(
                            "watchdog evidence changed within one scope generation"
                        )
                    for field in ("affected_tickers", "markets", "window_keys"):
                        if not set(current_payload[field]).issubset(
                            incoming_payload[field]
                        ):
                            raise ValueError(
                                "watchdog evidence cannot shrink within a generation"
                            )
                    if active["evidence_sha256"] == evidence_sha256:
                        return active
                    connection.execute(
                        """
                        UPDATE watchdog_incidents
                        SET evidence_sha256 = ?, payload_json = ?, last_seen_at = ?
                        WHERE id = ?
                        """,
                        (
                            evidence_sha256,
                            payload_json,
                            _timestamp(observed_at),
                            active["id"],
                        ),
                    )
                    persisted = connection.execute(
                        "SELECT * FROM watchdog_incidents WHERE id = ?",
                        (active["id"],),
                    ).fetchone()
                    assert persisted is not None
                    return _validated_watchdog_incident(dict(persisted))
                detected_notified = active["notified_at"]
                connection.execute(
                    """
                    UPDATE watchdog_incidents
                    SET state = 'RECOVERED', active = 0, resolved_at = ?,
                        delivery_kind = 'recovery',
                        delivery_status = 'suppressed',
                        detected_notified_at = ?, notified_at = NULL
                    WHERE id = ?
                    """,
                    (
                        _timestamp(observed_at),
                        detected_notified,
                        active["id"],
                    ),
                )
                self._delete_notification_claim(
                    connection,
                    self._watchdog_claim_key(int(active["id"])),
                )
            elif incidents:
                latest = incidents[-1]
                if latest["evidence_sha256"] == evidence_sha256:
                    return latest

            self._suppress_watchdog_pending_tx(connection, incidents)
            generation = (
                max((int(item["generation"]) for item in incidents), default=0)
                + 1
            )
            cursor = connection.execute(
                """
                INSERT INTO watchdog_incidents (
                    generation, state, active, evidence_sha256, payload_json,
                    first_seen_at, last_seen_at, resolved_at,
                    delivery_kind, delivery_status,
                    detected_notified_at, notified_at
                ) VALUES (?, 'BLIND', 1, ?, ?, ?, ?, NULL,
                          'detected', ?, NULL, NULL)
                """,
                (
                    generation,
                    evidence_sha256,
                    payload_json,
                    _timestamp(first_seen),
                    _timestamp(observed_at),
                    delivery_status,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite contract
                raise RuntimeError("SQLite did not return a watchdog incident id")
            persisted = connection.execute(
                "SELECT * FROM watchdog_incidents WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            assert persisted is not None
            return _validated_watchdog_incident(dict(persisted))

    def resolve_watchdog_incident(
        self,
        *,
        delivery_status: Literal["pending", "suppressed"] = "suppressed",
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Resolve the active deadline generation exactly once."""

        if delivery_status not in {"pending", "suppressed"}:
            raise ValueError("delivery_status must be pending or suppressed")
        resolved_at = _normalise_time(now)
        with self._transaction() as connection:
            incidents = self._watchdog_incidents_tx(
                connection, not_after=resolved_at
            )
            active = next(
                (item for item in incidents if item["active"]),
                None,
            )
            if active is None:
                return None
            if resolved_at < _parse_timestamp(active["last_seen_at"]):
                raise ValueError("watchdog resolution predates last evidence")
            scope_row = connection.execute(
                "SELECT 1 FROM protection_scope WHERE scope_key = 'global'"
            ).fetchone()
            if scope_row is not None:
                scope_record, generations = self._scope_with_generations_tx(
                    connection,
                    scope_key="global",
                    not_after=resolved_at,
                )
                incident_generation = active["payload"]["scope_generation"]
                known_generations = {
                    item["generation"] for item in generations
                }
                if incident_generation not in known_generations:
                    raise CorruptProtectionStateError(
                        "watchdog incident generation is unknown"
                    )
                if incident_generation != scope_record["watchdog_generation"]:
                    if self._watchdog_claim_is_live_tx(
                        connection,
                        int(active["id"]),
                        resolved_at,
                    ):
                        return active
                    self._retire_watchdog_scope_generation_tx(
                        connection,
                        [active],
                        expected_generation=incident_generation,
                        retired_at=resolved_at,
                    )
                    persisted = connection.execute(
                        "SELECT * FROM watchdog_incidents WHERE id = ?",
                        (active["id"],),
                    ).fetchone()
                    assert persisted is not None
                    return _validated_watchdog_incident(dict(persisted))
            if self._watchdog_claim_is_live_tx(
                connection,
                int(active["id"]),
                resolved_at,
            ):
                return active
            self._suppress_watchdog_pending_tx(connection, incidents)
            recovered_status = (
                "pending"
                if delivery_status == "pending"
                and active["delivery_status"] in {"pending", "sent"}
                else "suppressed"
            )
            detected_notified = active["notified_at"]
            connection.execute(
                """
                UPDATE watchdog_incidents
                SET state = 'RECOVERED', active = 0, resolved_at = ?,
                    delivery_kind = 'recovery', delivery_status = ?,
                    detected_notified_at = ?, notified_at = NULL
                WHERE id = ?
                """,
                (
                    _timestamp(resolved_at),
                    recovered_status,
                    detected_notified,
                    active["id"],
                ),
            )
            self._delete_notification_claim(
                connection,
                self._watchdog_claim_key(int(active["id"])),
            )
            persisted = connection.execute(
                "SELECT * FROM watchdog_incidents WHERE id = ?",
                (active["id"],),
            ).fetchone()
            assert persisted is not None
            return _validated_watchdog_incident(dict(persisted))

    def watchdog_incidents(
        self,
        *,
        active_only: bool = False,
        not_after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return the validated complete watchdog incident ledger."""

        if not isinstance(active_only, bool):
            raise TypeError("active_only must be a bool")
        self._ensure_open()
        with self._lock:
            incidents = self._watchdog_incidents_tx(
                self._connection,
                not_after=not_after,
            )
        if active_only:
            incidents = [item for item in incidents if item["active"]]
        incidents.sort(
            key=lambda item: (item["generation"], item["id"]),
            reverse=True,
        )
        return incidents

    def watchdog_incidents_with_scope_proof(
        self,
        *,
        scope: str = "global",
        not_after: datetime,
    ) -> list[dict[str, Any]]:
        """Validate every watchdog row against the authenticated scope chain."""

        scope_key = _require_text(scope, "scope")
        cutoff = _normalise_time(not_after)
        self._ensure_open()
        with self._lock:
            incidents = self._watchdog_incidents_tx(
                self._connection,
                not_after=cutoff,
            )
            scope_row = self._connection.execute(
                "SELECT 1 FROM protection_scope WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if scope_row is None:
                self._scope_generations_tx(
                    self._connection,
                    scope_record=None,
                    scope_key=scope_key,
                    not_after=cutoff,
                )
                if incidents:
                    raise CorruptProtectionStateError(
                        "watchdog incident has no authenticated scope"
                    )
            else:
                _scope_record, generations = self._scope_with_generations_tx(
                    self._connection,
                    scope_key=scope_key,
                    not_after=cutoff,
                )
                authenticated = {
                    item["generation"] for item in generations
                }
                if any(
                    item["payload"]["scope_generation"] not in authenticated
                    for item in incidents
                ):
                    raise CorruptProtectionStateError(
                        "watchdog incident generation is unauthenticated"
                    )
        incidents.sort(
            key=lambda item: (item["generation"], item["id"]),
            reverse=True,
        )
        return incidents

    def ensure_current_watchdog_incident_pending(
        self,
        *,
        now: datetime | None = None,
    ) -> int | None:
        """Atomically activate Preview's current BLIND generation for delivery."""

        activated_at = _normalise_time(now)
        with self._transaction() as connection:
            incidents = self._watchdog_incidents_tx(
                connection, not_after=activated_at
            )
            active = next(
                (item for item in incidents if item["active"]),
                None,
            )
            if active is None:
                return None
            if activated_at < _parse_timestamp(active["last_seen_at"]):
                raise ValueError("watchdog activation predates current evidence")
            scope_row = connection.execute(
                "SELECT 1 FROM protection_scope WHERE scope_key = 'global'"
            ).fetchone()
            if scope_row is not None:
                scope_record, generations = self._scope_with_generations_tx(
                    connection,
                    scope_key="global",
                    not_after=activated_at,
                )
                incident_generation = active["payload"]["scope_generation"]
                known_generations = {
                    item["generation"] for item in generations
                }
                if incident_generation not in known_generations:
                    raise CorruptProtectionStateError(
                        "watchdog incident generation is unknown"
                    )
                if incident_generation != scope_record["watchdog_generation"]:
                    if not self._watchdog_claim_is_live_tx(
                        connection,
                        int(active["id"]),
                        activated_at,
                    ):
                        self._retire_watchdog_scope_generation_tx(
                            connection,
                            [active],
                            expected_generation=incident_generation,
                            retired_at=activated_at,
                        )
                    return None
            if active["delivery_status"] == "suppressed":
                self._suppress_watchdog_pending_tx(connection, incidents)
                connection.execute(
                    """
                    UPDATE watchdog_incidents
                    SET delivery_kind = 'activation_sync',
                        delivery_status = 'pending'
                    WHERE id = ?
                    """,
                    (active["id"],),
                )
            return int(active["id"])

    def pending_watchdog_incident(
        self,
        *,
        not_after: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return the single current pending BLIND or RECOVERED generation."""

        incidents = self.watchdog_incidents(not_after=not_after)
        return next(
            (
                item
                for item in incidents
                if item["delivery_status"] == "pending"
                and item["notified_at"] is None
            ),
            None,
        )

    def read_claimed_watchdog_incident(
        self,
        incident_id: int,
        claim_token: str,
        *,
        not_after: datetime,
    ) -> dict[str, Any] | None:
        """Atomically revalidate the exact watchdog edge leased for delivery."""

        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id <= 0
        ):
            raise ValueError("incident_id must be a positive integer")
        token = _require_text(claim_token, "claim_token")
        checked_at = _normalise_time(not_after)
        claim_key = self._watchdog_claim_key(incident_id)
        with self._transaction() as connection:
            incidents = self._watchdog_incidents_tx(
                connection,
                not_after=checked_at,
            )
            claim = connection.execute(
                """
                SELECT claim_token, claimed_at, expires_at
                FROM notification_claims
                WHERE claim_key = ?
                """,
                (claim_key,),
            ).fetchone()
            if claim is None or claim["claim_token"] != token:
                return None
            try:
                claimed_at = _parse_timestamp(claim["claimed_at"])
                expires_at = _parse_timestamp(claim["expires_at"])
            except (TypeError, ValueError):
                raise CorruptProtectionStateError(
                    "persisted watchdog delivery claim is corrupt"
                ) from None
            if not claimed_at <= checked_at < expires_at:
                self._delete_notification_claim(connection, claim_key)
                return None
            current = next(
                (item for item in incidents if item["id"] == incident_id),
                None,
            )
            if (
                current is None
                or current["delivery_status"] != "pending"
                or current["notified_at"] is not None
            ):
                self._delete_notification_claim(connection, claim_key)
                return None
            scope_record, generations = self._scope_with_generations_tx(
                connection,
                scope_key="global",
                not_after=checked_at,
            )
            incident_generation = current["payload"]["scope_generation"]
            current_generation = scope_record["watchdog_generation"]
            if incident_generation != current_generation:
                generation_row = next(
                    (
                        item
                        for item in generations
                        if item["generation"] == incident_generation
                    ),
                    None,
                )
                if generation_row is None:
                    raise CorruptProtectionStateError(
                        "watchdog incident generation is unknown"
                    )
                superseded_at = (
                    _parse_timestamp(generation_row["superseded_at"])
                    if generation_row["superseded_at"] is not None
                    else None
                )
                if not (
                    superseded_at is not None
                    and claimed_at <= superseded_at < expires_at
                ):
                    self._delete_notification_claim(connection, claim_key)
                    self._retire_watchdog_scope_generation_tx(
                        connection,
                        [current],
                        expected_generation=incident_generation,
                        retired_at=checked_at,
                    )
                    return None
            return current

    def claim_watchdog_incident_notification(
        self,
        incident_id: int,
        *,
        lease_seconds: float = 300,
        now: datetime | None = None,
    ) -> str | None:
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id <= 0
        ):
            raise ValueError("incident_id must be a positive integer")
        claimed_at = _normalise_time(now)
        lease = _lease_duration(lease_seconds)
        claim_key = self._watchdog_claim_key(incident_id)
        with self._transaction() as connection:
            incidents = self._watchdog_incidents_tx(
                connection, not_after=claimed_at
            )
            current = next(
                (item for item in incidents if item["id"] == incident_id),
                None,
            )
            if current is None:
                raise KeyError(f"unknown watchdog incident: {incident_id}")
            if (
                current["delivery_status"] != "pending"
                or current["notified_at"] is not None
            ):
                self._delete_notification_claim(connection, claim_key)
                return None
            scope_record, generations = self._scope_with_generations_tx(
                connection,
                scope_key="global",
                not_after=claimed_at,
            )
            incident_generation = current["payload"]["scope_generation"]
            current_generation = scope_record["watchdog_generation"]
            known_generations = {
                item["generation"] for item in generations
            }
            if incident_generation not in known_generations:
                raise CorruptProtectionStateError(
                    "watchdog incident generation is unknown"
                )
            if incident_generation != current_generation:
                self._retire_watchdog_scope_generation_tx(
                    connection,
                    [current],
                    expected_generation=incident_generation,
                    retired_at=claimed_at,
                )
                return None
            return self._acquire_notification_claim(
                connection,
                claim_key,
                claim_key,
                lease,
                claimed_at,
            )

    def mark_watchdog_incident_notified(
        self,
        incident_id: int,
        claim_token: str,
        *,
        now: datetime | None = None,
    ) -> None:
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id <= 0
        ):
            raise ValueError("incident_id must be a positive integer")
        token = _require_text(claim_token, "claim_token")
        completed_at = _normalise_time(now)
        claim_key = self._watchdog_claim_key(incident_id)
        with self._transaction() as connection:
            incidents = self._watchdog_incidents_tx(
                connection, not_after=completed_at
            )
            current = next(
                (item for item in incidents if item["id"] == incident_id),
                None,
            )
            if current is None:
                raise KeyError(f"unknown watchdog incident: {incident_id}")
            self._require_notification_claim(
                connection,
                claim_key,
                token,
                completed_at,
            )
            superseded_generation = False
            scope_row = connection.execute(
                "SELECT 1 FROM protection_scope WHERE scope_key = 'global'"
            ).fetchone()
            if scope_row is not None:
                scope_record, generations = self._scope_with_generations_tx(
                    connection,
                    scope_key="global",
                    not_after=completed_at,
                )
                incident_generation = current["payload"]["scope_generation"]
                generation_row = next(
                    (
                        item
                        for item in generations
                        if item["generation"] == incident_generation
                    ),
                    None,
                )
                if generation_row is None:
                    raise CorruptProtectionStateError(
                        "watchdog incident generation is unknown"
                    )
                superseded_generation = (
                    incident_generation != scope_record["watchdog_generation"]
                )
                if superseded_generation:
                    claim_row = connection.execute(
                        """
                        SELECT claimed_at, expires_at
                        FROM notification_claims
                        WHERE claim_key = ? AND claim_token = ?
                        """,
                        (claim_key, token),
                    ).fetchone()
                    assert claim_row is not None
                    try:
                        claimed_at = _parse_timestamp(claim_row["claimed_at"])
                        expires_at = _parse_timestamp(claim_row["expires_at"])
                        superseded_at = _parse_timestamp(
                            generation_row["superseded_at"]
                        )
                    except (TypeError, ValueError):
                        raise CorruptProtectionStateError(
                            "persisted watchdog generation claim is corrupt"
                        ) from None
                    if not claimed_at <= superseded_at < expires_at:
                        raise CorruptProtectionStateError(
                            "watchdog claim does not belong to superseded generation"
                        )
            if superseded_generation and current["state"] == "BLIND":
                cursor = connection.execute(
                    """
                    UPDATE watchdog_incidents
                    SET state = 'RECOVERED', active = 0, resolved_at = ?,
                        delivery_kind = 'recovery',
                        delivery_status = 'suppressed',
                        detected_notified_at = ?, notified_at = NULL
                    WHERE id = ? AND delivery_status = 'pending'
                    """,
                    (
                        _timestamp(completed_at),
                        _timestamp(completed_at),
                        incident_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE watchdog_incidents
                    SET delivery_status = 'sent', notified_at = ?
                    WHERE id = ? AND delivery_status = 'pending'
                    """,
                    (_timestamp(completed_at), incident_id),
                )
            if cursor.rowcount != 1:
                raise CorruptProtectionStateError(
                    "watchdog incident changed before notification completion"
                )
            self._delete_notification_claim(connection, claim_key)

    def claim_incident_notification(
        self,
        event_id: int,
        *,
        lease_seconds: float = 300,
        now: datetime | None = None,
    ) -> str | None:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("event_id must be a positive integer")
        lease = _lease_duration(lease_seconds)
        claimed_at = _normalise_time(now)
        claim_key = self._incident_claim_key(event_id)
        with self._transaction() as connection:
            event = connection.execute(
                """
                SELECT scope_key, current_state, incident_id, payload_json,
                       notified_at, delivery_status
                FROM protection_events WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
            if event is None:
                raise KeyError(f"unknown protection event: {event_id}")
            if (
                event["delivery_status"] != "pending"
                or event["notified_at"] is not None
            ):
                self._delete_notification_claim(connection, claim_key)
                return None
            state_row = connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = ?",
                (event["scope_key"],),
            ).fetchone()
            if state_row is None:
                raise CorruptProtectionStateError(
                    "pending incident has no current protection state"
                )
            try:
                current = ProtectionSnapshot.model_validate_json(
                    state_row["snapshot_json"]
                )
                payload = ProtectionSnapshot.model_validate_json(
                    event["payload_json"]
                )
                if (
                    current.scope != event["scope_key"]
                    or current.state.value != event["current_state"]
                    or current.incident_id != event["incident_id"]
                    or payload.scope != event["scope_key"]
                    or payload.state.value != event["current_state"]
                    or payload.incident_id != event["incident_id"]
                ):
                    raise ValueError("pending incident evidence mismatch")
            except (TypeError, ValueError):
                raise CorruptProtectionStateError(
                    "persisted incident delivery evidence is corrupt"
                ) from None
            return self._acquire_notification_claim(
                connection,
                claim_key,
                claim_key,
                lease,
                claimed_at,
            )

    def mark_incident_notified(
        self,
        event_id: int,
        claim_token: str,
        *,
        now: datetime | None = None,
    ) -> None:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("event_id must be a positive integer")
        token = _require_text(claim_token, "claim_token")
        completed_at = _normalise_time(now)
        claim_key = self._incident_claim_key(event_id)
        with self._transaction() as connection:
            self._require_notification_claim(
                connection, claim_key, token, completed_at
            )
            cursor = connection.execute(
                """
                UPDATE protection_events
                SET notified_at = ?, delivery_status = 'sent'
                WHERE id = ?
                """,
                (_timestamp(completed_at), event_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown protection event: {event_id}")
            self._delete_notification_claim(connection, claim_key)

    def suppress_incident_notification(
        self,
        event_id: int,
    ) -> bool:
        """Mark a Preview/uninteresting edge as intentionally non-deliverable."""

        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("event_id must be a positive integer")
        claim_key = self._incident_claim_key(event_id)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE protection_events
                SET delivery_status = 'suppressed'
                WHERE id = ? AND delivery_status = 'pending'
                """,
                (event_id,),
            )
            self._delete_notification_claim(connection, claim_key)
            return cursor.rowcount == 1

    def repair_corrupt_protection_event(
        self,
        *,
        scope: str = "global",
        now: datetime | None = None,
    ) -> tuple[str, int]:
        """Quarantine one corrupt current pending edge and suppress its claim."""

        scope_key = _require_text(scope, "scope")
        repaired_at = _normalise_time(now)
        with self._transaction() as connection:
            state_row = connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if state_row is None:
                raise KeyError(f"no protection state exists for scope: {scope_key}")
            try:
                current = ProtectionSnapshot.model_validate_json(
                    state_row["snapshot_json"]
                )
                if current.scope != scope_key:
                    raise ValueError("snapshot scope does not match row key")
            except (TypeError, ValueError):
                raise CorruptProtectionStateError(
                    "persisted protection state is corrupt"
                ) from None
            rows = connection.execute(
                """
                SELECT * FROM protection_events
                WHERE scope_key = ?
                  AND current_state = ?
                  AND delivery_status = 'pending'
                  AND notified_at IS NULL
                  AND (
                      incident_id = ?
                      OR (incident_id IS NULL AND ? IS NULL)
                  )
                ORDER BY occurred_at DESC, id DESC
                """,
                (
                    scope_key,
                    current.state.value,
                    current.incident_id,
                    current.incident_id,
                ),
            ).fetchall()
            corrupt_row: sqlite3.Row | None = None
            for row in rows:
                try:
                    payload = ProtectionSnapshot.model_validate_json(
                        row["payload_json"]
                    )
                    if (
                        payload.scope != scope_key
                        or payload.state is not current.state
                        or payload.incident_id != current.incident_id
                        or row["current_state"] != current.state.value
                        or row["incident_id"] != current.incident_id
                    ):
                        raise ValueError("event payload does not match current state")
                except (TypeError, ValueError):
                    corrupt_row = row
                    break
            if corrupt_row is None:
                raise ValueError("no corrupt current protection event was found")
            raw_payload = _json_payload(dict(corrupt_row))
            digest = hashlib.sha256(raw_payload.encode()).hexdigest()
            event_id = int(corrupt_row["id"])
            connection.execute(
                """
                INSERT INTO protection_event_quarantine (
                    scope_key, event_id, payload_sha256,
                    raw_payload, quarantined_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scope_key,
                    event_id,
                    digest,
                    raw_payload,
                    _timestamp(repaired_at),
                ),
            )
            connection.execute(
                """
                UPDATE protection_events
                SET delivery_status = 'suppressed'
                WHERE id = ?
                """,
                (event_id,),
            )
            self._delete_notification_claim(
                connection, self._incident_claim_key(event_id)
            )
            self._resolve_integrity_incident_tx(
                connection,
                scope_key,
                "protection_event",
                repaired_at,
            )
            return digest, event_id

    def ensure_current_incident_pending(
        self,
        scope: str = "global",
        *,
        now: datetime | None = None,
    ) -> int | None:
        """Idempotently expose only the current non-healthy incident to ACTIVE."""

        scope_key = _require_text(scope, "scope")
        activated_at = _normalise_time(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if row is None:
                return None
            try:
                snapshot = ProtectionSnapshot.model_validate_json(
                    row["snapshot_json"]
                )
                if snapshot.scope != scope_key:
                    raise ValueError("snapshot scope does not match row key")
            except (TypeError, ValueError):
                raise CorruptProtectionStateError(
                    "persisted protection state is corrupt"
                ) from None
            if snapshot.state.value == "HEALTHY":
                self._suppress_pending_incident_events_tx(
                    connection,
                    scope_key,
                    except_state="HEALTHY",
                    except_incident_id=snapshot.incident_id,
                )
                recovered = connection.execute(
                    """
                    SELECT id
                    FROM protection_events
                    WHERE scope_key = ?
                      AND current_state = 'HEALTHY'
                      AND event_type = 'recovered'
                      AND delivery_status IN ('pending', 'sent')
                      AND (
                          incident_id = ?
                          OR (incident_id IS NULL AND ? IS NULL)
                      )
                    ORDER BY occurred_at DESC, id DESC
                    LIMIT 1
                    """,
                    (scope_key, snapshot.incident_id, snapshot.incident_id),
                ).fetchone()
                return int(recovered["id"]) if recovered is not None else None
            if snapshot.state.value not in {"BLIND", "DEGRADED", "RECOVERING"}:
                self._suppress_pending_incident_events_tx(connection, scope_key)
                return None
            self._suppress_pending_incident_events_tx(
                connection,
                scope_key,
                except_state=snapshot.state.value,
                except_incident_id=snapshot.incident_id,
            )
            existing = connection.execute(
                """
                SELECT id
                FROM protection_events
                WHERE scope_key = ?
                  AND current_state = ?
                  AND delivery_status IN ('pending', 'sent')
                  AND event_type IN (
                      'blind', 'degraded', 'recovering', 'activation_sync'
                  )
                  AND (
                      incident_id = ?
                      OR (incident_id IS NULL AND ? IS NULL)
                  )
                ORDER BY occurred_at DESC, id DESC
                LIMIT 1
                """,
                (
                    scope_key,
                    snapshot.state.value,
                    snapshot.incident_id,
                    snapshot.incident_id,
                ),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            payload = _json_payload(snapshot.model_dump(mode="json"))
            cursor = connection.execute(
                """
                INSERT INTO protection_events (
                    scope_key, event_type, previous_state, current_state,
                    incident_id, occurred_at, payload_json, notified_at,
                    delivery_status
                ) VALUES (?, 'activation_sync', ?, ?, ?, ?, ?, NULL, 'pending')
                """,
                (
                    scope_key,
                    snapshot.state.value,
                    snapshot.state.value,
                    snapshot.incident_id,
                    _timestamp(activated_at),
                    payload,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite contract
                raise RuntimeError("SQLite did not return an activation event id")
            return int(cursor.lastrowid)

    def pending_current_incident_event(
        self,
        scope: str,
    ) -> dict[str, Any] | None:
        """Return only a pending edge still relevant to the current state."""

        snapshot = self.load_protection_state(scope)
        if snapshot is None:
            return None
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, scope_key, event_type, previous_state, current_state,
                       incident_id, occurred_at, payload_json, notified_at,
                       delivery_status
                FROM protection_events
                WHERE scope_key = ?
                  AND current_state = ?
                  AND delivery_status = 'pending'
                  AND notified_at IS NULL
                  AND event_type IN (
                      'blind', 'degraded', 'recovering', 'recovered',
                      'activation_sync'
                  )
                  AND (
                      incident_id = ?
                      OR (incident_id IS NULL AND ? IS NULL)
                  )
                ORDER BY occurred_at DESC, id DESC
                LIMIT 1
                """,
                (
                    scope,
                    snapshot.state.value,
                    snapshot.incident_id,
                    snapshot.incident_id,
                ),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            payload = ProtectionSnapshot.model_validate_json(
                result.pop("payload_json")
            )
            if (
                payload.scope != result["scope_key"]
                or payload.state.value != result["current_state"]
                or payload.incident_id != result["incident_id"]
                or payload.scope != snapshot.scope
                or payload.state is not snapshot.state
                or payload.incident_id != snapshot.incident_id
            ):
                raise ValueError("incident payload does not match event/current state")
            # The immutable edge payload proves what was observed at the edge;
            # notification content must use the latest same-incident snapshot
            # so repeated non-edge observations refresh impact/reasons.
            result["payload"] = snapshot.model_dump(mode="json")
        except (TypeError, ValueError):
            raise CorruptProtectionStateError(
                "persisted incident delivery evidence is corrupt"
            ) from None
        return result

    @staticmethod
    def _validate_scope_generation_chain(
        scope_record: Mapping[str, Any],
        generations: Sequence[Mapping[str, Any]],
    ) -> None:
        persisted_current = scope_record.get("watchdog_generation")
        if persisted_current is None:
            if generations:
                raise CorruptProtectionStateError(
                    "scope generation ledger exists without current proof"
                )
            return
        current_rows = [
            item for item in generations if item["superseded_at"] is None
        ]
        if (
            not generations
            or len(current_rows) != 1
            or current_rows[0]["generation"] != persisted_current
        ):
            raise CorruptProtectionStateError(
                "scope generation ledger does not match current scope"
            )
        for index, item in enumerate(generations[:-1]):
            following = generations[index + 1]
            if (
                item["superseded_at"] is None
                or item["superseded_at"] != following["activated_at"]
            ):
                raise CorruptProtectionStateError(
                    "scope generation ledger chronology is corrupt"
                )
        if generations[-1]["superseded_at"] is not None:
            raise CorruptProtectionStateError(
                "scope generation ledger has no current generation"
            )

    @staticmethod
    def _scope_generations_tx(
        connection: sqlite3.Connection,
        *,
        scope_record: Mapping[str, Any] | None,
        scope_key: str,
        not_after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM protection_scope_generations"
        ).fetchall()
        all_generations = [
            _validated_scope_generation_row(dict(row), not_after=not_after)
            for row in rows
        ]
        persisted_scope_records: dict[str, dict[str, Any]] = {}
        for raw_scope in connection.execute(
            "SELECT * FROM protection_scope"
        ).fetchall():
            try:
                raw_scope_key = raw_scope["scope_key"]
                if not isinstance(raw_scope_key, str):
                    raise ValueError("scope key must be text")
                persisted_scope_records[raw_scope_key] = _validated_scope_row(
                    dict(raw_scope), expected_scope=raw_scope_key
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise CorruptProtectionStateError(
                    "persisted protection scope is corrupt"
                ) from None
        if any(
            item["scope"] not in persisted_scope_records
            for item in all_generations
        ):
            raise CorruptProtectionStateError(
                "scope generation ledger contains an orphan row"
            )
        grouped: dict[str, list[dict[str, Any]]] = {
            persisted_scope: [] for persisted_scope in persisted_scope_records
        }
        for item in all_generations:
            grouped[item["scope"]].append(item)
        for persisted_scope, persisted_record in persisted_scope_records.items():
            grouped[persisted_scope].sort(key=lambda item: item["id"])
            StateStore._validate_scope_generation_chain(
                persisted_record,
                grouped[persisted_scope],
            )
        generations = grouped.get(scope_key, [])
        if scope_record is None and generations:
            raise CorruptProtectionStateError(
                "scope generation ledger contains an orphan row"
            )
        return generations

    @classmethod
    def _scope_with_generations_tx(
        cls,
        connection: sqlite3.Connection,
        *,
        scope_key: str,
        not_after: datetime,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = connection.execute(
            "SELECT * FROM protection_scope WHERE scope_key = ?",
            (scope_key,),
        ).fetchone()
        if row is None:
            raise CorruptProtectionStateError(
                "watchdog incident has no protection scope"
            )
        try:
            scope_record = _validated_scope_row(
                dict(row), expected_scope=scope_key
            )
            cutoff = _normalise_time(not_after)
            scope_times = (
                _parse_timestamp(scope_record["activated_at"]),
                _parse_timestamp(scope_record["updated_at"]),
                *(
                    _parse_timestamp(value)
                    for value in scope_record["market_epochs"].values()
                ),
            )
            if any(value > cutoff for value in scope_times):
                raise ValueError("scope generation is future-dated")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise CorruptProtectionStateError(
                "persisted protection scope is corrupt"
            ) from None
        generations = cls._scope_generations_tx(
            connection,
            scope_record=scope_record,
            scope_key=scope_key,
            not_after=cutoff,
        )
        return scope_record, generations

    @classmethod
    def _transition_scope_generation_tx(
        cls,
        connection: sqlite3.Connection,
        *,
        current_scope: Mapping[str, Any] | None,
        next_scope: Mapping[str, Any],
        changed_at: datetime,
    ) -> str:
        scope_key = _require_text(next_scope["scope"], "scope")
        generation = watchdog_scope_generation(next_scope)
        generation_rows = cls._scope_generations_tx(
            connection,
            scope_record=current_scope,
            scope_key=scope_key,
            not_after=changed_at,
        )
        current_generation = (
            current_scope.get("watchdog_generation")
            if current_scope is not None
            else None
        )
        if scope_key == "global":
            incidents = cls._watchdog_incidents_tx(
                connection,
                not_after=changed_at,
            )
            outstanding = [
                incident
                for incident in incidents
                if incident["active"]
                or (
                    incident["delivery_status"] == "pending"
                    and incident["notified_at"] is None
                )
            ]
            known_generations = {
                item["generation"] for item in generation_rows
            }
            if outstanding and (
                current_generation is None
                or any(
                    incident["payload"]["scope_generation"]
                    not in known_generations
                    for incident in outstanding
                )
            ):
                raise CorruptProtectionStateError(
                    "watchdog incident has no authenticated scope generation"
                )
            for incident_generation in sorted(
                {
                    incident["payload"]["scope_generation"]
                    for incident in outstanding
                    if incident["payload"]["scope_generation"]
                    != current_generation
                }
            ):
                cls._retire_watchdog_scope_generation_tx(
                    connection,
                    [
                        incident
                        for incident in outstanding
                        if incident["payload"]["scope_generation"]
                        == incident_generation
                    ],
                    expected_generation=incident_generation,
                    retired_at=changed_at,
                )
            if generation != current_generation and current_generation is not None:
                cls._retire_watchdog_scope_generation_tx(
                    connection,
                    [
                        incident
                        for incident in outstanding
                        if incident["payload"]["scope_generation"]
                        == current_generation
                    ],
                    expected_generation=current_generation,
                    retired_at=changed_at,
                )
        if generation != current_generation:
            if current_generation is not None:
                cursor = connection.execute(
                    """
                    UPDATE protection_scope_generations
                    SET superseded_at = ?
                    WHERE scope_key = ? AND generation = ?
                      AND superseded_at IS NULL
                    """,
                    (
                        _timestamp(changed_at),
                        scope_key,
                        current_generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CorruptProtectionStateError(
                        "scope generation ledger changed during update"
                    )
            connection.execute(
                """
                INSERT INTO protection_scope_generations (
                    scope_key, generation, activated_at, superseded_at
                ) VALUES (?, ?, ?, NULL)
                """,
                (scope_key, generation, _timestamp(changed_at)),
            )
        return generation

    def set_protection_scope(
        self,
        enabled_markets: Sequence[str],
        *,
        enabled_instruments_by_market: Mapping[str, Sequence[str]] | None = None,
        market_contract_hashes: Mapping[str, str] | None = None,
        scope: str = "global",
        paused: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        scope_key = _require_text(scope, "scope")
        if not isinstance(paused, bool):
            raise TypeError("paused must be a bool")
        markets = tuple(
            sorted(
                dict.fromkeys(
                    _require_text(item, "market").strip().upper()
                    for item in enabled_markets
                )
            )
        )
        for market in markets:
            if len(market) > 16 or not all(
                character.isalnum() or character in "_.-" for character in market
            ):
                raise ValueError("market must be a low-cardinality identifier")
        requested_hashes: dict[str, str] | None = None
        if enabled_instruments_by_market is not None:
            if set(enabled_instruments_by_market) != set(markets):
                raise ValueError(
                    "instrument identity markets must match enabled markets"
                )
            requested_hashes = {
                market: instrument_set_hash(enabled_instruments_by_market[market])
                for market in markets
            }
        requested_contracts = (
            _market_digest_map(
                market_contract_hashes,
                markets,
                label="contract identity",
            )
            if market_contract_hashes is not None
            else None
        )
        changed_at = _normalise_time(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM protection_scope WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            existing: dict[str, Any] | None = None
            if row is not None:
                try:
                    existing = _validated_scope_row(
                        dict(row), expected_scope=scope_key
                    )
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    raise CorruptProtectionStateError(
                        "persisted protection scope is corrupt"
                    ) from None
            if existing is not None:
                previous_updated = _parse_persisted_aware_timestamp(
                    existing["updated_at"]
                )
                if changed_at < previous_updated:
                    raise ValueError("protection scope update is out of order")
                previous_epochs = {
                    market: _parse_persisted_aware_timestamp(epoch)
                    for market, epoch in existing["market_epochs"].items()
                }
                previous_hashes = existing["market_instrument_hashes"]
                previous_contracts = existing["market_contract_hashes"]
            else:
                previous_epochs = {}
                previous_hashes = {}
                previous_contracts = {}
            instrument_hashes = (
                {
                    market: previous_hashes[market]
                    for market in markets
                    if market in previous_hashes
                }
                if requested_hashes is None
                else requested_hashes
            )
            contract_hashes = (
                {
                    market: previous_contracts[market]
                    for market in markets
                    if market in previous_contracts
                }
                if requested_contracts is None
                else requested_contracts
            )
            market_epochs = {
                market: (
                    previous_epochs[market]
                    if market in previous_epochs
                    and (
                        requested_hashes is None
                        or instrument_hashes.get(market)
                        == previous_hashes.get(market)
                    )
                    and (
                        requested_contracts is None
                        or contract_hashes.get(market)
                        == previous_contracts.get(market)
                    )
                    else changed_at
                )
                for market in markets
            }
            activated_at = (
                min(market_epochs.values()) if market_epochs else changed_at
            )
            scope_evidence = {
                "scope": scope_key,
                "activated_at": _timestamp(activated_at),
                "enabled_markets": list(markets),
                "market_epochs": {
                    market: _timestamp(epoch)
                    for market, epoch in market_epochs.items()
                },
                "market_instrument_hashes": dict(instrument_hashes),
                "market_contract_hashes": dict(contract_hashes),
                "paused": paused,
                "updated_at": _timestamp(changed_at),
            }
            generation = self._transition_scope_generation_tx(
                connection,
                current_scope=existing,
                next_scope=scope_evidence,
                changed_at=changed_at,
            )
            connection.execute(
                """
                INSERT INTO protection_scope (
                    scope_key, activated_at, enabled_markets_json,
                    market_epochs_json, market_instrument_hashes_json,
                    market_contract_hashes_json, watchdog_generation,
                    paused, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    activated_at = excluded.activated_at,
                    enabled_markets_json = excluded.enabled_markets_json,
                    market_epochs_json = excluded.market_epochs_json,
                    market_instrument_hashes_json = excluded.market_instrument_hashes_json,
                    market_contract_hashes_json = excluded.market_contract_hashes_json,
                    watchdog_generation = excluded.watchdog_generation,
                    paused = excluded.paused,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_key,
                    _timestamp(activated_at),
                    _json_payload(markets),
                    _json_payload(
                        {
                            market: _timestamp(epoch)
                            for market, epoch in market_epochs.items()
                        }
                    ),
                    _json_payload(instrument_hashes),
                    _json_payload(contract_hashes),
                    generation,
                    int(paused),
                    _timestamp(changed_at),
                ),
            )
            self._resolve_integrity_incident_tx(
                connection,
                scope_key,
                "protection_scope",
                changed_at,
            )
        return {
            **scope_evidence,
            "watchdog_generation": generation,
        }

    def get_protection_scope(
        self,
        scope: str = "global",
        *,
        not_after: datetime | None = None,
    ) -> dict[str, Any] | None:
        scope_key = _require_text(scope, "scope")
        cutoff = _normalise_time(not_after) if not_after is not None else None
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM protection_scope WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if row is None:
                self._scope_generations_tx(
                    self._connection,
                    scope_record=None,
                    scope_key=scope_key,
                    not_after=cutoff,
                )
                return None
            try:
                validated = _validated_scope_row(
                    dict(row), expected_scope=scope_key
                )
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                raise CorruptProtectionStateError(
                    "persisted protection scope is corrupt"
                ) from None
            if cutoff is not None:
                try:
                    scope_times = (
                        _parse_timestamp(validated["activated_at"]),
                        _parse_timestamp(validated["updated_at"]),
                        *(
                            _parse_timestamp(value)
                            for value in validated["market_epochs"].values()
                        ),
                    )
                    if any(value > cutoff for value in scope_times):
                        raise ValueError("scope evidence is future-dated")
                except (KeyError, TypeError, ValueError):
                    raise CorruptProtectionStateError(
                        "persisted protection scope is corrupt"
                    ) from None
            self._scope_generations_tx(
                self._connection,
                scope_record=validated,
                scope_key=scope_key,
                not_after=cutoff,
            )
            return validated

    def repair_corrupt_protection_scope(
        self,
        enabled_markets: Sequence[str],
        *,
        enabled_instruments: int,
        enabled_instruments_by_market: Mapping[str, Sequence[str]] | None = None,
        market_contract_hashes: Mapping[str, str] | None = None,
        scope: str = "global",
        paused: bool = False,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], str, ProtectionSnapshot, int | None]:
        """Atomically quarantine scope evidence, rebuild it and force BLIND."""

        scope_key = _require_text(scope, "scope")
        if (
            isinstance(enabled_instruments, bool)
            or not isinstance(enabled_instruments, int)
            or enabled_instruments < 0
        ):
            raise ValueError("enabled_instruments must be a non-negative integer")
        if not isinstance(paused, bool):
            raise TypeError("paused must be a bool")
        markets = tuple(
            sorted(
                dict.fromkeys(
                    _require_text(item, "market").strip().upper()
                    for item in enabled_markets
                )
            )
        )
        for market in markets:
            if len(market) > 16 or not all(
                character.isalnum() or character in "_.-" for character in market
            ):
                raise ValueError("market must be a low-cardinality identifier")
        instrument_hashes: dict[str, str] = {}
        if enabled_instruments_by_market is not None:
            if set(enabled_instruments_by_market) != set(markets):
                raise ValueError(
                    "instrument identity markets must match enabled markets"
                )
            instrument_hashes = {
                market: instrument_set_hash(enabled_instruments_by_market[market])
                for market in markets
            }
        contract_hashes = (
            _market_digest_map(
                market_contract_hashes,
                markets,
                label="contract identity",
            )
            if market_contract_hashes is not None
            else {}
        )
        repaired_at = _normalise_time(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM protection_scope WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no protection scope exists for scope: {scope_key}")
            raw_record = dict(row)
            raw_generation_rows = [
                dict(item)
                for item in connection.execute(
                    """
                    SELECT rowid AS _physical_rowid, *
                    FROM protection_scope_generations
                    ORDER BY rowid
                    """,
                ).fetchall()
            ]
            persisted_scope_keys = {
                item["scope_key"]
                for item in connection.execute(
                    "SELECT scope_key FROM protection_scope"
                ).fetchall()
                if isinstance(item["scope_key"], str)
                and item["scope_key"] in _SUPPORTED_PROTECTION_SCOPES
            }
            raw_scope_rows = [
                dict(item)
                for item in connection.execute(
                    "SELECT rowid AS _physical_rowid, * FROM protection_scope"
                ).fetchall()
            ]
            validated_scopes: dict[str, dict[str, Any]] = {}
            invalid_scope_keys: set[str] = set()
            unsupported_scope_rowids: list[int] = []
            for raw_scope in raw_scope_rows:
                raw_scope_key = raw_scope.get("scope_key")
                if (
                    not isinstance(raw_scope_key, str)
                    or raw_scope_key not in _SUPPORTED_PROTECTION_SCOPES
                ):
                    unsupported_scope_rowids.append(
                        int(raw_scope["_physical_rowid"])
                    )
                    continue
                try:
                    validated_scopes[raw_scope_key] = _validated_scope_row(
                        raw_scope,
                        expected_scope=raw_scope_key,
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    invalid_scope_keys.add(raw_scope_key)
            if invalid_scope_keys - {scope_key}:
                raise CorruptProtectionStateError(
                    "foreign protection scope requires explicit repair"
                )
            orphan_generation_rowids = [
                item["_physical_rowid"]
                for item in raw_generation_rows
                if not isinstance(item.get("scope_key"), str)
                or item["scope_key"] not in persisted_scope_keys
            ]
            invalid_generation_scopes: set[str] = set()
            for persisted_scope, persisted_record in validated_scopes.items():
                raw_group = [
                    item
                    for item in raw_generation_rows
                    if item.get("scope_key") == persisted_scope
                ]
                try:
                    parsed_group = [
                        _validated_scope_generation_row(
                            item,
                            not_after=repaired_at,
                        )
                        for item in raw_group
                    ]
                    parsed_group.sort(key=lambda item: item["id"])
                    self._validate_scope_generation_chain(
                        persisted_record,
                        parsed_group,
                    )
                except CorruptProtectionStateError:
                    invalid_generation_scopes.add(persisted_scope)
            target_scope_valid = scope_key in validated_scopes
            target_generation_valid = (
                target_scope_valid
                and scope_key not in invalid_generation_scopes
            )
            raw_watchdog_rows: list[dict[str, Any]] = []
            raw_watchdog_claim_rows: list[dict[str, Any]] = []
            if scope_key == "global":
                # Once the scope-generation proof is corrupt, no durable
                # watchdog edge can be authenticated against it.  Capture the
                # physical SQLite values before removing that untrusted
                # outbox, including a live lease that would otherwise keep the
                # repaired scope permanently RED.
                raw_watchdog_rows = [
                    dict(item)
                    for item in connection.execute(
                        "SELECT * FROM watchdog_incidents ORDER BY id"
                    ).fetchall()
                ]
                raw_watchdog_claim_rows = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT * FROM notification_claims
                        WHERE claim_key LIKE 'watchdog:%'
                           OR business_key LIKE 'watchdog:%'
                        ORDER BY claim_key
                        """
                    ).fetchall()
                ]
            if (
                target_generation_valid
                and not orphan_generation_rowids
                and not invalid_generation_scopes
            ):
                raise ValueError("protection scope is valid and must not be repaired")
            raw_payload = _json_payload(
                {
                    "scope": json.loads(
                        _sqlite_quarantine_envelope(raw_record)
                    ),
                    "scope_rows": [
                        json.loads(_sqlite_quarantine_envelope(item))
                        for item in raw_scope_rows
                    ],
                    "generations": [
                        json.loads(_sqlite_quarantine_envelope(item))
                        for item in raw_generation_rows
                    ],
                    "watchdog_incidents": [
                        json.loads(_sqlite_quarantine_envelope(item))
                        for item in raw_watchdog_rows
                    ],
                    "watchdog_claims": [
                        json.loads(_sqlite_quarantine_envelope(item))
                        for item in raw_watchdog_claim_rows
                    ],
                }
            )
            digest = hashlib.sha256(raw_payload.encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO protection_scope_quarantine (
                    scope_key, payload_sha256, raw_payload, quarantined_at
                ) VALUES (?, ?, ?, ?)
                """,
                (scope_key, digest, raw_payload, _timestamp(repaired_at)),
            )
            if target_generation_valid:
                repaired_scope_evidence = validated_scopes[scope_key]
                repaired_generation = repaired_scope_evidence[
                    "watchdog_generation"
                ]
                assert repaired_generation is not None
            else:
                epochs = {market: repaired_at for market in markets}
                activated_at = min(epochs.values()) if epochs else repaired_at
                repaired_scope_evidence = {
                    "scope": scope_key,
                    "activated_at": _timestamp(activated_at),
                    "enabled_markets": list(markets),
                    "market_epochs": {
                        market: _timestamp(epoch)
                        for market, epoch in epochs.items()
                    },
                    "market_instrument_hashes": dict(instrument_hashes),
                    "market_contract_hashes": dict(contract_hashes),
                    "paused": paused,
                    "updated_at": _timestamp(repaired_at),
                }
                repaired_generation = watchdog_scope_generation(
                    repaired_scope_evidence
                )
            if scope_key == "global" and (
                not target_generation_valid or orphan_generation_rowids
            ):
                connection.execute(
                    """
                    DELETE FROM notification_claims
                    WHERE claim_key LIKE 'watchdog:%'
                       OR business_key LIKE 'watchdog:%'
                    """
                )
                connection.execute("DELETE FROM watchdog_incidents")
            for physical_rowid in orphan_generation_rowids:
                connection.execute(
                    """
                    DELETE FROM protection_scope_generations
                    WHERE rowid = ?
                    """,
                    (physical_rowid,),
                )
            for physical_rowid in unsupported_scope_rowids:
                connection.execute(
                    "DELETE FROM protection_scope WHERE rowid = ?",
                    (physical_rowid,),
                )
            for invalid_scope in sorted(
                invalid_generation_scopes - {scope_key}
            ):
                connection.execute(
                    "DELETE FROM protection_scope_generations WHERE scope_key = ?",
                    (invalid_scope,),
                )
                foreign_generation = validated_scopes[invalid_scope].get(
                    "watchdog_generation"
                )
                if foreign_generation is not None:
                    connection.execute(
                        """
                        INSERT INTO protection_scope_generations (
                            scope_key, generation, activated_at, superseded_at
                        ) VALUES (?, ?, ?, NULL)
                        """,
                        (
                            invalid_scope,
                            foreign_generation,
                            _timestamp(repaired_at),
                        ),
                    )
            if not target_generation_valid:
                connection.execute(
                    "DELETE FROM protection_scope_generations WHERE scope_key = ?",
                    (scope_key,),
                )
                connection.execute(
                    """
                    INSERT INTO protection_scope_generations (
                        scope_key, generation, activated_at, superseded_at
                    ) VALUES (?, ?, ?, NULL)
                    """,
                    (scope_key, repaired_generation, _timestamp(repaired_at)),
                )
                connection.execute(
                    """
                    UPDATE protection_scope
                    SET activated_at = ?, enabled_markets_json = ?,
                        market_epochs_json = ?, market_instrument_hashes_json = ?,
                        market_contract_hashes_json = ?, watchdog_generation = ?,
                        paused = ?, updated_at = ?
                    WHERE scope_key = ?
                    """,
                    (
                        repaired_scope_evidence["activated_at"],
                        _json_payload(markets),
                        _json_payload(repaired_scope_evidence["market_epochs"]),
                        _json_payload(instrument_hashes),
                        _json_payload(contract_hashes),
                        repaired_generation,
                        int(paused),
                        _timestamp(repaired_at),
                        scope_key,
                    ),
                )
            current: ProtectionSnapshot | None = None
            state_was_corrupt = False
            state_row = connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if state_row is not None:
                raw_state = str(state_row["snapshot_json"])
                try:
                    current = ProtectionSnapshot.model_validate_json(raw_state)
                    if current.scope != scope_key:
                        raise ValueError("snapshot scope does not match row key")
                except (TypeError, ValueError):
                    state_was_corrupt = True
                    state_digest = hashlib.sha256(raw_state.encode()).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO protection_state_quarantine (
                            scope_key, payload_sha256, raw_payload, quarantined_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            scope_key,
                            state_digest,
                            raw_state,
                            _timestamp(repaired_at),
                        ),
                    )
                    current = None
            transition = transition_protection(
                current,
                BlindnessObservation(
                    scope=scope_key,
                    observation_id=f"scope-repair:{digest}",
                    observed_at=repaired_at,
                    enabled_instruments=enabled_instruments,
                    usable_instruments=0,
                    reason_codes=("state_repaired",),
                ),
            )
            snapshot = transition.snapshot
            snapshot_payload = _json_payload(snapshot.model_dump(mode="json"))
            connection.execute(
                """
                INSERT INTO protection_state (scope_key, snapshot_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    snapshot_json = excluded.snapshot_json,
                    updated_at = excluded.updated_at
                """,
                (scope_key, snapshot_payload, _timestamp(repaired_at)),
            )
            event_id: int | None = None
            if transition.edge:
                cursor = connection.execute(
                    """
                    INSERT INTO protection_events (
                        scope_key, event_type, previous_state, current_state,
                        incident_id, occurred_at, payload_json, notified_at,
                        delivery_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'suppressed')
                    """,
                    (
                        scope_key,
                        transition.event_type,
                        (
                            transition.previous_state.value
                            if transition.previous_state is not None
                            else None
                        ),
                        snapshot.state.value,
                        snapshot.incident_id,
                        _timestamp(repaired_at),
                        snapshot_payload,
                    ),
                )
                if cursor.lastrowid is None:  # pragma: no cover - SQLite contract
                    raise RuntimeError("SQLite did not return a protection event id")
                event_id = int(cursor.lastrowid)
            self._resolve_integrity_incident_tx(
                connection,
                scope_key,
                "protection_scope",
                repaired_at,
            )
            if state_was_corrupt:
                self._resolve_integrity_incident_tx(
                    connection,
                    scope_key,
                    "protection_state",
                    repaired_at,
                )
        repaired = self.get_protection_scope(scope_key)
        assert repaired is not None
        return repaired, digest, snapshot, event_id

    def _watchdog_batch_failure_point(self) -> None:
        """No-op seam used to prove atomic rollback in fault-injection tests."""

    def finalize_overdue_market_windows(
        self,
        market: str,
        expected_windows: Sequence[tuple[str, datetime, datetime]],
        *,
        enabled_tickers: Sequence[str],
        now: datetime,
    ) -> tuple[tuple[str, ...], int | None]:
        """Atomically seal all overdue promises and apply one market BLIND edge."""

        if not isinstance(market, str) or market not in _MARKET_TIMEZONES:
            raise ValueError("market must be US or HK")
        if isinstance(expected_windows, (str, bytes)):
            raise TypeError("expected_windows must be a sequence")
        finalized_at = _normalise_time(now)
        tickers = tuple(sorted(enabled_tickers))
        instrument_set_hash(tickers)
        normalized_windows: list[tuple[str, datetime, datetime]] = []
        seen_keys: set[str] = set()
        for raw in expected_windows:
            if not isinstance(raw, (tuple, list)) or len(raw) != 3:
                raise TypeError("expected window must be a key/expected/deadline tuple")
            key = _require_text(raw[0], "window_key")
            if key in seen_keys:
                raise ValueError("expected windows must be unique")
            if not isinstance(raw[1], datetime) or not isinstance(raw[2], datetime):
                raise TypeError("expected window timestamps must be datetimes")
            expected = _normalise_time(raw[1])
            deadline = _normalise_time(raw[2])
            if deadline < expected:
                raise ValueError("window deadline cannot precede expected time")
            canonical_key = (
                f"{market}:"
                f"{expected.astimezone(_MARKET_TIMEZONES[market]).date().isoformat()}"
            )
            if key != canonical_key:
                raise ValueError("window key does not match market session date")
            seen_keys.add(key)
            normalized_windows.append((key, expected, deadline))
        normalized_windows.sort(key=lambda item: (item[2], item[0]))

        scope = f"market:{market}"
        finalized: list[str] = []
        result_event_id: int | None = None
        with self._transaction() as connection:
            # Validate the entire immutable SLO ledger before taking a view.
            persisted_rows = connection.execute(
                "SELECT * FROM protection_windows"
            ).fetchall()
            persisted = {
                item["window_key"]: item
                for item in (
                    _validated_protection_window(dict(row))
                    for row in persisted_rows
                )
            }
            candidates: list[
                tuple[str, datetime, datetime, dict[str, Any] | None]
            ] = []
            for key, expected, deadline in normalized_windows:
                if finalized_at <= deadline:
                    continue
                existing = persisted.get(key)
                if existing is not None:
                    if (
                        existing["market"] != market
                        or _parse_timestamp(existing["expected_at"]) != expected
                        or _parse_timestamp(existing["deadline_at"]) != deadline
                    ):
                        raise CorruptProtectionStateError(
                            "persisted protection window does not match promise"
                        )
                    if existing["status"] in {"good", "bad"}:
                        continue
                candidates.append((key, expected, deadline, existing))
            if not candidates:
                return (), None

            state_row = connection.execute(
                "SELECT snapshot_json FROM protection_state WHERE scope_key = ?",
                (scope,),
            ).fetchone()
            current: ProtectionSnapshot | None = None
            if state_row is not None:
                try:
                    current = ProtectionSnapshot.model_validate_json(
                        state_row["snapshot_json"]
                    )
                    if current.scope != scope:
                        raise ValueError("snapshot scope does not match row key")
                    if current.updated_at > finalized_at:
                        raise ValueError("protection state is future-dated")
                except (TypeError, ValueError):
                    raise CorruptProtectionStateError(
                        "persisted protection state is corrupt"
                    ) from None

            latest_key = candidates[-1][0]
            observation = BlindnessObservation(
                scope=scope,
                observation_id=f"deadline-batch:{scope}:{latest_key}",
                observed_at=finalized_at,
                enabled_instruments=len(tickers),
                usable_instruments=0,
                unusable_tickers=tickers,
                reason_codes=("expected_window_missing",),
                deadline_missed=True,
                full_coverage_scan=False,
            )
            observation_id = protection_observation_identity(observation)
            observation_payload = _json_payload(
                observation.model_dump(mode="json")
            )
            observation_digest = hashlib.sha256(
                observation_payload.encode()
            ).hexdigest()
            ledger_row = connection.execute(
                """
                SELECT payload_sha256
                FROM protection_observations
                WHERE scope_key = ? AND observation_id = ?
                """,
                (scope, observation_id),
            ).fetchone()
            if ledger_row is not None:
                raise CorruptProtectionStateError(
                    "deadline observation exists without terminal windows"
                )
            connection.execute(
                """
                INSERT INTO protection_observations (
                    scope_key, observation_id, payload_sha256, observed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    scope,
                    observation_id,
                    observation_digest,
                    _timestamp(finalized_at),
                ),
            )
            transition = transition_protection(current, observation)
            snapshot = transition.snapshot
            payload = snapshot.model_dump(mode="json")
            connection.execute(
                """
                INSERT INTO protection_state (scope_key, snapshot_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    snapshot_json = excluded.snapshot_json,
                    updated_at = excluded.updated_at
                """,
                (scope, _json_payload(payload), _timestamp(snapshot.updated_at)),
            )
            if transition.edge and transition.event_type is not None:
                self._suppress_pending_incident_events_tx(connection, scope)
                # Market scopes are evidence-only. Operational delivery is
                # coalesced through the global scope so a multi-market miss
                # can never fan out into duplicate alerts or be revived by an
                # activation-sync pass.
                effective_status = "suppressed"
                cursor = connection.execute(
                    """
                    INSERT INTO protection_events (
                        scope_key, event_type, previous_state, current_state,
                        incident_id, occurred_at, payload_json, notified_at,
                        delivery_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        scope,
                        transition.event_type,
                        (
                            transition.previous_state.value
                            if transition.previous_state is not None
                            else None
                        ),
                        snapshot.state.value,
                        snapshot.incident_id,
                        _timestamp(snapshot.updated_at),
                        _json_payload(payload),
                        effective_status,
                    ),
                )
                if cursor.lastrowid is None:  # pragma: no cover - SQLite contract
                    raise RuntimeError("SQLite did not return a protection event id")
                result_event_id = int(cursor.lastrowid)

            # Fault injection here proves state/event/observation and all
            # windows share the same SQLite transaction.
            self._watchdog_batch_failure_point()
            for key, expected, deadline, existing in candidates:
                if existing is None:
                    actual_at = None
                    enabled = len(tickers)
                    usable = 0
                    affected = tickers
                    reasons = ("expected_window_missing",)
                else:
                    actual_at = existing["actual_at"]
                    enabled = int(existing["enabled_instruments"])
                    usable = int(existing["usable_instruments"])
                    affected = tuple(existing["affected"])
                    reasons = tuple(
                        dict.fromkeys(
                            (*existing["reasons"], "expected_window_missing")
                        )
                    )
                ratio = usable / enabled if enabled else None
                connection.execute(
                    """
                    INSERT INTO protection_windows (
                        window_key, market, expected_at, deadline_at, status,
                        actual_at, last_success_at, enabled_instruments,
                        usable_instruments, coverage_ratio, affected_json,
                        reasons_json, updated_at
                    ) VALUES (?, ?, ?, ?, 'bad', ?, NULL, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(window_key) DO UPDATE SET
                        market = excluded.market,
                        expected_at = excluded.expected_at,
                        deadline_at = excluded.deadline_at,
                        status = 'bad',
                        actual_at = excluded.actual_at,
                        last_success_at = NULL,
                        enabled_instruments = excluded.enabled_instruments,
                        usable_instruments = excluded.usable_instruments,
                        coverage_ratio = excluded.coverage_ratio,
                        affected_json = excluded.affected_json,
                        reasons_json = excluded.reasons_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        market,
                        _timestamp(expected),
                        _timestamp(deadline),
                        actual_at,
                        enabled,
                        usable,
                        ratio,
                        _json_payload(affected),
                        _json_payload(reasons),
                        _timestamp(finalized_at),
                    ),
                )
                finalized.append(key)
            for key in finalized:
                row = connection.execute(
                    "SELECT * FROM protection_windows WHERE window_key = ?",
                    (key,),
                ).fetchone()
                assert row is not None
                _validated_protection_window(dict(row))
            self._resolve_integrity_incident_tx(
                connection,
                scope,
                "protection_state",
                finalized_at,
            )
        return tuple(finalized), result_event_id

    def record_protection_window(
        self,
        window_key: str,
        market: str,
        expected_at: datetime,
        deadline_at: datetime,
        status: str,
        *,
        actual_at: datetime | None = None,
        last_success_at: datetime | None = None,
        enabled_instruments: int = 0,
        usable_instruments: int = 0,
        affected_tickers: Sequence[str] = (),
        reason_codes: Sequence[str] = (),
        now: datetime | None = None,
    ) -> None:
        key = _require_text(window_key, "window_key")
        market_name = _require_text(market, "market")
        if status not in {"pending", "good", "bad"}:
            raise ValueError("window status must be pending, good or bad")
        if enabled_instruments < 0 or not 0 <= usable_instruments <= enabled_instruments:
            raise ValueError("window coverage counts are inconsistent")
        expected = _normalise_time(expected_at)
        deadline = _normalise_time(deadline_at)
        if deadline < expected:
            raise ValueError("window deadline cannot precede expected time")
        recorded_at = _normalise_time(now)
        actual = _normalise_time(actual_at) if actual_at is not None else None
        success = (
            _normalise_time(last_success_at)
            if last_success_at is not None
            else None
        )
        ratio = usable_instruments / enabled_instruments if enabled_instruments else None
        if status == "good":
            on_time_full_coverage = (
                enabled_instruments > 0
                and usable_instruments == enabled_instruments
                and actual is not None
                and expected <= actual
                and actual <= deadline
            )
            if not on_time_full_coverage:
                status = "bad"
                success = None
            elif success is None:
                success = actual
        reasons = tuple(
            _low_cardinality_code(item, "reason_code") for item in reason_codes
        )
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO protection_windows (
                    window_key, market, expected_at, deadline_at, status,
                    actual_at, last_success_at, enabled_instruments,
                    usable_instruments, coverage_ratio, affected_json,
                    reasons_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(window_key) DO UPDATE SET
                    market = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.market
                        ELSE excluded.market
                    END,
                    expected_at = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.expected_at
                        ELSE excluded.expected_at
                    END,
                    deadline_at = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.deadline_at
                        ELSE excluded.deadline_at
                    END,
                    status = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.status
                        ELSE excluded.status
                    END,
                    actual_at = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.actual_at
                        ELSE COALESCE(excluded.actual_at, protection_windows.actual_at)
                    END,
                    last_success_at = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.last_success_at
                        ELSE COALESCE(
                            excluded.last_success_at,
                            protection_windows.last_success_at
                        )
                    END,
                    enabled_instruments = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.enabled_instruments
                        ELSE excluded.enabled_instruments
                    END,
                    usable_instruments = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.usable_instruments
                        ELSE excluded.usable_instruments
                    END,
                    coverage_ratio = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.coverage_ratio
                        ELSE excluded.coverage_ratio
                    END,
                    affected_json = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.affected_json
                        ELSE excluded.affected_json
                    END,
                    reasons_json = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.reasons_json
                        ELSE excluded.reasons_json
                    END,
                    updated_at = CASE
                        WHEN protection_windows.status IN ('good', 'bad')
                            THEN protection_windows.updated_at
                        ELSE excluded.updated_at
                    END
                """,
                (
                    key,
                    market_name,
                    _timestamp(expected),
                    _timestamp(deadline),
                    status,
                    _timestamp(actual) if actual is not None else None,
                    _timestamp(success) if success is not None else None,
                    enabled_instruments,
                    usable_instruments,
                    ratio,
                    _json_payload(tuple(dict.fromkeys(affected_tickers))),
                    _json_payload(reasons),
                    _timestamp(recorded_at),
                ),
            )

    def protection_windows(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        beginning = _normalise_time(since) if since is not None else None
        finish = _normalise_time(until) if until is not None else None
        if beginning is not None and finish is not None and finish < beginning:
            raise ValueError("until cannot be earlier than since")
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM protection_windows"
            ).fetchall()
        # Validate the complete safety ledger before applying a caller's view.
        # Otherwise a malformed timestamp could hide outside a lexical SQL
        # range and silently poison the SLO denominator.
        result = [_validated_protection_window(dict(row)) for row in rows]
        if beginning is not None:
            result = [
                item
                for item in result
                if _parse_timestamp(item["deadline_at"]) >= beginning
            ]
        if finish is not None:
            result = [
                item
                for item in result
                if _parse_timestamp(item["deadline_at"]) <= finish
            ]
        result.sort(key=lambda item: item["window_key"])
        result.sort(
            key=lambda item: _parse_timestamp(item["deadline_at"]),
            reverse=True,
        )
        return result

    def record_delivery_state(
        self,
        channel: str,
        *,
        config_fingerprint: str,
        configured: bool,
        mode: str,
        attempted_at: datetime | None = None,
        success: bool | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> None:
        channel_name = _low_cardinality_code(channel, "channel")
        assert channel_name is not None
        mode_name = _low_cardinality_code(mode, "mode")
        assert mode_name is not None
        if channel_name not in {"telegram", "whatsapp", "heartbeat"}:
            raise ValueError("channel must be telegram, whatsapp, or heartbeat")
        if mode_name not in {"active", "preview"}:
            raise ValueError("mode must be active or preview")
        error = _low_cardinality_code(error_code, "error_code")
        try:
            fingerprint = _optional_sha256(config_fingerprint)
        except ValueError:
            fingerprint = None
        if fingerprint is None or config_fingerprint != fingerprint:
            raise ValueError(
                "config_fingerprint must be a lowercase SHA-256 digest"
            )
        updated = _normalise_time(now)
        attempt = _normalise_time(attempted_at) if attempted_at is not None else None
        if success is not None and attempt is None:
            raise ValueError("success requires attempted_at")
        if error is not None and attempt is None:
            raise ValueError("error_code requires attempted_at")
        if success is True and error is not None:
            raise ValueError("successful delivery cannot have error_code")
        with self._transaction() as connection:
            previous = connection.execute(
                """
                SELECT generation, config_fingerprint, last_attempt_at,
                       last_success_at, error_code
                FROM delivery_state WHERE channel = ?
                """,
                (channel_name,),
            ).fetchone()
            same_generation = bool(
                previous is not None
                and previous["config_fingerprint"] == fingerprint
            )
            delivery_generation = (
                int(previous["generation"])
                if same_generation and previous is not None
                else (
                    int(previous["generation"]) + 1
                    if previous is not None
                    else 1
                )
            )
            previous_attempt = (
                previous["last_attempt_at"] if same_generation else None
            )
            previous_success = (
                previous["last_success_at"] if same_generation else None
            )
            previous_error = previous["error_code"] if same_generation else None
            last_attempt = (
                _timestamp(attempt) if attempt is not None else previous_attempt
            )
            last_success = (
                _timestamp(attempt)
                if success is True and attempt is not None
                else previous_success
            )
            persisted_error = (
                previous_error
                if attempt is None
                else (None if success is True else error)
            )
            connection.execute(
                """
                INSERT INTO delivery_state (
                    channel, generation, configured, mode, config_fingerprint, last_attempt_at,
                    last_success_at, error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel) DO UPDATE SET
                    generation = excluded.generation,
                    configured = excluded.configured,
                    mode = excluded.mode,
                    config_fingerprint = excluded.config_fingerprint,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = excluded.last_success_at,
                    error_code = excluded.error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    channel_name,
                    delivery_generation,
                    int(configured),
                    mode_name,
                    fingerprint,
                    last_attempt,
                    last_success,
                    persisted_error,
                    _timestamp(updated),
                ),
            )

    def delivery_states(
        self,
        *,
        not_after: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return validated low-cardinality delivery evidence."""

        cutoff = _normalise_time(not_after) if not_after is not None else None
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM delivery_state ORDER BY channel"
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            raw = dict(row)
            try:
                channel = raw["channel"]
                validated = _validated_delivery_state(raw)
                updated_at = _parse_persisted_aware_timestamp(raw["updated_at"])
                if cutoff is not None and updated_at > cutoff:
                    raise ValueError("delivery evidence is future-dated")
            except (KeyError, TypeError, ValueError):
                raise CorruptProtectionStateError(
                    "persisted delivery evidence is corrupt"
                ) from None
            assert isinstance(channel, str)
            result[channel] = validated
        return result

    def claim_outbound_delivery(
        self,
        business_key: str,
        channel: str,
        config_fingerprint: str,
        *,
        lease_seconds: float = 300,
        now: datetime | None = None,
    ) -> str | None:
        """Lease one channel of a durable multi-channel business delivery.

        A successful sibling channel never needs to be repeated after another
        channel fails.  Changing the destination/configuration fingerprint
        starts a new proof generation for this business edge.
        """

        key = _outbound_business_key(business_key)
        channel_name = _low_cardinality_code(channel, "channel")
        if channel_name not in {"telegram", "whatsapp"}:
            raise ValueError("channel must be telegram or whatsapp")
        fingerprint = _optional_sha256(config_fingerprint)
        if fingerprint is None or fingerprint != config_fingerprint:
            raise ValueError(
                "config_fingerprint must be a lowercase SHA-256 digest"
            )
        claimed_at = _normalise_time(now)
        lease = _lease_duration(lease_seconds)
        claim_key = _outbound_claim_key(key, channel_name)
        with self._transaction() as connection:
            raw = connection.execute(
                """
                SELECT * FROM outbound_deliveries
                WHERE business_key = ? AND channel = ?
                """,
                (key, channel_name),
            ).fetchone()
            if raw is None:
                connection.execute(
                    """
                    INSERT INTO outbound_deliveries (
                        business_key, channel, config_fingerprint, status,
                        last_attempt_at, sent_at, error_code, updated_at
                    ) VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, ?)
                    """,
                    (key, channel_name, fingerprint, _timestamp(claimed_at)),
                )
                current_status = "pending"
            else:
                current = _validated_outbound_delivery(
                    dict(raw), not_after=claimed_at
                )
                same_generation = (
                    current["config_fingerprint"] == fingerprint
                )
                if not same_generation:
                    self._delete_notification_claim(connection, claim_key)
                    connection.execute(
                        """
                        UPDATE outbound_deliveries
                        SET config_fingerprint = ?, status = 'pending',
                            last_attempt_at = NULL, sent_at = NULL,
                            error_code = NULL, updated_at = ?
                        WHERE business_key = ? AND channel = ?
                        """,
                        (
                            fingerprint,
                            _timestamp(claimed_at),
                            key,
                            channel_name,
                        ),
                    )
                    current_status = "pending"
                else:
                    current_status = str(current["status"])
            if current_status == "sent":
                self._delete_notification_claim(connection, claim_key)
                return None
            return self._acquire_notification_claim(
                connection,
                claim_key,
                key,
                lease,
                claimed_at,
            )

    def mark_outbound_delivery_sent(
        self,
        business_key: str,
        channel: str,
        config_fingerprint: str,
        claim_token: str,
        *,
        now: datetime | None = None,
    ) -> None:
        key = _outbound_business_key(business_key)
        channel_name = _low_cardinality_code(channel, "channel")
        if channel_name not in {"telegram", "whatsapp"}:
            raise ValueError("channel must be telegram or whatsapp")
        fingerprint = _optional_sha256(config_fingerprint)
        if fingerprint is None or fingerprint != config_fingerprint:
            raise ValueError(
                "config_fingerprint must be a lowercase SHA-256 digest"
            )
        token = _require_text(claim_token, "claim_token")
        completed_at = _normalise_time(now)
        claim_key = _outbound_claim_key(key, channel_name)
        with self._transaction() as connection:
            self._require_notification_claim(
                connection, claim_key, token, completed_at
            )
            cursor = connection.execute(
                """
                UPDATE outbound_deliveries
                SET status = 'sent', last_attempt_at = ?, sent_at = ?,
                    error_code = NULL, updated_at = ?
                WHERE business_key = ? AND channel = ?
                  AND config_fingerprint = ? AND status = 'pending'
                """,
                (
                    _timestamp(completed_at),
                    _timestamp(completed_at),
                    _timestamp(completed_at),
                    key,
                    channel_name,
                    fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                raise CorruptProtectionStateError(
                    "outbound delivery changed before completion"
                )
            self._delete_notification_claim(connection, claim_key)

    def mark_outbound_delivery_failed(
        self,
        business_key: str,
        channel: str,
        config_fingerprint: str,
        claim_token: str,
        error_code: str,
        *,
        now: datetime | None = None,
    ) -> None:
        key = _outbound_business_key(business_key)
        channel_name = _low_cardinality_code(channel, "channel")
        if channel_name not in {"telegram", "whatsapp"}:
            raise ValueError("channel must be telegram or whatsapp")
        fingerprint = _optional_sha256(config_fingerprint)
        if fingerprint is None or fingerprint != config_fingerprint:
            raise ValueError(
                "config_fingerprint must be a lowercase SHA-256 digest"
            )
        token = _require_text(claim_token, "claim_token")
        error = _low_cardinality_code(error_code, "error_code")
        if error is None:
            raise ValueError("error_code is required")
        completed_at = _normalise_time(now)
        claim_key = _outbound_claim_key(key, channel_name)
        with self._transaction() as connection:
            self._require_notification_claim(
                connection, claim_key, token, completed_at
            )
            cursor = connection.execute(
                """
                UPDATE outbound_deliveries
                SET status = 'pending', last_attempt_at = ?, sent_at = NULL,
                    error_code = ?, updated_at = ?
                WHERE business_key = ? AND channel = ?
                  AND config_fingerprint = ? AND status = 'pending'
                """,
                (
                    _timestamp(completed_at),
                    error,
                    _timestamp(completed_at),
                    key,
                    channel_name,
                    fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                raise CorruptProtectionStateError(
                    "outbound delivery changed before failure recording"
                )
            self._delete_notification_claim(connection, claim_key)

    def outbound_deliveries(
        self,
        *,
        business_key: str | None = None,
        not_after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return the fully validated channel ledger, then apply its view."""

        selected_key = (
            _outbound_business_key(business_key)
            if business_key is not None
            else None
        )
        cutoff = _normalise_time(not_after) if not_after is not None else None
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM outbound_deliveries"
            ).fetchall()
        result = [
            _validated_outbound_delivery(dict(row), not_after=cutoff)
            for row in rows
        ]
        if selected_key is not None:
            result = [
                item for item in result if item["business_key"] == selected_key
            ]
        result.sort(key=lambda item: (item["business_key"], item["channel"]))
        return result

    def save_provider_runtime_state(
        self,
        payload: Mapping[str, Any],
        *,
        scope: str = "global",
        now: datetime | None = None,
    ) -> None:
        scope_key = _require_text(scope, "scope")
        encoded = _json_payload(payload)
        updated = _normalise_time(now)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_runtime_state (scope_key, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (scope_key, encoded, _timestamp(updated)),
            )

    def load_provider_runtime_state(
        self,
        scope: str = "global",
        *,
        strict: bool = False,
        not_after: datetime | None = None,
    ) -> dict[str, Any] | None:
        scope_key = _require_text(scope, "scope")
        cutoff = _normalise_time(not_after) if not_after is not None else None
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload_json, updated_at
                FROM provider_runtime_state
                WHERE scope_key = ?
                """,
                (scope_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            updated_at = _parse_persisted_aware_timestamp(row["updated_at"])
            if cutoff is not None and updated_at > cutoff:
                raise ValueError("provider runtime evidence is future-dated")
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError, ValueError):
            if strict:
                raise CorruptProtectionStateError(
                    "persisted provider runtime evidence is corrupt"
                ) from None
            return None
        if not isinstance(payload, dict):
            if strict:
                raise CorruptProtectionStateError(
                    "persisted provider runtime evidence is corrupt"
                )
            return None
        if strict:
            return _validated_provider_runtime_payload(
                payload,
                not_after=cutoff,
            )
        return payload

    def repair_corrupt_provider_runtime_state(
        self,
        affected_markets: Sequence[str],
        *,
        scope: str = "global",
        now: datetime | None = None,
    ) -> str:
        """Quarantine corrupt provider state and invalidate market baselines."""

        scope_key = _require_text(scope, "scope")
        if scope_key != "global":
            raise ValueError("provider runtime repair scope must be global")
        if isinstance(affected_markets, (str, bytes)):
            raise ValueError("affected_markets must be a sequence")
        markets = tuple(
            sorted(
                dict.fromkeys(
                    _require_text(item, "market").strip().upper()
                    for item in affected_markets
                )
            )
        )
        if any(market not in {"US", "HK"} for market in markets):
            raise ValueError("affected_markets must contain only US or HK")
        repaired_at = _normalise_time(now)
        with self._transaction() as connection:
            provider_row = connection.execute(
                "SELECT * FROM provider_runtime_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if provider_row is None:
                raise KeyError(
                    f"no provider runtime state exists for scope: {scope_key}"
                )
            raw_record = dict(provider_row)
            try:
                updated_at = _parse_persisted_aware_timestamp(
                    raw_record["updated_at"]
                )
                if updated_at > repaired_at:
                    raise ValueError("provider runtime state is future-dated")
                payload = json.loads(raw_record["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("provider runtime payload must be an object")
                _validated_provider_runtime_payload(
                    payload,
                    not_after=repaired_at,
                )
            except (
                CorruptProtectionStateError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                pass
            else:
                raise ValueError(
                    "provider runtime state is valid and must not be repaired"
                )

            scope_row = connection.execute(
                "SELECT * FROM protection_scope WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            protection_scope: dict[str, Any] | None = None
            enabled_markets: set[str] = set()
            if scope_row is not None:
                try:
                    protection_scope = _validated_scope_row(
                        dict(scope_row),
                        expected_scope=scope_key,
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    raise CorruptProtectionStateError(
                        "persisted protection scope is corrupt"
                    ) from None
                enabled_markets = set(protection_scope["enabled_markets"])
            # Provider runtime is one global generation.  A corrupt payload can
            # have influenced every currently protected market, so callers may
            # not repair only a convenient subset and revive stale GREEN state.
            if set(markets) != enabled_markets:
                raise ValueError(
                    "affected_markets must exactly match enabled markets"
                )
            if protection_scope is not None and enabled_markets:
                previous_updated = _parse_persisted_aware_timestamp(
                    protection_scope["updated_at"]
                )
                if repaired_at < previous_updated:
                    raise ValueError("provider runtime repair is out of order")

            raw_payload = _sqlite_quarantine_envelope(raw_record)
            digest = hashlib.sha256(raw_payload.encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO provider_runtime_quarantine (
                    scope_key, payload_sha256, raw_payload, quarantined_at
                ) VALUES (?, ?, ?, ?)
                """,
                (scope_key, digest, raw_payload, _timestamp(repaired_at)),
            )
            connection.execute(
                "DELETE FROM provider_runtime_state WHERE scope_key = ?",
                (scope_key,),
            )
            if protection_scope is not None and enabled_markets:
                market_epochs = {
                    market: repaired_at
                    for market in protection_scope["market_epochs"]
                }
                next_scope = {
                    **protection_scope,
                    "activated_at": _timestamp(repaired_at),
                    "market_epochs": {
                        market: _timestamp(epoch)
                        for market, epoch in market_epochs.items()
                    },
                    "updated_at": _timestamp(repaired_at),
                }
                generation = self._transition_scope_generation_tx(
                    connection,
                    current_scope=protection_scope,
                    next_scope=next_scope,
                    changed_at=repaired_at,
                )
                connection.execute(
                    """
                    UPDATE protection_scope
                    SET activated_at = ?, market_epochs_json = ?,
                        watchdog_generation = ?, updated_at = ?
                    WHERE scope_key = ?
                    """,
                    (
                        _timestamp(repaired_at),
                        _json_payload(
                            {
                                market: _timestamp(epoch)
                                for market, epoch in market_epochs.items()
                            }
                        ),
                        generation,
                        _timestamp(repaired_at),
                        scope_key,
                    ),
                )
            self._resolve_integrity_incident_tx(
                connection,
                scope_key,
                "provider_runtime",
                repaired_at,
            )
            return digest

    def close(self) -> None:
        """Close the underlying connection; repeated calls are harmless."""

        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> StateStore:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()
