"""Market-session-aware freshness evaluation and decision gating.

Freshness has two clocks: provider event time (``source_as_of``) and local
observation time (``observed_at``).  The latter is never promoted to the former.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .models import (
    CacheState,
    CoverageSummary,
    FieldFreshnessPolicy,
    FieldReliability,
    FreshnessReference,
    FreshnessContext,
    FreshnessStatus,
    OverallStatus,
    ProviderAttempt,
    ReliabilityReport,
    TimestampConfidence,
    TimeBasis,
)


RULE_FIELD_MAP: dict[str, str] = {
    "price_above": "price",
    "price_below": "price",
    "price_drop_pct": "price",
    "pe_above": "pe_ttm",
    "pe_below": "pe_ttm",
    "roe_above": "roe",
}

_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9_.+-]{1,80}$")


def field_for_rule_type(rule_type: str) -> str:
    """Return the market-data field required by a supported rule type."""

    try:
        return RULE_FIELD_MAP[rule_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported rule type: {rule_type!r}") from exc


def fields_for_rule_type(rule_type: str) -> frozenset[str]:
    """Return all field capabilities needed by one rule.

    The current engine deliberately requires a valid price for every rule, so a
    fundamental rule depends on both its metric and price.
    """

    field = field_for_rule_type(rule_type)
    return frozenset({"price", field})


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def required_fields_by_rule(instrument: Any) -> dict[str, frozenset[str]]:
    """Derive field dependencies from all enabled rules of an instrument."""

    result: dict[str, frozenset[str]] = {}
    for group in ("sell_rules", "buy_rules"):
        rules = _value(instrument, group, ()) or ()
        for index, rule in enumerate(rules):
            rule_type = str(_value(rule, "type", ""))
            rule_id = str(_value(rule, "id", "") or f"{group}.{index}")
            result[rule_id] = fields_for_rule_type(rule_type)
    return result


def required_fields_for_rules(instrument: Any) -> frozenset[str]:
    """Return the union of dependencies across an instrument's configured rules."""

    required: set[str] = set()
    for fields in required_fields_by_rule(instrument).values():
        required.update(fields)
    return frozenset(required)


def _as_aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _seconds_between(now: datetime, then: datetime) -> float:
    return (now - then).total_seconds()


def _provider_name(metadata: Mapping[str, Any]) -> str | None:
    raw = metadata.get("provider") or metadata.get("source")
    if raw is None:
        return None
    provider = str(raw).strip()
    return provider if _PROVIDER_NAME.fullmatch(provider) else None


def _time_basis(metadata: Mapping[str, Any]) -> TimeBasis:
    raw = str(metadata.get("time_basis") or "none").strip().lower()
    aliases = {
        "source": TimeBasis.SOURCE_EVENT,
        "source_event": TimeBasis.SOURCE_EVENT,
        "observed": TimeBasis.OBSERVED_ONLY,
        "observed_only": TimeBasis.OBSERVED_ONLY,
        "none": TimeBasis.NONE,
    }
    return aliases.get(raw, TimeBasis.NONE)


def _unusable_field(
    field: str,
    *,
    status: FreshnessStatus,
    policy: FieldFreshnessPolicy,
    source_as_of: datetime | None,
    observed_at: datetime | None,
    source_age: float | None,
    observation_age: float | None,
    time_basis: TimeBasis,
    provider: str | None,
    reason: str,
    session_reference: bool = False,
    expected_source_after: datetime | None = None,
    cache_state: CacheState = CacheState.NONE,
) -> FieldReliability:
    confidence: TimestampConfidence
    default_reference: FreshnessReference
    if time_basis is TimeBasis.SOURCE_EVENT:
        confidence = "provider_event"
        default_reference = "wall_clock"
    elif time_basis is TimeBasis.OBSERVED_ONLY:
        confidence = "observed_only"
        default_reference = "observation_only"
    else:
        confidence = "none"
        default_reference = "none"
    budget = None if session_reference else (
        policy.max_source_age_seconds
        if time_basis is TimeBasis.SOURCE_EVENT
        else policy.max_observation_age_seconds
    )
    reference: FreshnessReference = (
        "session_watermark" if session_reference else default_reference
    )
    return FieldReliability(
        field=field,
        status=status,
        source_as_of=source_as_of,
        observed_at=observed_at,
        source_age_seconds=source_age,
        observation_age_seconds=observation_age,
        budget_seconds=budget,
        observation_budget_seconds=policy.max_observation_age_seconds,
        time_basis=time_basis,
        cache_state=cache_state,
        freshness_reference=reference,
        expected_source_after=expected_source_after,
        timestamp_confidence=confidence,
        provider=provider,
        usable_for_signal=False,
        reason=reason,
    )


def evaluate_field_freshness(
    field: str,
    value: Any,
    metadata: Mapping[str, Any] | None,
    policy: FieldFreshnessPolicy,
    context: FreshnessContext,
    *,
    future_tolerance_seconds: float = 300.0,
) -> FieldReliability:
    """Evaluate one field without consulting wall clock or mutable global state."""

    if not math.isfinite(future_tolerance_seconds) or future_tolerance_seconds < 0:
        raise ValueError("future_tolerance_seconds must be finite and non-negative")
    meta = metadata or {}
    basis = _time_basis(meta)
    provider = _provider_name(meta)
    try:
        field_cache = CacheState(str(meta.get("cache_state") or "none"))
    except ValueError:
        field_cache = CacheState.NONE
        return _unusable_field(
            field,
            status=FreshnessStatus.UNKNOWN,
            policy=policy,
            source_as_of=None,
            observed_at=None,
            source_age=None,
            observation_age=None,
            time_basis=basis,
            provider=provider,
            reason="cache state is invalid",
        )
    source_raw = meta.get("source_as_of")
    observed_raw = meta.get("observed_at")
    source = _as_aware_datetime(source_raw)
    observed = _as_aware_datetime(observed_raw)
    source_age = (
        _seconds_between(context.evaluated_at, source) if source is not None else None
    )
    observation_age = (
        _seconds_between(context.evaluated_at, observed)
        if observed is not None
        else None
    )

    if field_cache is CacheState.STALE_IF_ERROR:
        return _unusable_field(
            field,
            status=FreshnessStatus.STALE,
            policy=policy,
            source_as_of=source,
            observed_at=observed,
            source_age=source_age,
            observation_age=observation_age,
            time_basis=basis,
            provider=provider,
            reason="provider returned bounded stale-if-error context",
            cache_state=field_cache,
        )

    if value is None:
        return _unusable_field(
            field,
            status=FreshnessStatus.UNKNOWN,
            policy=policy,
            source_as_of=source,
            observed_at=observed,
            source_age=source_age,
            observation_age=observation_age,
            time_basis=basis,
            provider=provider,
            reason="field value is missing",
        )
    if observed_raw is not None and observed is None:
        return _unusable_field(
            field,
            status=FreshnessStatus.UNKNOWN,
            policy=policy,
            source_as_of=source,
            observed_at=None,
            source_age=source_age,
            observation_age=None,
            time_basis=basis,
            provider=provider,
            reason="observed_at is invalid or timezone-naive",
        )
    if observed is None:
        return _unusable_field(
            field,
            status=FreshnessStatus.UNKNOWN,
            policy=policy,
            source_as_of=source,
            observed_at=None,
            source_age=source_age,
            observation_age=None,
            time_basis=basis,
            provider=provider,
            reason="observed_at is missing",
        )
    if observation_age is not None and observation_age < -future_tolerance_seconds:
        return _unusable_field(
            field,
            status=FreshnessStatus.FUTURE,
            policy=policy,
            source_as_of=source,
            observed_at=observed,
            source_age=source_age,
            observation_age=observation_age,
            time_basis=basis,
            provider=provider,
            reason="observed_at exceeds clock-skew tolerance",
        )
    if source_age is not None and source_age < -future_tolerance_seconds:
        return _unusable_field(
            field,
            status=FreshnessStatus.FUTURE,
            policy=policy,
            source_as_of=source,
            observed_at=observed,
            source_age=source_age,
            observation_age=observation_age,
            time_basis=basis,
            provider=provider,
            reason="source_as_of exceeds clock-skew tolerance",
        )

    safe_observation_age = max(0.0, observation_age or 0.0)
    safe_source_age = max(0.0, source_age or 0.0)
    if safe_observation_age > policy.max_observation_age_seconds:
        return _unusable_field(
            field,
            status=FreshnessStatus.STALE,
            policy=policy,
            source_as_of=source,
            observed_at=observed,
            source_age=source_age,
            observation_age=observation_age,
            time_basis=basis,
            provider=provider,
            reason="observation age exceeds its budget",
        )

    source_ratio = 0.0
    use_session_watermark = False
    if basis is TimeBasis.SOURCE_EVENT:
        if source_raw is not None and source is None:
            return _unusable_field(
                field,
                status=FreshnessStatus.UNKNOWN,
                policy=policy,
                source_as_of=None,
                observed_at=observed,
                source_age=None,
                observation_age=observation_age,
                time_basis=basis,
                provider=provider,
                reason="source_as_of is invalid or timezone-naive",
            )
        if source is None or policy.max_source_age_seconds is None:
            return _unusable_field(
                field,
                status=FreshnessStatus.UNKNOWN,
                policy=policy,
                source_as_of=None,
                observed_at=observed,
                source_age=None,
                observation_age=observation_age,
                time_basis=basis,
                provider=provider,
                reason="source event time or source budget is missing",
            )
        use_session_watermark = (
            policy.session_aware
            and context.market_phase != "open"
            and context.expected_source_after is not None
        )
        if use_session_watermark:
            watermark = context.expected_source_after
            assert watermark is not None
            if source.timestamp() + future_tolerance_seconds < watermark.timestamp():
                return _unusable_field(
                    field,
                    status=FreshnessStatus.STALE,
                    policy=policy,
                    source_as_of=source,
                    observed_at=observed,
                    source_age=source_age,
                    observation_age=observation_age,
                    time_basis=basis,
                    provider=provider,
                    reason="source event predates the latest completed session",
                    session_reference=True,
                    expected_source_after=watermark,
                )
        else:
            if safe_source_age > policy.max_source_age_seconds:
                return _unusable_field(
                    field,
                    status=FreshnessStatus.STALE,
                    policy=policy,
                    source_as_of=source,
                    observed_at=observed,
                    source_age=source_age,
                    observation_age=observation_age,
                    time_basis=basis,
                    provider=provider,
                    reason="source event age exceeds its budget",
                )
            source_ratio = safe_source_age / policy.max_source_age_seconds
    elif basis is TimeBasis.OBSERVED_ONLY:
        if not policy.allow_observed_only:
            return _unusable_field(
                field,
                status=FreshnessStatus.UNKNOWN,
                policy=policy,
                source_as_of=source,
                observed_at=observed,
                source_age=source_age,
                observation_age=observation_age,
                time_basis=basis,
                provider=provider,
                reason="provider did not supply source event time",
            )
    else:
        return _unusable_field(
            field,
            status=FreshnessStatus.UNKNOWN,
            policy=policy,
            source_as_of=source,
            observed_at=observed,
            source_age=source_age,
            observation_age=observation_age,
            time_basis=basis,
            provider=provider,
            reason="time basis is missing or unsupported",
        )

    observation_ratio = safe_observation_age / policy.max_observation_age_seconds
    aging = max(source_ratio, observation_ratio) >= policy.aging_ratio
    status = FreshnessStatus.AGING if aging else FreshnessStatus.FRESH
    confidence: TimestampConfidence = (
        "provider_event"
        if basis is TimeBasis.SOURCE_EVENT
        else "observed_only"
    )
    budget = (
        None
        if use_session_watermark
        else (
            policy.max_source_age_seconds
            if basis is TimeBasis.SOURCE_EVENT
            else policy.max_observation_age_seconds
        )
    )
    if use_session_watermark:
        reason = "source event matches latest completed session watermark"
        if aging:
            reason += "; observation is approaching expiry"
    else:
        reason = (
            "within freshness budgets but approaching expiry"
            if aging
            else "within freshness budgets"
        )
    if basis is TimeBasis.OBSERVED_ONLY:
        reason += "; provider event time unavailable"
    reference: FreshnessReference = (
        "session_watermark"
        if use_session_watermark
        else (
            "wall_clock"
            if basis is TimeBasis.SOURCE_EVENT
            else "observation_only"
        )
    )
    return FieldReliability(
        field=field,
        status=status,
        source_as_of=source,
        observed_at=observed,
        source_age_seconds=source_age,
        observation_age_seconds=observation_age,
        budget_seconds=budget,
        observation_budget_seconds=policy.max_observation_age_seconds,
        time_basis=basis,
        cache_state=field_cache,
        freshness_reference=reference,
        expected_source_after=(
            context.expected_source_after if use_session_watermark else None
        ),
        timestamp_confidence=confidence,
        provider=provider,
        usable_for_signal=True,
        reason=reason,
    )


def _coerce_policy(value: Any) -> FieldFreshnessPolicy:
    if isinstance(value, FieldFreshnessPolicy):
        return value
    if isinstance(value, Mapping):
        return FieldFreshnessPolicy.model_validate(value)
    if hasattr(value, "model_dump"):
        return FieldFreshnessPolicy.model_validate(value.model_dump(mode="python"))
    raise TypeError("freshness policy must be a mapping or FieldFreshnessPolicy")


def evaluate_snapshot_reliability(
    snapshot: Mapping[str, Any],
    required_fields: Iterable[str],
    policies: Mapping[str, Any],
    context: FreshnessContext,
    *,
    future_tolerance_seconds: float = 300.0,
    provider_attempts: Sequence[ProviderAttempt] = (),
    cache_state: CacheState | str | None = None,
) -> ReliabilityReport:
    """Build one immutable report for the fields promised by enabled rules."""

    required = tuple(sorted(set(required_fields)))
    metadata_root = snapshot.get("field_metadata", {})
    if not isinstance(metadata_root, Mapping):
        metadata_root = {}
    fields: dict[str, FieldReliability] = {}
    for required_field in required:
        if required_field not in policies:
            raise KeyError(
                f"No freshness policy configured for field {required_field!r}"
            )
        metadata = metadata_root.get(required_field)
        if not isinstance(metadata, Mapping):
            metadata = None
        fields[required_field] = evaluate_field_freshness(
            required_field,
            snapshot.get(required_field),
            metadata,
            _coerce_policy(policies[required_field]),
            context,
            future_tolerance_seconds=future_tolerance_seconds,
        )

    explicit_snapshot_cache = cache_state is not None
    if explicit_snapshot_cache:
        resolved_cache = CacheState(cache_state)
    else:
        field_cache_states = {field.cache_state for field in fields.values()}
        resolved_cache = next(
            (
                state
                for state in (
                    CacheState.STALE_IF_ERROR,
                    CacheState.FRESH,
                    CacheState.MISS,
                )
                if state in field_cache_states
            ),
            CacheState.NONE,
        )
    if explicit_snapshot_cache and resolved_cache is CacheState.STALE_IF_ERROR:
        fields = {
            name: field.model_copy(
                update={
                    "status": FreshnessStatus.STALE,
                    "usable_for_signal": False,
                    "reason": "bounded stale-if-error context; signals are disabled",
                    "cache_state": CacheState.STALE_IF_ERROR,
                }
            )
            for name, field in fields.items()
        }

    full_coverage = bool(fields) and all(
        field.usable_for_signal for field in fields.values()
    )
    trusted_silence = full_coverage and all(
        field.time_basis is not TimeBasis.OBSERVED_ONLY
        for field in fields.values()
    )
    usable_count = sum(field.usable_for_signal for field in fields.values())
    foundational_price_blind = (
        "price" in fields and not fields["price"].usable_for_signal
    )
    if foundational_price_blind:
        overall = OverallStatus.BLIND
    elif full_coverage:
        overall = (
            OverallStatus.DEGRADED
            if any(
                field.status is FreshnessStatus.AGING
                or field.time_basis is TimeBasis.OBSERVED_ONLY
                for field in fields.values()
            )
            else OverallStatus.HEALTHY
        )
    elif usable_count:
        overall = OverallStatus.DEGRADED
    else:
        overall = OverallStatus.BLIND

    reasons: list[str] = []
    for name, field_evidence in fields.items():
        if field_evidence.status is not FreshnessStatus.FRESH:
            reasons.append(
                f"{name}:{field_evidence.status.value.lower()}:"
                f"{field_evidence.reason}"
            )
        elif field_evidence.time_basis is TimeBasis.OBSERVED_ONLY:
            reasons.append(f"{name}:observed_only")
    if resolved_cache is CacheState.STALE_IF_ERROR:
        reasons.insert(0, "snapshot:stale_if_error")

    return ReliabilityReport(
        ticker=str(snapshot.get("ticker") or snapshot.get("symbol") or "UNKNOWN"),
        market=str(snapshot.get("market") or "UNKNOWN"),
        overall=overall,
        usable_for_signal=full_coverage,
        full_coverage=full_coverage,
        usable_for_trusted_silence=trusted_silence,
        evaluated_at=context.evaluated_at,
        fields=fields,
        provider_attempts=tuple(provider_attempts),
        reasons=tuple(reasons),
        cache_state=resolved_cache,
    )


def gate_snapshot_for_decision(
    snapshot: Mapping[str, Any],
    report: ReliabilityReport,
    required_fields: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a decision copy with only ineligible capabilities blanked.

    Raw values stay available under ``context_values`` for diagnostics.  This is
    intentionally rule-level gating: a stale PE must not disable a fresh-price
    protective rule, but it can never trigger a PE rule itself.
    """

    gated = dict(snapshot)
    issues = list(snapshot.get("quality_issues") or ())
    context_values = dict(snapshot.get("context_values") or {})
    selected = set(required_fields) if required_fields is not None else set(report.fields)
    for field in sorted(selected):
        evidence = report.fields.get(field)
        if evidence is None or not evidence.usable_for_signal:
            context_values[field] = snapshot.get(field)
            gated[field] = None
            issue = f"{field}:reliability_gate"
            if issue not in issues:
                issues.append(issue)
    gated["context_values"] = context_values
    gated["quality_issues"] = issues
    gated["reliability"] = report.model_dump(mode="json")
    return gated


def triggered_evidence_usable(
    evidence: Iterable[Mapping[str, Any]], report: ReliabilityReport
) -> bool:
    """Verify every actually-triggered rule against its field capabilities."""

    triggered = [item for item in evidence if item.get("status") == "TRIGGERED"]
    if not triggered:
        return False
    for item in triggered:
        rule_type = str(item.get("rule_type") or "")
        try:
            dependencies = fields_for_rule_type(rule_type)
        except ValueError:
            return False
        if any(
            field not in report.fields
            or not report.fields[field].usable_for_signal
            for field in dependencies
        ):
            return False
    return True


def summarize_instrument_coverage(
    reports: Mapping[str, ReliabilityReport],
    enabled_tickers: Iterable[str],
) -> CoverageSummary:
    """Compute the product SLI without inventing a percentage for no samples."""

    enabled = tuple(dict.fromkeys(enabled_tickers))
    usable = sum(
        ticker in reports and reports[ticker].usable_for_trusted_silence
        for ticker in enabled
    )
    unusable = tuple(
        ticker
        for ticker in enabled
        if ticker not in reports or not reports[ticker].usable_for_trusted_silence
    )
    coverage = usable / len(enabled) if enabled else None
    return CoverageSummary(
        enabled_instruments=len(enabled),
        usable_instruments=usable,
        fresh_coverage=coverage,
        unusable_tickers=unusable,
    )
