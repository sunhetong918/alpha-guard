"""Alpha Guard command-line application and supervised workflows.

The runtime is intentionally read-only with respect to brokerage accounts.  It
evaluates user-authored rules, presents evidence for human review, and sends a
notification only after two independent opt-ins: ``--notify`` and
``NOTIFICATIONS_ENABLED=true``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer
from platformdirs import user_state_path
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from config import (
    PROJECT_ROOT,
    InstrumentConfig,
    RulesConfig,
    Settings,
    get_settings,
    load_news_config,
    load_rules_config,
)
from data.fetcher import get_stock
from news.filter import filter_news
from news.sources import fetch_all_news
from notifier.telegram_bot import (
    render_incident_alert,
    render_signal_alert,
    send_incident,
    send_message,
    send_news_alert,
    send_signal,
)
from notifier.heartbeat import (
    delivery_config_fingerprints,
    heartbeat_eligible,
    ping_heartbeat,
)
from notifier.mobile import (
    MobileDeliveryReport,
    configured_mobile_channels,
    deliver_mobile,
)
from reliability import (
    ProviderRuntime,
    ProviderRuntimeConfig,
    ProviderUnavailableError,
    ReliabilityReport,
    evaluate_snapshot_reliability,
    gate_snapshot_for_decision,
    required_fields_for_rules,
    summarize_instrument_coverage,
    triggered_evidence_usable,
)
from scheduler import (
    build_scheduler,
    expected_market_scans_between,
    latest_expected_market_scan,
    market_freshness_context,
    next_market_run,
    next_news_run,
)
from signals.engine import EvaluationDecision, RuleStatus, evaluate
from state import (
    BlindnessObservation,
    CorruptProtectionStateError,
    ProtectionObservationCollisionError,
    ProtectionSnapshot,
    StateStore,
    protection_contract_version,
    transition_protection,
    watchdog_scope_generation,
)
from state.cockpit import (
    build_corrupt_reliability_cockpit,
    build_reliability_cockpit,
)

logger = logging.getLogger(__name__)
console = Console()

_APPLICATION_LOGGERS = (
    "main",
    "config",
    "data",
    "reliability",
    "news",
    "notifier",
    "signals",
    "state",
    "scheduler",
    "analysis",
)
_SECRET_TRANSPORT_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "requests",
    "telegram",
    "yfinance",
    "peewee",
    "aiohttp",
    "curl_cffi",
    "anthropic",
    "hpack",
)


class _ApplicationLogFilter(logging.Filter):
    """Allow production handlers to emit only project-owned log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.split(".", 1)[0] in _APPLICATION_LOGGERS

STATE_PATH = (
    PROJECT_ROOT / ".alpha-guard" / "state.db"
    if (PROJECT_ROOT / "pyproject.toml").is_file()
    else user_state_path("alpha-guard") / "state.db"
)
DEFAULT_FIXTURE_PATH = PROJECT_ROOT / "data" / "fixtures" / "snapshots.json"
SUPPORTED_MARKETS = frozenset({"US", "HK"})
TRUST_RECEIPT_MESSAGE = (
    "Alpha Guard Trust Receipt：移动交付通道已验证；未执行任何交易。"
)

app = typer.Typer(
    name="alpha-guard",
    no_args_is_help=True,
    add_completion=False,
    help="港美股自托管、只读、证据优先的决策守门员。",
)


def _configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        # Never put the root logger in DEBUG: HTTP clients can log full secret
        # request paths.  Verbosity is an explicit application allowlist.
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        force=True,
    )
    for logger_name in _APPLICATION_LOGGERS:
        logging.getLogger(logger_name).setLevel(
            logging.DEBUG if verbose else logging.INFO
        )
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.addFilter(_ApplicationLogFilter())
    # These clients are known to include bearer-style secrets in request URLs.
    # Raising their logger threshold also protects test/host handlers that are
    # attached outside our filtered production handler.
    for logger_name in _SECRET_TRANSPORT_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.CRITICAL + 1)


def _market(value: str | None) -> str | None:
    if value is None:
        return None
    market = value.strip().upper()
    if market not in SUPPORTED_MARKETS:
        choices = ", ".join(sorted(SUPPORTED_MARKETS))
        raise ValueError(f"不支持的市场 {value!r}；可选值：{choices}")
    return market


def _telegram_configured(settings: Settings) -> bool:
    return bool(
        settings.notifications_enabled
        and settings.telegram_bot_token is not None
        and (settings.telegram_chat_id or "").strip()
    )


def _notification_error(settings: Settings) -> str | None:
    if configured_mobile_channels(settings):
        return None
    return "未配置可用的 Telegram 或 WhatsApp 通道"


def _dependency_status(module: str, *, enabled: bool = True) -> str:
    installed = importlib.util.find_spec(module) is not None
    if installed:
        return "已安装"
    return "缺少可选 extra" if enabled else "未安装（能力未启用）"


def _cockpit_configuration(
    rules: RulesConfig,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return the current protection responsibility without exposing rules."""

    enabled: dict[str, str] = {
        ticker: instrument.market
        for ticker, instrument in rules.watchlist.items()
        if instrument.enabled
    }
    contracts: dict[str, str] = {
        market: protection_contract_version(rules, market)
        for market in sorted(set(enabled.values()))
    }
    return enabled, contracts


def _doctor_sqlite_status(path: Path) -> str:
    """Run SQLite quick_check through a read-only, query-only connection."""

    if not path.is_file():
        return "missing"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute("PRAGMA quick_check").fetchall()
        if rows and all(row[0] == "ok" for row in rows):
            return "ok"
        return "corrupt"
    except sqlite3.Error:
        return "corrupt"
    finally:
        if connection is not None:
            connection.close()


def _require_notifications(settings: Settings) -> None:
    problem = _notification_error(settings)
    if problem:
        raise ValueError(f"通知未就绪：{problem}")


def _record_delivery_state(
    store: StateStore,
    settings: Settings,
    channel: Literal["telegram", "whatsapp", "heartbeat"],
    *,
    mode: Literal["active", "preview"],
    attempted_at: datetime | None = None,
    success: bool | None = None,
    error_code: str | None = None,
    now: datetime,
    ) -> None:
    fingerprints = delivery_config_fingerprints(settings)
    if channel == "telegram":
        configured = _telegram_configured(settings)
    elif channel == "whatsapp":
        configured = settings.whatsapp_enabled
    else:
        configured = bool(
            settings.heartbeat_enabled and settings.heartbeat_url is not None
        )
    store.record_delivery_state(
        channel,
        config_fingerprint=fingerprints[channel],
        configured=configured,
        mode=mode,
        attempted_at=attempted_at,
        success=success,
        error_code=error_code,
        now=now,
    )


def _record_mobile_delivery_modes(
    store: StateStore,
    settings: Settings,
    *,
    mode: Literal["active", "preview"],
    now: datetime,
) -> None:
    for channel in ("telegram", "whatsapp"):
        configured = (
            _telegram_configured(settings)
            if channel == "telegram"
            else settings.whatsapp_enabled
        )
        _record_delivery_state(
            store,
            settings,
            channel,
            mode=mode if configured else "preview",
            now=now,
        )


def _record_mobile_workflow_result(
    store: StateStore,
    settings: Settings,
    *,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    """Fold this workflow's outbox attempts without mixing channel health."""

    fingerprints = delivery_config_fingerprints(settings)
    rows = store.outbound_deliveries(not_after=completed_at)
    for channel in configured_mobile_channels(settings):
        attempts = [
            row
            for row in rows
            if row["channel"] == channel
            and row["config_fingerprint"] == fingerprints[channel]
            and row["last_attempt_at"] is not None
            and datetime.fromisoformat(row["last_attempt_at"]).astimezone(UTC)
            >= started_at
        ]
        if not attempts:
            continue
        error = next(
            (
                str(row["error_code"])
                for row in attempts
                if row["status"] != "sent" and row["error_code"] is not None
            ),
            None,
        )
        latest_attempt = max(
            datetime.fromisoformat(str(row["last_attempt_at"])).astimezone(UTC)
            for row in attempts
        )
        _record_delivery_state(
            store,
            settings,
            channel,
            mode="active",
            attempted_at=latest_attempt,
            success=error is None,
            error_code=error,
            now=completed_at,
        )


def _error_code(exc: BaseException) -> str:
    """Return a bounded error taxonomy without exception text or secrets."""

    if isinstance(exc, _MobileDeliveryRejected):
        return exc.error_code
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    raw = type(exc).__name__.lower()
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in raw
    ).strip("_")
    return (normalized or "unknown")[:64]


class _MobileDeliveryRejected(RuntimeError):
    """Opaque multi-channel failure carrying only a bounded error code."""

    def __init__(self, error_code: str) -> None:
        super().__init__("mobile delivery was not accepted")
        self.error_code = error_code


def _mobile_report_error(report: MobileDeliveryReport) -> str | None:
    if report.accepted:
        return None
    for channel in report.required_channels:
        for item in report.channels:
            if item.channel == channel and not item.accepted:
                return item.error_code or "not_accepted"
    return "not_configured"


def _raise_mobile_delivery(error_code: str) -> None:
    if error_code == "timeout":
        raise TimeoutError("mobile delivery timed out")
    if error_code == "connection":
        raise ConnectionError("mobile delivery connection failed")
    raise _MobileDeliveryRejected(error_code)


def _mobile_business_key(kind: str, *parts: object) -> str:
    digest = hashlib.sha256()
    digest.update(b"alpha-guard:mobile-business:v1\x00")
    digest.update(kind.encode("ascii"))
    for part in parts:
        digest.update(b"\x00")
        digest.update(str(part).encode("utf-8"))
    return f"{kind}:{digest.hexdigest()}"


def _signal_delivery_edge_id(
    store: StateStore,
    signal_key: str,
    fingerprint: str,
) -> str:
    row = store.connection.execute(
        """
        SELECT s.activated_at, s.last_sent_at, e.id
        FROM signal_state AS s
        LEFT JOIN signal_events AS e
          ON e.signal_key = s.signal_key
         AND e.fingerprint = s.fingerprint
         AND e.event_type IN ('activated', 'evidence_changed')
        WHERE s.signal_key = ? AND s.fingerprint = ?
        ORDER BY e.id DESC
        LIMIT 1
        """,
        (signal_key, fingerprint),
    ).fetchone()
    if row is None or row["id"] is None:
        raise CorruptProtectionStateError("signal delivery edge is missing")
    return f"{row['id']}:{row['activated_at']}:{row['last_sent_at'] or 'first'}"


def _persist_and_close_runtime(
    store: StateStore,
    runtime: ProviderRuntime,
    at: datetime,
    *,
    persist: bool = True,
    delivery_status: Literal["pending", "suppressed"] = "suppressed",
    raise_on_failure: bool = False,
) -> None:
    persistence_error: BaseException | None = None
    try:
        if persist:
            store.save_provider_runtime_state(
                runtime.export_state().model_dump(mode="json"), now=at
            )
    except Exception as exc:  # noqa: BLE001 - cleanup must preserve main result
        persistence_error = exc
        logger.error("provider runtime state persistence failed: %s", _error_code(exc))
        try:
            store.observe_integrity_incident(
                "global",
                "provider_runtime",
                "state_corrupt",
                delivery_status=delivery_status,
                now=at,
            )
        except Exception:  # noqa: BLE001 - original failure remains authoritative
            pass
    finally:
        try:
            store.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask root cause
            logger.error("state store close failed: %s", _error_code(exc))
    if persistence_error is not None and raise_on_failure:
        raise RuntimeError("provider_runtime_persistence_failed") from None


def _rules_snapshot(rules: RulesConfig) -> dict[str, Any]:
    snapshot = rules.model_dump(mode="python")
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot["version"] = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    return snapshot


def _instrument_rules_version(
    instrument: InstrumentConfig,
    rules: RulesConfig | None = None,
) -> str:
    required_fields = required_fields_for_rules(instrument)
    reliability_policy: dict[str, Any] | None = None
    if rules is not None:
        freshness_fields = cast(
            Mapping[str, Any], rules.reliability.freshness.fields
        )
        reliability_policy = {
            "fields": {
                field: freshness_fields[field].model_dump(mode="python")
                for field in sorted(required_fields)
            },
            "future_tolerance_seconds": (
                rules.reliability.freshness.future_tolerance_seconds
            ),
        }
    payload = {
        "buy_rules": [rule.model_dump(mode="python") for rule in instrument.buy_rules],
        "cost_basis": instrument.cost_basis,
        "currency": instrument.currency,
        "market": instrument.market,
        "reliability_policy": reliability_policy,
        "sell_rules": [
            rule.model_dump(mode="python") for rule in instrument.sell_rules
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _selected_instruments(
    rules: RulesConfig,
    market: str | None,
    *,
    include_disabled: bool,
) -> list[tuple[str, InstrumentConfig]]:
    return [
        (ticker, instrument)
        for ticker, instrument in rules.watchlist.items()
        if (include_disabled or instrument.enabled)
        and (market is None or instrument.market == market)
    ]


def _load_fixture(path: Path) -> dict[str, dict[str, Any]]:
    with path.expanduser().resolve().open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise TypeError("离线快照根节点必须是对象")
    raw_snapshots = payload.get("snapshots", payload)
    if not isinstance(raw_snapshots, Mapping):
        raise TypeError("离线快照的 snapshots 必须是对象")

    snapshots: dict[str, dict[str, Any]] = {}
    for ticker, snapshot in raw_snapshots.items():
        if not isinstance(ticker, str) or not isinstance(snapshot, Mapping):
            raise TypeError("每个离线快照必须使用字符串 ticker 和对象值")
        snapshots[ticker] = dict(snapshot)
    return snapshots


async def _fetch_snapshots(
    instruments: Sequence[tuple[str, InstrumentConfig]],
    fixture_path: Path | None,
    *,
    rules: RulesConfig,
    provider_runtime: ProviderRuntime,
    evaluated_at: datetime,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, ReliabilityReport],
    list[dict[str, str]],
]:
    if fixture_path is not None:
        fixture = _load_fixture(fixture_path)
        snapshots: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        for ticker, _instrument in instruments:
            if ticker not in fixture:
                errors[ticker] = "离线样例中没有该标的"
            else:
                snapshots[ticker] = fixture[ticker]
        return snapshots, errors, {}, []

    async def fetch_one(
        ticker: str, instrument: InstrumentConfig
    ) -> tuple[str, dict[str, Any] | None, ReliabilityReport | None, Exception | None]:
        required_fields = required_fields_for_rules(instrument)
        policies = cast(Mapping[str, Any], rules.reliability.freshness.fields)
        context = market_freshness_context(instrument.market, evaluated_at)
        try:
            snapshot = await asyncio.to_thread(
                get_stock,
                ticker,
                instrument.market,
                required_fields=required_fields,
                freshness_policies=policies,
                freshness_context=context,
                future_tolerance_seconds=(
                    rules.reliability.freshness.future_tolerance_seconds
                ),
                provider_runtime=provider_runtime,
                timeout_seconds=rules.reliability.provider.request_timeout_seconds,
            )
            report = ReliabilityReport.model_validate(snapshot.get("reliability"))
            # The data adapter already gates, but the workflow repeats the
            # fail-closed check at its own trust boundary.
            gated = gate_snapshot_for_decision(snapshot, report, required_fields)
            return ticker, gated, report, None
        except ProviderUnavailableError as exc:
            return ticker, None, exc.report, exc
        except Exception as exc:  # noqa: BLE001 - provider boundary
            return ticker, None, None, exc

    fetched = await asyncio.gather(
        *(fetch_one(ticker, instrument) for ticker, instrument in instruments)
    )
    snapshots = {}
    errors = {}
    reports: dict[str, ReliabilityReport] = {}
    provider_failures: list[dict[str, str]] = []
    for ticker, snapshot, report, failure in fetched:
        failures_before_report = len(provider_failures)
        if report is not None:
            reports[ticker] = report
            provider_failures.extend(
                {**item, "ticker": ticker}
                for item in _provider_capability_failures(report)
            )
        if failure is not None:
            # Error text is deliberately low-cardinality: provider exceptions
            # may contain URLs, symbols, response bodies or credentials.
            errors[ticker] = type(failure).__name__
            if (
                isinstance(failure, ProviderUnavailableError)
                and len(provider_failures) == failures_before_report
            ):
                provider_failures.append(
                    {
                        "provider": failure.key.provider,
                        "operation": failure.key.operation,
                        "market": failure.key.market,
                        "reason": (
                            "no_data" if failure.attempts else "provider_unavailable"
                        ),
                        "circuit": failure.circuit.state.value,
                        "ticker": ticker,
                    }
                )
        elif snapshot is not None:
            snapshots[ticker] = snapshot
    deduplicated = {
        (
            item["ticker"],
            item["provider"],
            item["operation"],
            item["market"],
            item["reason"],
        ): item
        for item in provider_failures
    }
    return snapshots, errors, reports, list(deduplicated.values())


def _provider_capability_failures(
    report: ReliabilityReport,
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for attempt in report.provider_attempts:
        if attempt.failure_class == "none":
            continue
        failures.append(
            {
                "provider": attempt.provider,
                "operation": attempt.operation,
                "market": attempt.market,
                "reason": attempt.failure_class,
                "circuit": attempt.circuit_state.value,
            }
        )
    return failures


def _revalidate_snapshot_for_decision(
    snapshot: Mapping[str, Any],
    prior_report: ReliabilityReport,
    instrument: InstrumentConfig,
    rules: RulesConfig,
    *,
    evaluated_at: datetime,
) -> tuple[dict[str, Any], ReliabilityReport]:
    """Recompute freshness at the final decision boundary.

    Provider fetching may take long enough to consume a freshness budget or
    cross an exchange phase boundary.  The adapter's first gate is therefore
    necessary but not sufficient.  Recover only adapter-preserved raw values,
    retain provider attempts/cache evidence, then rebuild and apply the gate
    using the current exchange-aware context.
    """

    required_fields = required_fields_for_rules(instrument)
    raw = dict(snapshot)
    context_values = snapshot.get("context_values")
    if isinstance(context_values, Mapping):
        for field in required_fields:
            if field in context_values:
                raw[field] = context_values[field]
    previous_gate_issues = {
        f"{field}:reliability_gate" for field in required_fields
    }
    raw["quality_issues"] = [
        issue
        for issue in snapshot.get("quality_issues", ())
        if issue not in previous_gate_issues
    ]
    policies = cast(Mapping[str, Any], rules.reliability.freshness.fields)
    report = evaluate_snapshot_reliability(
        raw,
        required_fields,
        policies,
        market_freshness_context(instrument.market, evaluated_at),
        future_tolerance_seconds=(
            rules.reliability.freshness.future_tolerance_seconds
        ),
        provider_attempts=prior_report.provider_attempts,
        cache_state=prior_report.cache_state,
    )
    return gate_snapshot_for_decision(raw, report, required_fields), report


_FIELD_BY_RULE = {
    "price_above": "price",
    "price_below": "price",
    "price_drop_pct": "price",
    "pe_above": "pe_ttm",
    "pe_below": "pe_ttm",
    "roe_above": "roe",
}


def _enrich_evidence(
    evidence: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any]
) -> list[dict[str, Any]]:
    sources = snapshot.get("sources", snapshot.get("source", {}))
    provider = str(snapshot.get("provider") or "未提供")
    enriched: list[dict[str, Any]] = []
    for item in evidence:
        rule_type = str(item.get("rule_type") or "")
        field = _FIELD_BY_RULE.get(rule_type, "price")
        source = (
            sources.get(field, provider) if isinstance(sources, Mapping) else provider
        )
        enriched.append(
            {
                **dict(item),
                "source": source,
                "as_of": snapshot.get("as_of", snapshot.get("retrieved_at")),
                "currency": snapshot.get("currency"),
                "quality_issues": snapshot.get("quality_issues", []),
            }
        )
    return enriched


def _relevant_evidence(
    result: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> list[dict[str, Any]]:
    decision = result.get("decision")
    evidence = result.get("evidence", {})
    if not isinstance(evidence, Mapping):
        return []
    sell = evidence.get("sell", [])
    buy = evidence.get("buy", [])
    sell_items = list(sell) if isinstance(sell, Sequence) else []
    buy_items = list(buy) if isinstance(buy, Sequence) else []

    if decision == EvaluationDecision.SELL_REVIEW.value:
        selected = [item for item in sell_items if item.get("status") == "TRIGGERED"]
    elif decision == EvaluationDecision.BUY_REVIEW.value:
        selected = [item for item in buy_items if item.get("status") == "TRIGGERED"]
    elif decision == EvaluationDecision.CONFLICT.value:
        selected = [
            item
            for item in [*sell_items, *buy_items]
            if item.get("status") == "TRIGGERED"
        ]
    elif decision == EvaluationDecision.UNKNOWN.value:
        selected = [
            item
            for item in [*sell_items, *buy_items]
            if item.get("status") == "UNKNOWN"
        ]
    else:
        selected = []
    return _enrich_evidence(selected, snapshot)


def _fingerprint(
    action: str,
    evidence: Sequence[Mapping[str, Any]],
    *,
    config_version: str | None = None,
    cost_basis: float | None = None,
) -> str:
    # Actual quotes and timestamps deliberately stay out of the fingerprint. A
    # moving quote inside the same active rule must not bypass the cooldown.
    stable_evidence = sorted(
        (
            {
                "rule_id": item.get("rule_id"),
                "rule_type": item.get("rule_type"),
                "status": item.get("status"),
                "operator": item.get("operator"),
                "threshold": item.get("threshold"),
                "unit": item.get("unit"),
            }
            for item in evidence
        ),
        key=lambda item: (
            str(item.get("rule_id")),
            str(item.get("rule_type")),
            str(item.get("operator")),
            str(item.get("unit")),
            str(item.get("threshold")),
        ),
    )
    raw = json.dumps(
        {
            "action": action,
            "cost_basis": cost_basis,
            "evidence": stable_evidence,
            "config_version": config_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _signal_payload(
    ticker: str,
    instrument: InstrumentConfig,
    result: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    reasons = [
        str(item.get("note") or item.get("reason") or item.get("rule_id"))
        for item in evidence
    ]
    return {
        "ticker": ticker,
        "name": result.get("name", instrument.name),
        "market": instrument.market,
        "price": result.get("price"),
        "action": result.get("decision"),
        "reasons": reasons,
        "evidence": list(evidence),
        "source": snapshot.get("provider"),
        "as_of": snapshot.get("as_of", snapshot.get("retrieved_at")),
        "currency": snapshot.get("currency", instrument.currency),
        "quality_issues": snapshot.get("quality_issues", []),
        "rules_version": result.get("rules_version"),
        "instrument_rules_version": result.get("instrument_rules_version"),
    }


def _signal_observations(result: Mapping[str, Any]) -> dict[str, bool | None]:
    decision = result.get("decision")
    conflict = decision == EvaluationDecision.CONFLICT.value

    def directional(status: Any, expected_decision: str) -> bool | None:
        if conflict:
            return None
        if decision == expected_decision:
            return True
        if status == RuleStatus.NOT_TRIGGERED.value:
            return False
        return None

    conflict_active: bool | None
    if conflict:
        conflict_active = True
    elif decision == EvaluationDecision.UNKNOWN.value:
        conflict_active = None
    else:
        conflict_active = False
    return {
        EvaluationDecision.SELL_REVIEW.value: directional(
            result.get("sell_status"), EvaluationDecision.SELL_REVIEW.value
        ),
        EvaluationDecision.BUY_REVIEW.value: directional(
            result.get("buy_status"), EvaluationDecision.BUY_REVIEW.value
        ),
        EvaluationDecision.CONFLICT.value: conflict_active,
    }


async def _apply_notification_state(
    store: StateStore,
    *,
    ticker: str,
    instrument: InstrumentConfig,
    result: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    settings: Settings,
    now: datetime | None = None,
    record_delivery_state: bool = True,
) -> bool:
    delivery_at = now or datetime.now(UTC)
    if delivery_at.tzinfo is None or delivery_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    delivery_at = delivery_at.astimezone(UTC)
    if not instrument.enabled:
        raise ValueError(f"禁用标的 {ticker} 不能进入真实通知边界")
    relevant = _relevant_evidence(result, snapshot)
    raw_report = snapshot.get("reliability")
    if raw_report is not None:
        report = ReliabilityReport.model_validate(raw_report)
        decision = result.get("decision")
        directional = decision in {
            EvaluationDecision.BUY_REVIEW.value,
            EvaluationDecision.SELL_REVIEW.value,
            EvaluationDecision.CONFLICT.value,
        }
        if directional and not triggered_evidence_usable(relevant, report):
            # Preserve any previously active edge. Untrusted evidence is
            # UNKNOWN at the notification boundary, never an implicit reset.
            return False
    observations = _signal_observations(result)
    selected_key: str | None = None
    selected_fingerprint: str | None = None
    selected_claim: str | None = None

    for action, active in observations.items():
        key = f"{ticker}:{action}"
        action_evidence = relevant if result.get("decision") == action else []
        fingerprint = _fingerprint(
            action,
            action_evidence,
            config_version=result.get("instrument_rules_version"),
            cost_basis=instrument.cost_basis,
        )
        claim = store.claim_signal_notification(
            key,
            active,
            fingerprint,
            instrument.alert_cooldown_hours,
            now=delivery_at,
        )
        if active and claim is not None and result.get("decision") == action:
            selected_key = key
            selected_fingerprint = fingerprint
            selected_claim = claim

    if selected_key is None or selected_fingerprint is None or selected_claim is None:
        return False

    payload = _signal_payload(ticker, instrument, result, snapshot, relevant)
    delivery_edge = _signal_delivery_edge_id(
        store, selected_key, selected_fingerprint
    )
    try:
        delivery_report = await deliver_mobile(
            store,
            business_key=_mobile_business_key(
                "signal", selected_key, selected_fingerprint, delivery_edge
            ),
            kind="signal",
            payload=payload,
            settings=settings,
            now=delivery_at,
            clock=lambda: delivery_at,
            telegram_sender=(
                (lambda: send_signal(payload, settings=settings))
                if _telegram_configured(settings)
                else None
            ),
            record_delivery_state=record_delivery_state,
        )
        error = _mobile_report_error(delivery_report)
        if error is not None:
            raise _MobileDeliveryRejected(error)
        store.mark_signal_notified(
            selected_key,
            selected_fingerprint,
            claim_token=selected_claim,
            now=delivery_at,
        )
    except Exception:
        try:
            store.release_notification_claim(selected_key, selected_claim)
        except Exception:  # noqa: BLE001 - preserve the original delivery error
            logger.error("释放信号通知 claim 失败；将等待 lease 自动过期")
        raise
    return True


async def _deliver_current_incident(
    store: StateStore,
    *,
    scope: str,
    settings: Settings,
    clock: Callable[[], datetime],
    record_delivery_state: bool = True,
) -> tuple[bool, bool, str | None, int | None]:
    """Claim and deliver only the latest still-relevant operational edge."""

    pending = store.pending_current_incident_event(scope)
    if pending is None:
        return False, False, None, None
    event_id = int(pending["id"])
    claim_at = clock()
    if claim_at.tzinfo is None or claim_at.utcoffset() is None:
        raise ValueError("clock must return timezone-aware datetimes")
    claim_at = claim_at.astimezone(UTC)
    claim = store.claim_incident_notification(event_id, now=claim_at)
    if claim is None:
        return False, False, None, event_id
    try:
        payload = dict(pending["payload"])
        coverage = payload.get("coverage")
        if isinstance(coverage, Mapping):
            payload["affected_tickers"] = list(
                coverage.get("unusable_tickers") or ()
            )
        # Presentation validation belongs inside the retryable claim boundary.
        payload["message_preview"] = render_incident_alert(payload)
        report = await deliver_mobile(
            store,
            business_key=_mobile_business_key("incident", event_id),
            kind="incident",
            payload=payload,
            settings=settings,
            now=claim_at,
            clock=lambda: claim_at,
            telegram_sender=(
                (lambda: send_incident(payload, settings=settings))
                if _telegram_configured(settings)
                else None
            ),
            record_delivery_state=record_delivery_state,
        )
        error = _mobile_report_error(report)
        if error is not None:
            raise _MobileDeliveryRejected(error)
        sent_at = clock()
        if sent_at.tzinfo is None or sent_at.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetimes")
        store.mark_incident_notified(
            event_id,
            claim,
            now=sent_at.astimezone(UTC),
        )
    except Exception as exc:  # noqa: BLE001 - operational delivery boundary
        try:
            store.release_notification_claim(f"incident:{event_id}", claim)
        except Exception:  # noqa: BLE001 - preserve original low-card error
            logger.error("释放 operational notification claim 失败")
        return True, False, _error_code(exc), event_id
    return True, True, None, event_id


async def _deliver_watchdog_incident(
    store: StateStore,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    record_delivery_state: bool = True,
) -> tuple[bool, bool, str | None, int | None]:
    """Deliver one durable, aggregate deadline incident with a claim lease."""

    claim_at = clock()
    if claim_at.tzinfo is None or claim_at.utcoffset() is None:
        raise ValueError("clock must return timezone-aware datetimes")
    claim_at = claim_at.astimezone(UTC)
    pending = store.pending_watchdog_incident(not_after=claim_at)
    if pending is None:
        return False, False, None, None
    incident_id = int(pending["id"])
    claim = store.claim_watchdog_incident_notification(
        incident_id,
        now=claim_at,
    )
    if claim is None:
        return False, False, None, incident_id
    send_started = False
    try:
        validation_at = clock()
        if validation_at.tzinfo is None or validation_at.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetimes")
        validation_at = validation_at.astimezone(UTC)
        if validation_at < claim_at:
            raise ValueError("clock moved backwards during watchdog delivery")
        # Re-read every safety ledger after taking the lease. The immutable
        # watchdog row owns delivery, while current per-market snapshots own
        # presentation. This coalesces a same-workflow late full scan into a
        # RECOVERING receipt instead of sending a stale red alert.
        current = store.read_claimed_watchdog_incident(
            incident_id,
            claim,
            not_after=validation_at,
        )
        if current is None:
            return False, False, None, incident_id
        evidence = current["payload"]
        scope_record = store.get_protection_scope("global")
        if scope_record is None:
            raise CorruptProtectionStateError(
                "watchdog incident has no current protection scope"
            )
        _validate_watchdog_scope_at(scope_record, validation_at)
        market_states = store.protection_states()
        for snapshot in market_states.values():
            if any(
                value is not None and value > validation_at
                for value in (
                    snapshot.state_since,
                    snapshot.updated_at,
                    snapshot.incident_started_at,
                    snapshot.blind_started_at,
                    snapshot.recovered_at,
                    snapshot.last_success_at,
                )
            ):
                raise CorruptProtectionStateError(
                    "protection state is future-dated at delivery"
                )
        affected_states = []
        for market in evidence["markets"]:
            market_snapshot: ProtectionSnapshot | None = market_states.get(
                f"market:{market}"
            )
            if market_snapshot is None:
                raise CorruptProtectionStateError(
                    "watchdog incident has no current market state"
                )
            affected_states.append(market_snapshot)

        ledger_state = str(current["state"])
        if ledger_state == "RECOVERED":
            display_state = "RECOVERED"
            reasons = ["watchdog_recovered"]
            action = "核验盲区与恢复时间，继续只读监控"
        elif any(
            snapshot.state.value in {"BLIND", "DEGRADED"}
            for snapshot in affected_states
        ):
            display_state = "BLIND"
            reasons = ["expected_window_missing"]
            action = "检查调度器、数据提供者与最近应跑窗口"
        else:
            display_state = "RECOVERING"
            confirmations = min(
                snapshot.healthy_confirmations
                for snapshot in affected_states
            )
            reasons = [
                "expected_window_missing",
                f"recovery_confirmation_{min(confirmations, 1)}_of_2",
            ]
            action = "已恢复第 1 次 full scan；等待下一次独立确认"
        last_successes = [
            snapshot.last_success_at
            for snapshot in affected_states
            if snapshot.last_success_at is not None
        ]
        last_success_at = (
            min(last_successes).isoformat() if last_successes else None
        )
        payload = {
            "state": display_state,
            "scope": "global",
            "state_since": current["first_seen_at"],
            "incident_started_at": current["first_seen_at"],
            "blind_started_at": current["first_seen_at"],
            "recovered_at": current["resolved_at"],
            "last_success_at": last_success_at,
            "affected_tickers": list(evidence["affected_tickers"]),
            "reason_codes": reasons,
            "recommended_action": action,
        }
        payload["message_preview"] = render_incident_alert(payload)
        report = await deliver_mobile(
            store,
            business_key=_mobile_business_key(
                "watchdog",
                incident_id,
                current["generation"],
                current["delivery_kind"],
            ),
            kind="incident",
            payload=payload,
            settings=settings,
            now=validation_at,
            clock=lambda: validation_at,
            telegram_sender=(
                (lambda: send_incident(payload, settings=settings))
                if _telegram_configured(settings)
                else None
            ),
            record_delivery_state=record_delivery_state,
        )
        send_started = report.attempted
        error = _mobile_report_error(report)
        if error is not None:
            raise _MobileDeliveryRejected(error)
        sent_at = clock()
        if sent_at.tzinfo is None or sent_at.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetimes")
        store.mark_watchdog_incident_notified(
            incident_id,
            claim,
            now=sent_at.astimezone(UTC),
        )
    except Exception as exc:  # noqa: BLE001 - operational delivery boundary
        try:
            store.release_notification_claim(
                f"watchdog:{incident_id}",
                claim,
            )
        except Exception:  # noqa: BLE001 - preserve original low-card error
            logger.error("释放 watchdog notification claim 失败")
        error = (
            "state_corrupt"
            if isinstance(exc, CorruptProtectionStateError)
            else _error_code(exc)
        )
        return (
            send_started,
            False,
            error,
            incident_id,
        )
    return True, True, None, incident_id


async def _deliver_pending_integrity_incidents(
    store: StateStore,
    *,
    scope: str | None,
    settings: Settings,
    clock: Callable[[], datetime],
    record_delivery_state: bool = True,
) -> tuple[int, int, dict[str, str], list[int]]:
    """Deliver durable low-cardinality integrity incidents with claim leases."""

    attempted = 0
    notified = 0
    errors: dict[str, str] = {}
    event_ids: list[int] = []
    for pending in store.pending_integrity_incidents(scope=scope):
        incident_id = int(pending["id"])
        component = str(pending["component"])
        repair_scope = {
            "provider_runtime": "provider-runtime",
            "run_log": "run-log",
            "protection_scope": "global",
        }.get(component, str(pending["scope"]))
        claim_at = clock()
        if claim_at.tzinfo is None or claim_at.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetimes")
        claim = store.claim_integrity_notification(
            incident_id,
            now=claim_at.astimezone(UTC),
        )
        if claim is None:
            continue
        attempted += 1
        event_ids.append(incident_id)
        payload = {
            "state": "BLIND",
            "scope": pending["scope"],
            "state_since": pending["first_seen_at"],
            "incident_started_at": pending["first_seen_at"],
            "blind_started_at": pending["first_seen_at"],
            "last_success_at": None,
            "affected_tickers": [],
            "reason_codes": [pending["reason_code"], component],
            "recommended_action": (
                "运行 alpha-guard repair-state --scope "
                f"{repair_scope} --confirm；先保留本地数据库备份"
            ),
        }
        try:
            payload["message_preview"] = render_incident_alert(payload)
            report = await deliver_mobile(
                store,
                business_key=_mobile_business_key(
                    "integrity", incident_id, component
                ),
                kind="incident",
                payload=payload,
                settings=settings,
                now=claim_at.astimezone(UTC),
                clock=lambda: claim_at.astimezone(UTC),
                telegram_sender=(
                    (lambda: send_incident(payload, settings=settings))
                    if _telegram_configured(settings)
                    else None
                ),
                record_delivery_state=record_delivery_state,
            )
            error = _mobile_report_error(report)
            if error is not None:
                raise _MobileDeliveryRejected(error)
            sent_at = clock()
            if sent_at.tzinfo is None or sent_at.utcoffset() is None:
                raise ValueError("clock must return timezone-aware datetimes")
            store.mark_integrity_notified(
                incident_id,
                claim,
                now=sent_at.astimezone(UTC),
            )
        except Exception as exc:  # noqa: BLE001 - operational delivery boundary
            try:
                store.release_notification_claim(
                    f"integrity:{incident_id}", claim
                )
            except Exception:  # noqa: BLE001 - preserve original error code
                logger.error("释放 integrity notification claim 失败")
            errors[component] = _error_code(exc)
        else:
            notified += 1
    return attempted, notified, errors, event_ids


def _observe_signal_state(
    store: StateStore,
    *,
    ticker: str,
    instrument: InstrumentConfig,
    result: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> None:
    """Persist real-scan state edges without claiming or sending anything."""

    relevant = _relevant_evidence(result, snapshot)
    for action, active in _signal_observations(result).items():
        action_evidence = relevant if result.get("decision") == action else []
        store.should_notify_signal(
            f"{ticker}:{action}",
            active,
            _fingerprint(
                action,
                action_evidence,
                config_version=result.get("instrument_rules_version"),
                cost_basis=instrument.cost_basis,
            ),
            instrument.alert_cooldown_hours,
        )


def _reliability_reason_codes(
    *,
    enabled: int,
    usable: int,
    reports: Mapping[str, ReliabilityReport],
    provider_failures: Sequence[Mapping[str, str]],
    workflow_failure_codes: Sequence[str] = (),
) -> tuple[str, ...]:
    reasons: list[str] = []
    if enabled and usable == 0:
        reasons.append("coverage_zero")
    elif usable < enabled:
        reasons.append("partial_coverage")
    if provider_failures:
        reasons.append("provider_degraded")
        reasons.extend(
            f"provider:{item.get('reason', 'unknown')}"
            for item in provider_failures
        )
    report_reasons = [reason for report in reports.values() for reason in report.reasons]
    if any(":stale:" in reason for reason in report_reasons):
        reasons.append("stale_data")
    if any(":unknown:" in reason for reason in report_reasons):
        reasons.append("unknown_freshness")
    reasons.extend(workflow_failure_codes)
    return tuple(dict.fromkeys(reasons))


def _validate_watchdog_scope_at(
    scope_record: Mapping[str, Any],
    observed_at: datetime,
) -> None:
    """Validate all scope-generation timestamps against one decision clock."""

    try:
        raw_activated_at = scope_record["activated_at"]
        raw_updated_at = scope_record["updated_at"]
        raw_market_epochs = scope_record["market_epochs"]
        if (
            not isinstance(raw_activated_at, str)
            or not isinstance(raw_updated_at, str)
            or not isinstance(raw_market_epochs, Mapping)
            or any(
                not isinstance(market, str) or not isinstance(epoch, str)
                for market, epoch in raw_market_epochs.items()
            )
        ):
            raise ValueError("watchdog scope timestamps must be text")
        parsed_scope_times = (
            datetime.fromisoformat(raw_activated_at),
            datetime.fromisoformat(raw_updated_at),
            *(datetime.fromisoformat(epoch) for epoch in raw_market_epochs.values()),
        )
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in parsed_scope_times
        ):
            raise ValueError("watchdog scope timestamps must be timezone-aware")
        scope_times = tuple(
            value.astimezone(UTC) for value in parsed_scope_times
        )
        if any(value > observed_at for value in scope_times):
            raise ValueError("watchdog scope is future-dated")
    except (KeyError, TypeError, ValueError):
        raise CorruptProtectionStateError(
            "persisted protection scope is future-dated or corrupt"
        ) from None


def _reconcile_watchdog_incident(
    store: StateStore,
    rules: RulesConfig,
    scope_record: Mapping[str, Any],
    *,
    now: datetime,
    delivery_status: Literal["pending", "suppressed"],
) -> dict[str, Any] | None:
    """Reconcile durable operational intent from immutable market evidence."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("watchdog time must be timezone-aware")
    observed_at = now.astimezone(UTC)
    if delivery_status not in {"pending", "suppressed"}:
        raise ValueError("delivery_status must be pending or suppressed")

    _validate_watchdog_scope_at(scope_record, observed_at)

    windows = store.protection_windows()
    states = store.protection_states()
    incidents = store.watchdog_incidents(not_after=observed_at)
    active = next((item for item in incidents if item["active"]), None)
    markets = tuple(scope_record["enabled_markets"])
    enabled_by_market: dict[str, tuple[str, ...]] = {
        market: tuple(
            sorted(
                ticker
                for ticker, instrument in rules.watchlist.items()
                if instrument.enabled and instrument.market == market
            )
        )
        for market in markets
    }
    enabled_tickers = tuple(
        ticker
        for market in sorted(enabled_by_market)
        for ticker in enabled_by_market[market]
    )
    if any(not enabled_by_market[market] for market in markets):
        raise CorruptProtectionStateError(
            "watchdog scope contains an empty enabled market"
        )
    if not enabled_tickers:
        if active is not None:
            return store.resolve_watchdog_incident(
                delivery_status="suppressed",
                now=observed_at,
            )
        return None

    for state_snapshot in states.values():
        evidence_times = (
            state_snapshot.state_since,
            state_snapshot.updated_at,
            state_snapshot.incident_started_at,
            state_snapshot.blind_started_at,
            state_snapshot.recovered_at,
            state_snapshot.last_success_at,
        )
        if any(
            value is not None and value > observed_at
            for value in evidence_times
        ):
            raise CorruptProtectionStateError(
                "persisted protection state is future-dated"
            )

    scope_generation = watchdog_scope_generation(scope_record)
    bad_windows: dict[str, dict[str, Any]] = {}
    outstanding_keys: set[str] = set()
    current_affected: set[str] = set()
    for window in windows:
        updated_at = datetime.fromisoformat(window["updated_at"]).astimezone(UTC)
        expected_at = datetime.fromisoformat(window["expected_at"]).astimezone(UTC)
        deadline_at = datetime.fromisoformat(window["deadline_at"]).astimezone(UTC)
        actual_at = (
            datetime.fromisoformat(window["actual_at"]).astimezone(UTC)
            if window["actual_at"] is not None
            else None
        )
        last_success_at = (
            datetime.fromisoformat(window["last_success_at"]).astimezone(UTC)
            if window["last_success_at"] is not None
            else None
        )
        # A deadline may still be in the future while its promise is inside
        # grace. Every piece of already-observed evidence must be no later
        # than the decision clock, and the full ledger is checked before the
        # responsibility/epoch view is applied.
        if any(
            value is not None and value > observed_at
            for value in (updated_at, expected_at, actual_at, last_success_at)
        ):
            raise CorruptProtectionStateError(
                "persisted protection window is future-dated"
            )
        market = window["market"]
        if market not in enabled_by_market or window["status"] != "bad":
            continue
        epoch = datetime.fromisoformat(
            scope_record["market_epochs"][market]
        ).astimezone(UTC)
        if expected_at < epoch or deadline_at >= observed_at:
            continue
        bad_windows[window["window_key"]] = window
        market_snapshot = states.get(f"market:{market}")
        recovered = (
            market_snapshot is not None
            and market_snapshot.state.value == "HEALTHY"
            and market_snapshot.recovery_has_full_scan
            and market_snapshot.healthy_confirmations >= 2
            and market_snapshot.recovered_at is not None
            and market_snapshot.recovered_at >= deadline_at
            and market_snapshot.last_success_at is not None
            and market_snapshot.last_success_at >= deadline_at
        )
        if not recovered:
            outstanding_keys.add(window["window_key"])
            current_affected.update(enabled_by_market[market])

    for market in markets:
        market_snapshot = states.get(f"market:{market}")
        if market_snapshot is None:
            continue
        snapshot_affected = set(market_snapshot.coverage.unusable_tickers)
        if not snapshot_affected.issubset(enabled_by_market[market]):
            raise CorruptProtectionStateError(
                "protection state contains an out-of-scope ticker"
            )
        current_affected.update(snapshot_affected)

    if active is not None:
        active_payload = active["payload"]
        active_scope_generation = active_payload["scope_generation"]
        if active_scope_generation == scope_generation:
            active_keys = set(active_payload["window_keys"])
            if not active_keys.issubset(bad_windows):
                raise CorruptProtectionStateError(
                    "watchdog incident references missing deadline evidence"
                )
            if not outstanding_keys:
                return store.resolve_watchdog_incident(
                    delivery_status=delivery_status,
                    now=observed_at,
                )
            outstanding_keys.update(active_keys)
            current_affected.update(active_payload["affected_tickers"])
            first_seen = datetime.fromisoformat(
                active_payload["first_seen_at"]
            ).astimezone(UTC)
        elif not outstanding_keys:
            return store.resolve_watchdog_incident(
                delivery_status="suppressed",
                now=observed_at,
            )
        else:
            first_seen = min(
                datetime.fromisoformat(bad_windows[key]["updated_at"]).astimezone(
                    UTC
                )
                for key in outstanding_keys
            )
    elif not outstanding_keys:
        return None
    else:
        first_seen = min(
            datetime.fromisoformat(bad_windows[key]["updated_at"]).astimezone(UTC)
            for key in outstanding_keys
        )

    incident_markets = tuple(
        sorted({key.split(":", 1)[0] for key in outstanding_keys})
    )
    incident = store.observe_watchdog_incident(
        scope_generation=scope_generation,
        enabled_instruments=len(enabled_tickers),
        affected_tickers=tuple(sorted(current_affected)),
        markets=incident_markets,
        window_keys=tuple(sorted(outstanding_keys)),
        first_seen_at=first_seen,
        delivery_status=delivery_status,
        now=observed_at,
    )
    if delivery_status == "pending":
        store.ensure_current_watchdog_incident_pending(now=observed_at)
        refreshed = store.watchdog_incidents(
            active_only=True,
            not_after=observed_at,
        )
        return refreshed[0] if refreshed else incident
    return incident


def _finalize_due_protection_windows(
    store: StateStore,
    rules: RulesConfig,
    *,
    now: datetime,
    delivery_status: Literal["pending", "suppressed"],
) -> dict[str, list[Any]]:
    """Atomically finalize every overdue promise, one market batch at a time."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("watchdog time must be timezone-aware")
    observed_now = now.astimezone(UTC)
    if delivery_status not in {"pending", "suppressed"}:
        raise ValueError("delivery_status must be pending or suppressed")
    scope = store.get_protection_scope("global")
    if scope is None or scope["paused"]:
        return {"finalized": [], "event_ids": []}

    enabled_by_market: dict[str, tuple[str, ...]] = {
        market: tuple(
            sorted(
                ticker
                for ticker, instrument in rules.watchlist.items()
                if instrument.enabled and instrument.market == market
            )
        )
        for market in scope["enabled_markets"]
    }
    finalized: list[str] = []
    event_ids: list[int] = []
    for market in sorted(scope["enabled_markets"]):
        epoch = datetime.fromisoformat(scope["market_epochs"][market]).astimezone(
            UTC
        )
        due = tuple(
            window
            for window in expected_market_scans_between(
                market,
                epoch,
                observed_now,
            )
            if window.expected_at.astimezone(UTC) >= epoch
        )
        batch_finalized, event_id = store.finalize_overdue_market_windows(
            market,
            tuple(
                (window.key, window.expected_at, window.deadline_at)
                for window in due
            ),
            enabled_tickers=enabled_by_market[market],
            now=observed_now,
        )
        finalized.extend(batch_finalized)
        if event_id is not None:
            event_ids.append(event_id)
    _reconcile_watchdog_incident(
        store,
        rules,
        scope,
        now=observed_now,
        delivery_status=delivery_status,
    )
    return {"finalized": finalized, "event_ids": event_ids}


def _record_protection_evidence(
    store: StateStore,
    *,
    scope: str,
    started_at: datetime,
    completed_at: datetime,
    enabled_tickers: Sequence[str],
    reports: Mapping[str, ReliabilityReport],
    provider_failures: Sequence[Mapping[str, str]],
    enabled_markets: Sequence[str],
    market_by_ticker: Mapping[str, str],
    protection_activated_at_by_market: Mapping[str, datetime],
    incident_delivery_status: Literal["pending", "suppressed"] = "suppressed",
    workflow_failure_codes: Sequence[str] = (),
    record_windows: bool = True,
) -> tuple[ProtectionSnapshot, int | None, tuple[int, ...]]:
    coverage = summarize_instrument_coverage(reports, enabled_tickers)
    reason_codes = _reliability_reason_codes(
        enabled=coverage.enabled_instruments,
        usable=coverage.usable_instruments,
        reports=reports,
        provider_failures=provider_failures,
        workflow_failure_codes=workflow_failure_codes,
    )
    windows_by_market = {
        market: latest_expected_market_scan(market, started_at)
        for market in sorted(dict.fromkeys(enabled_markets))
    }
    existing_windows = {
        item["window_key"]: item
        for item in store.protection_windows(until=completed_at)
    }
    missed_markets = [
        market
        for market, window in windows_by_market.items()
        if window.expected_at.astimezone(UTC)
        >= protection_activated_at_by_market[market]
        and completed_at > window.deadline_at.astimezone(UTC)
        and existing_windows.get(window.key, {}).get("status")
        not in {"good", "bad"}
    ]
    deadline_event_id: int | None = None
    if missed_markets:
        _deadline_transition, deadline_event_id = store.observe_protection(
            BlindnessObservation(
                scope=scope,
                observation_id=(
                    "deadline:"
                    + scope
                    + ":"
                    + ",".join(
                        windows_by_market[market].key
                        for market in sorted(missed_markets)
                    )
                ),
                observed_at=completed_at,
                enabled_instruments=coverage.enabled_instruments,
                usable_instruments=coverage.usable_instruments,
                unusable_tickers=coverage.unusable_tickers,
                reason_codes=("expected_window_missing",),
                deadline_missed=True,
            ),
            delivery_status=incident_delivery_status,
        )
    observation = BlindnessObservation(
        scope=scope,
        observation_id=f"scan:{scope}:{started_at.isoformat()}",
        observed_at=completed_at,
        enabled_instruments=coverage.enabled_instruments,
        usable_instruments=coverage.usable_instruments,
        unusable_tickers=coverage.unusable_tickers,
        reason_codes=reason_codes,
        provider_degraded=bool(provider_failures),
        full_coverage_scan=(
            coverage.enabled_instruments > 0
            and coverage.usable_instruments == coverage.enabled_instruments
        ),
    )
    transition, event_id = store.observe_protection(
        observation,
        delivery_status=incident_delivery_status,
    )

    for market in (
        sorted(dict.fromkeys(enabled_markets)) if record_windows else ()
    ):
        market_reports = {
            ticker: report
            for ticker, report in reports.items()
            if ticker in enabled_tickers and report.market == market
        }
        configured_market_tickers = [
            ticker for ticker in enabled_tickers if market_by_ticker[ticker] == market
        ]
        market_coverage = summarize_instrument_coverage(
            market_reports, configured_market_tickers
        )
        market_failures = [
            item for item in provider_failures if item.get("market") == market
        ]
        window = windows_by_market[market]
        if (
            window.expected_at.astimezone(UTC)
            < protection_activated_at_by_market[market]
        ):
            continue
        good = (
            market_coverage.enabled_instruments > 0
            and market_coverage.usable_instruments
            == market_coverage.enabled_instruments
            and not market_failures
            and window.expected_at.astimezone(UTC)
            <= completed_at
            <= window.deadline_at.astimezone(UTC)
        )
        within_window = (
            window.expected_at.astimezone(UTC)
            <= completed_at
            <= window.deadline_at.astimezone(UTC)
        )
        window_status = "good" if good else ("pending" if within_window else "bad")
        store.record_protection_window(
            window.key,
            market,
            window.expected_at,
            window.deadline_at,
            window_status,
            actual_at=completed_at,
            last_success_at=completed_at if good else None,
            enabled_instruments=market_coverage.enabled_instruments,
            usable_instruments=market_coverage.usable_instruments,
            affected_tickers=market_coverage.unusable_tickers,
            reason_codes=_reliability_reason_codes(
                enabled=market_coverage.enabled_instruments,
                usable=market_coverage.usable_instruments,
                reports=market_reports,
                provider_failures=market_failures,
                workflow_failure_codes=workflow_failure_codes,
            ),
            now=completed_at,
        )
    new_event_ids = tuple(
        item
        for item in (deadline_event_id, event_id)
        if item is not None
    )
    return transition.snapshot, event_id or deadline_event_id, new_event_ids


def _synthetic_corrupt_protection(
    *,
    scope: str,
    observed_at: datetime,
    enabled_instruments: int,
    reason_code: str = "state_corrupt",
) -> dict[str, Any]:
    """Build a non-persisted RED receipt without mutating corrupt evidence."""

    if enabled_instruments <= 0:
        # Integrity failure is orthogonal to configuration.  This deliberately
        # is not a ProtectionSnapshot (whose correct zero-enabled state is
        # UNCONFIGURED); it is an explicit non-persisted RED overlay so corrupt
        # evidence can never be presented as harmless Gray onboarding.
        return {
            "scope": scope,
            "state": "BLIND",
            "color": "RED",
            "state_since": observed_at.isoformat(),
            "updated_at": observed_at.isoformat(),
            "coverage": {
                "enabled_instruments": 0,
                "usable_instruments": 0,
                "ratio": None,
                "unusable_tickers": [],
            },
            "reason_codes": [reason_code],
            "incident_id": None,
            "incident_started_at": observed_at.isoformat(),
            "blind_started_at": observed_at.isoformat(),
            "recovered_at": None,
            "last_success_at": None,
            "healthy_confirmations": 0,
            "recovery_has_full_scan": False,
            "last_observation_id": f"synthetic:{reason_code}",
            "integrity_overlay": True,
            "repair_required": True,
            "repair_guidance": "run explicit repair-state after backing up the DB",
            "persisted": False,
        }
    else:
        snapshot = transition_protection(
            None,
            BlindnessObservation(
                scope=scope,
                observation_id=f"synthetic:{reason_code}",
                observed_at=observed_at,
                enabled_instruments=enabled_instruments,
                usable_instruments=0,
                reason_codes=(reason_code,),
            ),
        ).snapshot
    return {
        **snapshot.model_dump(mode="json"),
        "repair_required": True,
        "repair_guidance": "run explicit repair-state after backing up the DB",
        "persisted": False,
    }


async def _ensure_trust_receipt(
    store: StateStore,
    settings: Settings,
    *,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
) -> tuple[bool, bool | None, str | None]:
    """Prove every configured mobile channel generation independently."""

    if _notification_error(settings) is not None:
        return False, None, None
    _record_mobile_delivery_modes(
        store,
        settings,
        mode="active",
        now=now,
    )
    # A UTC-day generation both expires stale proof and avoids resurrecting an
    # old outbox edge after disable/re-enable with the same credentials.
    delivery_states = store.delivery_states(not_after=now)
    trust_generation = ":".join(
        f"{channel}-{delivery_states[channel]['generation']}"
        for channel in configured_mobile_channels(settings)
    )
    trust_day = now.astimezone(UTC).date().isoformat()
    report = await deliver_mobile(
        store,
        business_key=f"trust:startup:{trust_day}:{trust_generation}",
        kind="trust",
        payload=TRUST_RECEIPT_MESSAGE,
        settings=settings,
        now=now,
        clock=clock or (lambda: now),
        telegram_sender=(
            (lambda: send_message(TRUST_RECEIPT_MESSAGE, settings=settings))
            if _telegram_configured(settings)
            else None
        ),
    )
    if not report.attempted and report.accepted:
        return False, None, None
    error = _mobile_report_error(report)
    return report.attempted, report.accepted, error


async def run_stock_scan(
    *,
    market: str | None = None,
    notify: bool = False,
    fixture_path: Path | None = None,
    include_disabled: bool = False,
    record_run: bool = True,
    provider_runtime: ProviderRuntime | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Execute one bounded stock scan and return a serialisable outcome."""

    runtime_clock = clock or (lambda: datetime.now(UTC))
    fixed_replay_clock = now is not None and clock is None
    started_at = now or runtime_clock()
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    started_at = started_at.astimezone(UTC)
    completed_at = started_at
    selected_market = _market(market)
    protection_scope_key = (
        f"market:{selected_market}" if selected_market else "global"
    )
    rules = load_rules_config()
    settings = get_settings()
    if notify:
        _require_notifications(settings)
        if include_disabled:
            raise ValueError("真实通知不能包含 enabled: false 的标的")
        if fixture_path is not None:
            raise ValueError("真实通知不能使用离线或合成 fixture")

    instruments = _selected_instruments(
        rules, selected_market, include_disabled=include_disabled
    )
    enabled_tickers = [
        ticker for ticker, instrument in instruments if instrument.enabled
    ]
    enabled_ticker_set = set(enabled_tickers)
    owns_runtime = provider_runtime is None
    runtime = provider_runtime or ProviderRuntime(
        ProviderRuntimeConfig.model_validate(
            rules.reliability.provider.model_dump(mode="python")
        )
    )
    store: StateStore | None = None
    protection_scope_record: dict[str, Any] | None = None
    protection_activated_at_by_market: dict[str, datetime] = {}
    protection_corruption: str | None = None
    provider_runtime_corrupt = False
    run_log_corrupt = False
    integrity_ledger_corrupt = False
    activation_sync_event_id: int | None = None
    active_watchdog_markets: set[str] = set()
    watchdog_controls_protection_delivery = False
    # Fixture replay is always state-free. Every real scan shares/restores the
    # provider runtime and advances protection evidence, even in PREVIEW mode.
    if fixture_path is None:
        store = StateStore(STATE_PATH)
        try:
            try:
                store.run_records(not_after=started_at)
            except CorruptProtectionStateError:
                run_log_corrupt = True
                protection_corruption = "state_corrupt"
                try:
                    store.observe_integrity_incident(
                        "global",
                        "run_log",
                        "state_corrupt",
                        delivery_status="pending" if notify else "suppressed",
                        now=started_at,
                    )
                except CorruptProtectionStateError:
                    integrity_ledger_corrupt = True
            try:
                persisted_runtime = (
                    store.load_provider_runtime_state(
                        strict=True,
                        not_after=started_at,
                    )
                    if owns_runtime
                    else None
                )
                if persisted_runtime is not None:
                    runtime.import_state(persisted_runtime)
            except (CorruptProtectionStateError, TypeError, ValueError):
                # Provider history is a separate integrity component.  Never
                # overwrite it automatically: keep Signal evaluation alive on
                # a fresh in-memory runtime while Silence fails closed until an
                # explicit quarantine repair resets the affected baselines.
                provider_runtime_corrupt = True
                try:
                    store.observe_integrity_incident(
                        "global",
                        "provider_runtime",
                        "state_corrupt",
                        delivery_status="pending" if notify else "suppressed",
                        now=started_at,
                    )
                except CorruptProtectionStateError:
                    integrity_ledger_corrupt = True
            configured_markets: list[str] = sorted(
                {
                    instrument.market
                    for instrument in rules.watchlist.values()
                    if instrument.enabled
                }
            )
            configured_instruments_by_market: Mapping[str, Sequence[str]] = {
                configured_market: tuple(
                    sorted(
                        ticker
                        for ticker, instrument in rules.watchlist.items()
                        if instrument.enabled
                        and instrument.market == configured_market
                    )
                )
                for configured_market in configured_markets
            }
            configured_contracts_by_market: Mapping[str, str] = {
                configured_market: protection_contract_version(
                    rules, configured_market
                )
                for configured_market in configured_markets
            }
            active_watchdog: list[dict[str, Any]] = []
            watchdog_ledger_corrupt = False
            try:
                active_watchdog = store.watchdog_incidents(
                    active_only=True,
                    not_after=started_at,
                )
            except CorruptProtectionStateError:
                # Validate the independent watchdog ledger before scope
                # mutation. Otherwise set_protection_scope would encounter
                # the same bad row and misclassify it as scope corruption,
                # leaving the actual component without a durable repair path.
                watchdog_ledger_corrupt = True
                protection_corruption = "state_corrupt"
                try:
                    store.observe_integrity_incident(
                        "global",
                        "watchdog_incidents",
                        "state_corrupt",
                        delivery_status="pending" if notify else "suppressed",
                        now=started_at,
                    )
                except CorruptProtectionStateError:
                    integrity_ledger_corrupt = True
            if watchdog_ledger_corrupt:
                # Preserve both the bad watchdog row and the last authenticated
                # scope. Signal evaluation may continue, but no scope mutation
                # or operational delivery can rely on this evidence.
                try:
                    scope_record = store.get_protection_scope(
                        "global", not_after=started_at
                    )
                except CorruptProtectionStateError:
                    scope_record = None
                    try:
                        store.observe_integrity_incident(
                            "global",
                            "protection_scope",
                            "state_corrupt",
                            delivery_status=(
                                "pending" if notify else "suppressed"
                            ),
                            now=started_at,
                        )
                    except CorruptProtectionStateError:
                        integrity_ledger_corrupt = True
            else:
                try:
                    scope_record = store.set_protection_scope(
                        configured_markets,
                        enabled_instruments_by_market=(
                            configured_instruments_by_market
                        ),
                        market_contract_hashes=configured_contracts_by_market,
                        now=started_at,
                    )
                except CorruptProtectionStateError:
                    # Scope evidence controls promises/deadlines, not the Signal
                    # Plane.  Keep it immutable and fail Silence closed while
                    # allowing independently fresh evidence to be evaluated.
                    protection_corruption = "state_corrupt"
                    try:
                        store.observe_integrity_incident(
                            "global",
                            "protection_scope",
                            "state_corrupt",
                            delivery_status=(
                                "pending" if notify else "suppressed"
                            ),
                            now=started_at,
                        )
                    except CorruptProtectionStateError:
                        integrity_ledger_corrupt = True
                    scope_record = None
            if scope_record is not None:
                protection_scope_record = scope_record
                protection_activated_at_by_market = {
                    market: datetime.fromisoformat(str(epoch)).astimezone(UTC)
                    for market, epoch in scope_record["market_epochs"].items()
                }
                if active_watchdog:
                    active_watchdog_markets.update(
                        active_watchdog[0]["payload"]["markets"]
                    )
            watchdog_controls_protection_delivery = bool(
                active_watchdog_markets
            ) and (
                selected_market is None
                or selected_market in active_watchdog_markets
            )
            if (
                notify
                and protection_corruption is None
                and not watchdog_controls_protection_delivery
            ):
                try:
                    activation_sync_event_id = (
                        store.ensure_current_incident_pending(
                            (
                                f"market:{selected_market}"
                                if selected_market
                                else "global"
                            ),
                            now=started_at,
                        )
                    )
                except CorruptProtectionStateError:
                    protection_corruption = "state_corrupt"
                    try:
                        store.observe_integrity_incident(
                            protection_scope_key,
                            "protection_state",
                            "state_corrupt",
                            delivery_status=(
                                "pending" if notify else "suppressed"
                            ),
                            now=started_at,
                        )
                    except CorruptProtectionStateError:
                        integrity_ledger_corrupt = True
            delivery_mode: Literal["active", "preview"] = (
                "active" if notify else "preview"
            )
            _record_mobile_delivery_modes(
                store,
                settings,
                mode=delivery_mode,
                now=started_at,
            )
            _record_delivery_state(
                store,
                settings,
                "heartbeat",
                mode=delivery_mode,
                now=started_at,
            )
        except BaseException:
            _persist_and_close_runtime(
                store,
                runtime,
                started_at,
                persist=not provider_runtime_corrupt,
                delivery_status="pending" if notify else "suppressed",
            )
            raise
    try:
        snapshots, errors, reports, raw_provider_failures = await _fetch_snapshots(
            instruments,
            fixture_path,
            rules=rules,
            provider_runtime=runtime,
            evaluated_at=started_at,
        )
        provider_failures_by_capability = {
            (
                item["provider"],
                item["operation"],
                item["market"],
                item["reason"],
            ): {key: value for key, value in item.items() if key != "ticker"}
            for item in raw_provider_failures
            if item.get("ticker") in enabled_ticker_set
        }
        provider_failures = list(provider_failures_by_capability.values())
    except BaseException:
        if store is not None:
            _persist_and_close_runtime(
                store,
                runtime,
                started_at,
                persist=not provider_runtime_corrupt,
                delivery_status="pending" if notify else "suppressed",
            )
        raise
    notification_errors: dict[str, str] = {}
    incident_notification_error: str | None = None
    incident_attempted = False
    incident_notified = False
    integrity_incident_attempted = 0
    integrity_incident_notified = 0
    integrity_notification_errors: dict[str, str] = {}
    telegram_probe_attempted = False
    telegram_probe_success: bool | None = None
    telegram_probe_error: str | None = None
    evaluation_failure_codes: list[str] = []
    evaluation_failure_codes_by_ticker: dict[str, list[str]] = {}
    snapshot_rules = _rules_snapshot(rules)
    results: list[dict[str, Any]] = []
    prepared_results: list[
        tuple[str, InstrumentConfig, dict[str, Any], dict[str, Any]]
    ] = []
    decision_reports: dict[str, ReliabilityReport] = {}
    notified = 0

    try:
        # Fetch-time freshness is only provisional.  Revalidate every fetched
        # snapshot at one post-gather decision boundary before any evaluation,
        # rendering, state claim or network delivery can occur.
        if fixture_path is None:
            decision_at = started_at if fixed_replay_clock else runtime_clock()
            if decision_at.tzinfo is None or decision_at.utcoffset() is None:
                raise ValueError("clock must return timezone-aware datetimes")
            decision_at = decision_at.astimezone(UTC)
            for ticker, instrument in instruments:
                snapshot = snapshots.get(ticker)
                prior_report = reports.get(ticker)
                if snapshot is None or prior_report is None:
                    continue
                try:
                    gated, refreshed_report = _revalidate_snapshot_for_decision(
                        snapshot,
                        prior_report,
                        instrument,
                        rules,
                        evaluated_at=decision_at,
                    )
                except Exception as exc:  # noqa: BLE001 - trust boundary
                    snapshots.pop(ticker, None)
                    reports.pop(ticker, None)
                    errors[ticker] = _error_code(exc)
                    evaluation_failure_codes.append("revalidation_failed")
                    evaluation_failure_codes_by_ticker.setdefault(ticker, []).append(
                        "revalidation_failed"
                    )
                else:
                    snapshots[ticker] = gated
                    reports[ticker] = refreshed_report

        # Data plane phase: finish all gate/evaluate/render work first.  Only
        # successfully completed evaluations contribute to trusted coverage.
        for ticker, instrument in instruments:
            snapshot = snapshots.get(ticker)
            if snapshot is None:
                continue
            try:
                result = evaluate(ticker, snapshot, snapshot_rules)
                result["market"] = instrument.market
                result["instrument_rules_version"] = _instrument_rules_version(
                    instrument, rules
                )
                result["currency"] = snapshot.get("currency", instrument.currency)
                result["as_of"] = snapshot.get("as_of", snapshot.get("retrieved_at"))
                result["provider"] = snapshot.get("provider")
                result["quality_issues"] = snapshot.get("quality_issues", [])
                result["relevant_evidence"] = _relevant_evidence(result, snapshot)
                if result["decision"] != EvaluationDecision.NONE.value:
                    result["message_preview"] = render_signal_alert(
                        _signal_payload(
                            ticker,
                            instrument,
                            result,
                            snapshot,
                            result["relevant_evidence"],
                        )
                    )
                result["notified"] = False
                results.append(result)
                prepared_results.append((ticker, instrument, result, snapshot))
                report = reports.get(ticker)
                if report is not None:
                    decision_reports[ticker] = report
            except Exception as exc:  # noqa: BLE001 - isolate one instrument
                errors[ticker] = _error_code(exc)
                evaluation_failure_codes.append("evaluation_failed")
                evaluation_failure_codes_by_ticker.setdefault(ticker, []).append(
                    "evaluation_failed"
                )

        data_completed_at = (
            started_at if fixed_replay_clock else runtime_clock()
        )
        if (
            data_completed_at.tzinfo is None
            or data_completed_at.utcoffset() is None
        ):
            raise ValueError("clock must return timezone-aware datetimes")
        data_completed_at = data_completed_at.astimezone(UTC)
        completed_at = data_completed_at
        fresh_data_coverage = summarize_instrument_coverage(
            reports, enabled_tickers
        )
        coverage = summarize_instrument_coverage(decision_reports, enabled_tickers)
        protected_evaluation_failure_codes = tuple(
            dict.fromkeys(
                failure_code
                for ticker in enabled_tickers
                for failure_code in evaluation_failure_codes_by_ticker.get(
                    ticker, ()
                )
            )
        )
        reliability_summary: dict[str, Any]
        if fixture_path is not None:
            reliability_summary = {
                "mode": "SYNTHETIC_FIXTURE",
                "freshness_claimed": False,
                "reason": "fixture evaluation bypasses wall-clock freshness",
            }
        else:
            reliability_summary = {
                **coverage.model_dump(mode="json"),
                "coverage_semantics": "trusted_decision_coverage",
                "fresh_data_coverage": fresh_data_coverage.model_dump(mode="json"),
                "trusted_decision_coverage": coverage.model_dump(mode="json"),
                "provider_capability_failures": provider_failures,
            }
            if selected_market is None:
                market_by_ticker = {
                    ticker: instrument.market
                    for ticker, instrument in instruments
                    if instrument.enabled
                }
                by_market: dict[str, Any] = {}
                for enabled_market in sorted(set(market_by_ticker.values())):
                    market_tickers = [
                        ticker
                        for ticker in enabled_tickers
                        if market_by_ticker[ticker] == enabled_market
                    ]
                    market_fresh = summarize_instrument_coverage(
                        {
                            ticker: report
                            for ticker, report in reports.items()
                            if ticker in market_tickers
                        },
                        market_tickers,
                    )
                    market_trusted = summarize_instrument_coverage(
                        {
                            ticker: report
                            for ticker, report in decision_reports.items()
                            if ticker in market_tickers
                        },
                        market_tickers,
                    )
                    by_market[enabled_market] = {
                        "selected": len(market_tickers),
                        "evaluated": sum(
                            ticker in decision_reports for ticker in market_tickers
                        ),
                        "fresh_data_coverage": market_fresh.model_dump(mode="json"),
                        "trusted_decision_coverage": market_trusted.model_dump(
                            mode="json"
                        ),
                    }
                if by_market:
                    reliability_summary["by_market"] = by_market
        protection: dict[str, Any] | None = None
        protection_event_id: int | None = activation_sync_event_id
        protection_event_ids: list[int] = (
            [activation_sync_event_id]
            if activation_sync_event_id is not None
            else []
        )
        if fixture_path is None and store is not None:
            protection_scope = protection_scope_key
            enabled_markets = sorted(
                {
                    instrument.market
                    for _ticker, instrument in instruments
                    if instrument.enabled
                }
            )
            protection_scopes_for_delivery = (
                []
                if watchdog_controls_protection_delivery
                else [protection_scope_key]
            )
            if protection_corruption is not None:
                protection = _synthetic_corrupt_protection(
                    scope=protection_scope,
                    observed_at=data_completed_at,
                    enabled_instruments=len(enabled_tickers),
                    reason_code=protection_corruption,
                )
            else:
                try:
                    (
                        protection_snapshot,
                        new_protection_event_id,
                        new_protection_event_ids,
                    ) = (
                        _record_protection_evidence(
                            store,
                            scope=protection_scope,
                            started_at=started_at,
                            completed_at=data_completed_at,
                            enabled_tickers=enabled_tickers,
                            reports=decision_reports,
                            provider_failures=provider_failures,
                            enabled_markets=enabled_markets,
                            market_by_ticker={
                                ticker: instrument.market
                                for ticker, instrument in instruments
                            },
                            protection_activated_at_by_market=(
                                protection_activated_at_by_market
                            ),
                            incident_delivery_status=(
                                "pending"
                                if notify
                                and not watchdog_controls_protection_delivery
                                else "suppressed"
                            ),
                            workflow_failure_codes=tuple(
                                protected_evaluation_failure_codes
                            ),
                            record_windows=selected_market is not None,
                        )
                    )
                    if new_protection_event_id is not None:
                        protection_event_id = new_protection_event_id
                    protection_event_ids.extend(
                        event_id
                        for event_id in new_protection_event_ids
                        if event_id not in protection_event_ids
                    )
                    if selected_market is None:
                        market_by_ticker = {
                            ticker: instrument.market
                            for ticker, instrument in instruments
                        }
                        for enabled_market in enabled_markets:
                            market_tickers = [
                                ticker
                                for ticker in enabled_tickers
                                if market_by_ticker[ticker] == enabled_market
                            ]
                            market_reports = {
                                ticker: report
                                for ticker, report in decision_reports.items()
                                if ticker in market_tickers
                            }
                            market_failures = [
                                item
                                for item in provider_failures
                                if item.get("market") == enabled_market
                            ]
                            market_workflow_failures = tuple(
                                dict.fromkeys(
                                    failure_code
                                    for ticker in market_tickers
                                    for failure_code in (
                                        evaluation_failure_codes_by_ticker.get(
                                            ticker, ()
                                        )
                                    )
                                )
                            )
                            (
                                _market_snapshot,
                                _market_event_id,
                                market_event_ids,
                            ) = (
                                _record_protection_evidence(
                                    store,
                                    scope=f"market:{enabled_market}",
                                    started_at=started_at,
                                    completed_at=data_completed_at,
                                    enabled_tickers=market_tickers,
                                    reports=market_reports,
                                    provider_failures=market_failures,
                                    enabled_markets=[enabled_market],
                                    market_by_ticker=market_by_ticker,
                                    protection_activated_at_by_market=(
                                        protection_activated_at_by_market
                                    ),
                                    incident_delivery_status=(
                                        "suppressed"
                                    ),
                                    workflow_failure_codes=(
                                        market_workflow_failures
                                    ),
                                )
                            )
                            protection_event_ids.extend(
                                event_id
                                for event_id in market_event_ids
                                if event_id not in protection_event_ids
                            )
                except ProtectionObservationCollisionError:
                    try:
                        collision_snapshot = store.load_protection_state(
                            protection_scope
                        )
                    except CorruptProtectionStateError:
                        collision_snapshot = None
                    protection = (
                        collision_snapshot.model_dump(mode="json")
                        if collision_snapshot is not None
                        else _synthetic_corrupt_protection(
                            scope=protection_scope,
                            observed_at=data_completed_at,
                            enabled_instruments=len(enabled_tickers),
                            reason_code="observation_id_collision",
                        )
                    )
                except CorruptProtectionStateError:
                    protection_corruption = "state_corrupt"
                    protection = _synthetic_corrupt_protection(
                        scope=protection_scope,
                        observed_at=data_completed_at,
                        enabled_instruments=len(enabled_tickers),
                    )
                    try:
                        store.observe_integrity_incident(
                            protection_scope,
                            "protection_state",
                            "state_corrupt",
                            delivery_status=(
                                "pending" if notify else "suppressed"
                            ),
                            now=data_completed_at,
                        )
                    except CorruptProtectionStateError:
                        integrity_ledger_corrupt = True
                else:
                    protection = protection_snapshot.model_dump(mode="json")

            if (
                protection_scope_record is not None
                and protection_corruption is None
            ):
                try:
                    reconciled_watchdog = _reconcile_watchdog_incident(
                        store,
                        rules,
                        protection_scope_record,
                        now=data_completed_at,
                        delivery_status=(
                            "pending" if notify else "suppressed"
                        ),
                    )
                    if reconciled_watchdog is not None:
                        # A late scan can create the immutable missed-window
                        # evidence and the aggregate outbox in this same
                        # workflow. The watchdog outbox exclusively owns that
                        # operational story; suppress all ordinary protection
                        # edges from this scan before any claim is attempted.
                        watchdog_controls_protection_delivery = True
                        protection_scopes_for_delivery = []
                        for event_id in protection_event_ids:
                            store.suppress_incident_notification(event_id)
                except CorruptProtectionStateError:
                    # Watchdog intent is an independent safety ledger. A bad
                    # row must fail Silence closed and raise one durable ops
                    # incident, but it can never block a fresh Signal Plane.
                    protection_corruption = "state_corrupt"
                    protection = _synthetic_corrupt_protection(
                        scope=protection_scope_key,
                        observed_at=data_completed_at,
                        enabled_instruments=len(enabled_tickers),
                    )
                    try:
                        store.observe_integrity_incident(
                            "global",
                            "watchdog_incidents",
                            "state_corrupt",
                            delivery_status=(
                                "pending" if notify else "suppressed"
                            ),
                            now=data_completed_at,
                        )
                    except CorruptProtectionStateError:
                        integrity_ledger_corrupt = True

        # Delivery plane phase: notification latency/failure is recorded
        # separately and can never rewrite the already captured data SLO.
        if store is not None:
            for ticker, instrument, result, snapshot in prepared_results:
                if not instrument.enabled:
                    continue
                if notify:
                    try:
                        was_notified = await _apply_notification_state(
                            store,
                            ticker=ticker,
                            instrument=instrument,
                            result=result,
                            snapshot=snapshot,
                            settings=settings,
                            now=data_completed_at,
                            record_delivery_state=False,
                        )
                        result["notified"] = was_notified
                        notified += int(was_notified)
                    except Exception as exc:  # noqa: BLE001 - delivery boundary
                        error = _error_code(exc)
                        result["notification_error"] = error
                        notification_errors[ticker] = error
                else:
                    _observe_signal_state(
                        store,
                        ticker=ticker,
                        instrument=instrument,
                        result=result,
                        snapshot=snapshot,
                    )
            if notify and protection_corruption is None:
                try:
                    for incident_scope in protection_scopes_for_delivery:
                        (
                            scope_attempted,
                            scope_notified,
                            scope_error,
                            delivered_event_id,
                        ) = await _deliver_current_incident(
                            store,
                            scope=incident_scope,
                            settings=settings,
                            clock=(
                                (lambda: started_at)
                                if fixed_replay_clock
                                else runtime_clock
                            ),
                            record_delivery_state=False,
                        )
                        incident_attempted = incident_attempted or scope_attempted
                        incident_notified = incident_notified or scope_notified
                        if (
                            incident_notification_error is None
                            and scope_error is not None
                        ):
                            incident_notification_error = scope_error
                        if delivered_event_id is not None:
                            protection_event_id = delivered_event_id
                            if delivered_event_id not in protection_event_ids:
                                protection_event_ids.append(delivered_event_id)
                except CorruptProtectionStateError:
                    protection_corruption = "state_corrupt"
                    incident_notification_error = "state_corrupt"
                    try:
                        store.observe_integrity_incident(
                            protection_scope_key,
                            "protection_event",
                            "state_corrupt",
                            delivery_status="pending",
                            now=data_completed_at,
                        )
                    except CorruptProtectionStateError:
                        integrity_ledger_corrupt = True
                    if protection is None or protection.get("state") != "BLIND":
                        protection = _synthetic_corrupt_protection(
                            scope=protection_scope_key,
                            observed_at=data_completed_at,
                            enabled_instruments=len(enabled_tickers),
                        )
            if notify and protection_corruption is None:
                (
                    watchdog_attempted,
                    watchdog_notified,
                    watchdog_error,
                    _watchdog_incident_id,
                ) = await _deliver_watchdog_incident(
                    store,
                    settings=settings,
                    clock=(
                        (lambda: started_at)
                        if fixed_replay_clock
                        else runtime_clock
                    ),
                    record_delivery_state=False,
                )
                incident_attempted = incident_attempted or watchdog_attempted
                incident_notified = incident_notified or watchdog_notified
                if (
                    incident_notification_error is None
                    and watchdog_error is not None
                ):
                    incident_notification_error = watchdog_error
                if watchdog_error == "state_corrupt":
                    protection_corruption = "state_corrupt"
                    if protection is None or protection.get("state") != "BLIND":
                        protection = _synthetic_corrupt_protection(
                            scope=protection_scope_key,
                            observed_at=data_completed_at,
                            enabled_instruments=len(enabled_tickers),
                        )
            if notify and not integrity_ledger_corrupt:
                try:
                    (
                        integrity_incident_attempted,
                        integrity_incident_notified,
                        integrity_notification_errors,
                        _integrity_event_ids,
                    ) = await _deliver_pending_integrity_incidents(
                        store,
                        scope=None,
                        settings=settings,
                        clock=(
                            (lambda: started_at)
                            if fixed_replay_clock
                            else runtime_clock
                        ),
                        record_delivery_state=False,
                    )
                except CorruptProtectionStateError:
                    integrity_ledger_corrupt = True
                    if protection is None or protection.get("state") != "BLIND":
                        protection = _synthetic_corrupt_protection(
                            scope=protection_scope_key,
                            observed_at=data_completed_at,
                            enabled_instruments=len(enabled_tickers),
                        )

        business_delivery_attempted = bool(
            notified
            or notification_errors
            or incident_attempted
            or integrity_incident_attempted
            or integrity_notification_errors
        )
        delivery_blocked = business_delivery_attempted or bool(
            incident_notification_error
            or integrity_notification_errors
            or protection_corruption
            or integrity_ledger_corrupt
        )
        if (
            notify
            and store is not None
            and enabled_tickers
            and not delivery_blocked
        ):
            (
                telegram_probe_attempted,
                telegram_probe_success,
                telegram_probe_error,
            ) = await _ensure_trust_receipt(
                store,
                settings,
                now=data_completed_at,
                clock=(
                    (lambda: started_at)
                    if fixed_replay_clock
                    else runtime_clock
                ),
            )

        if integrity_ledger_corrupt and (
            protection is None or protection.get("state") != "BLIND"
        ):
            protection = _synthetic_corrupt_protection(
                scope=protection_scope_key,
                observed_at=data_completed_at,
                enabled_instruments=len(enabled_tickers),
            )

        workflow_finished_at = (
            started_at if fixed_replay_clock else runtime_clock()
        )
        if (
            workflow_finished_at.tzinfo is None
            or workflow_finished_at.utcoffset() is None
        ):
            raise ValueError("clock must return timezone-aware datetimes")
        workflow_finished_at = workflow_finished_at.astimezone(UTC)
        completed_at = workflow_finished_at
        if notify and store is not None:
            _record_mobile_workflow_result(
                store,
                settings,
                started_at=started_at,
                completed_at=workflow_finished_at,
            )
        protected_errors = {
            ticker: error
            for ticker, error in errors.items()
            if ticker in enabled_ticker_set
        }
        protected_notification_errors = {
            ticker: error
            for ticker, error in notification_errors.items()
            if ticker in enabled_ticker_set
        }
        protected_evaluated = sum(
            instrument.enabled
            for _ticker, instrument, _result, _snapshot in prepared_results
        )
        protected_notified = sum(
            bool(result.get("notified"))
            for _ticker, instrument, result, _snapshot in prepared_results
            if instrument.enabled
        )
        failed = bool(
            protected_errors
            or protected_notification_errors
            or incident_notification_error
            or integrity_notification_errors
            or telegram_probe_error
            or run_log_corrupt
            or integrity_ledger_corrupt
        )
        status = (
            "success"
            if not failed
            else ("partial" if protected_evaluated else "error")
        )
        outcome = {
            "status": status,
            "market": selected_market or "ALL",
            "fixture": str(fixture_path) if fixture_path else None,
            "selected": len(instruments),
            "evaluated": len(results),
            "notified": notified,
            "results": results,
            "errors": errors,
            "notification_errors": notification_errors,
            "incident_notified": bool(
                incident_notified or integrity_incident_notified
            ),
            "incident_attempted": bool(
                incident_attempted or integrity_incident_attempted
            ),
            "incident_notification_error": incident_notification_error,
            "integrity_incident_notified": integrity_incident_notified,
            "integrity_incident_attempted": integrity_incident_attempted,
            "integrity_notification_errors": integrity_notification_errors,
            "integrity_ledger_error": (
                "state_corrupt"
                if (run_log_corrupt or integrity_ledger_corrupt)
                else None
            ),
            "telegram_probe_attempted": telegram_probe_attempted,
            "telegram_probe_success": telegram_probe_success,
            "telegram_probe_error": telegram_probe_error,
            "reliability": reliability_summary,
            "protection": protection,
            "protection_event_id": protection_event_id,
            "protection_event_ids": protection_event_ids,
        }
        if record_run and store is not None:
            store.record_run(
                f"stock-scan:{selected_market or 'ALL'}",
                status,
                {
                    "selected": len(enabled_tickers),
                    "evaluated": protected_evaluated,
                    "notified": protected_notified,
                    "error_tickers": sorted(protected_errors),
                    "notification_error_tickers": sorted(
                        protected_notification_errors
                    ),
                    "incident_attempted": incident_attempted,
                    "incident_notified": incident_notified,
                    "incident_notification_error": incident_notification_error,
                    "integrity_incident_attempted": integrity_incident_attempted,
                    "integrity_incident_notified": integrity_incident_notified,
                    "integrity_notification_errors": integrity_notification_errors,
                    "integrity_ledger_error": (
                        "state_corrupt"
                        if (run_log_corrupt or integrity_ledger_corrupt)
                        else None
                    ),
                    "telegram_probe_attempted": telegram_probe_attempted,
                    "telegram_probe_success": telegram_probe_success,
                    "telegram_probe_error": telegram_probe_error,
                    "protection_event_id": protection_event_id,
                    "protection_event_ids": protection_event_ids,
                    "reliability": reliability_summary,
                },
                started_at=started_at,
                finished_at=workflow_finished_at,
            )
        return outcome
    finally:
        if store is not None:
            _persist_and_close_runtime(
                store,
                runtime,
                completed_at,
                persist=not provider_runtime_corrupt,
                delivery_status="pending" if notify else "suppressed",
                raise_on_failure=(
                    not provider_runtime_corrupt and sys.exc_info()[0] is None
                ),
            )


def _news_fingerprint(article: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {
            "title": article.get("title"),
            "url": article.get("url"),
            "source": article.get("source"),
            "datetime": article.get("datetime"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


async def run_news_scan(
    *, notify: bool = False, record_run: bool = True
) -> dict[str, Any]:
    """Fetch enabled news sources, label matches, and optionally notify."""

    started_at = datetime.now(UTC)
    rules = load_rules_config()
    news_config = load_news_config()
    settings = get_settings()
    if notify:
        _require_notifications(settings)

    enabled = {
        ticker: instrument
        for ticker, instrument in rules.watchlist.items()
        if instrument.enabled
    }
    watchlist_names = {
        ticker: instrument.name for ticker, instrument in enabled.items()
    }
    enabled_stock_keywords = {
        ticker: keywords
        for ticker, keywords in news_config.stock_keywords.items()
        if ticker in enabled
    }
    macro_queries = [
        keyword for topic in news_config.macro_topics for keyword in topic.keywords[:2]
    ]
    articles = await asyncio.to_thread(
        fetch_all_news,
        enabled.keys(),
        macro_queries,
        news_config.sources,
        settings=settings,
    )
    alerts = await asyncio.to_thread(
        filter_news,
        articles,
        enabled_stock_keywords,
        news_config.macro_topics,
        watchlist_names,
        ai_filter=news_config.ai_filter,
        settings=settings,
    )

    store: StateStore | None = None
    if notify or record_run:
        store = StateStore(STATE_PATH)
    notified = 0
    notification_errors: list[str] = []
    try:
        if store is not None:
            delivery_mode: Literal["active", "preview"] = (
                "active" if notify else "preview"
            )
            delivery_started_at = datetime.now(UTC)
            _record_mobile_delivery_modes(
                store,
                settings,
                mode=delivery_mode,
                now=delivery_started_at,
            )
            _record_delivery_state(
                store,
                settings,
                "heartbeat",
                mode=delivery_mode,
                now=delivery_started_at,
            )
        if notify and store is not None:
            for article in alerts[:5]:
                fingerprint = _news_fingerprint(article)
                claim = store.claim_news_notification(fingerprint)
                if claim is None:
                    continue
                try:
                    delivered_at = datetime.now(UTC)
                    async def send_telegram_news() -> None:
                        await send_news_alert(article, settings=settings)

                    report = await deliver_mobile(
                        store,
                        business_key=_mobile_business_key("news", fingerprint),
                        kind="news",
                        payload=article,
                        settings=settings,
                        now=delivered_at,
                        telegram_sender=(
                            send_telegram_news
                            if _telegram_configured(settings)
                            else None
                        ),
                    )
                    error = _mobile_report_error(report)
                    if error is not None:
                        raise _MobileDeliveryRejected(error)
                    store.mark_news_notified(fingerprint, claim_token=claim)
                    notified += 1
                except Exception as exc:  # noqa: BLE001 - delivery boundary
                    try:
                        store.release_notification_claim(fingerprint, claim)
                    except Exception:  # noqa: BLE001 - lease expiry remains a fallback
                        logger.warning(
                            "释放新闻通知 claim 失败；将等待 lease 自动过期"
                        )
                    notification_errors.append(_error_code(exc))

        delivery_finished_at = datetime.now(UTC)
        if notify and store is not None:
            _record_mobile_workflow_result(
                store,
                settings,
                started_at=started_at,
                completed_at=delivery_finished_at,
            )
        status = "partial" if notification_errors else "success"
        outcome = {
            "status": status,
            "fetched": len(articles),
            "review_items": len(alerts),
            "notified": notified,
            "notification_errors": notification_errors,
            "alerts": alerts,
        }
        if record_run and store is not None:
            store.record_run(
                "news-scan",
                status,
                {
                    "fetched": len(articles),
                    "review_items": len(alerts),
                    "notified": notified,
                    "notification_failures": len(notification_errors),
                },
                started_at=started_at,
                finished_at=delivery_finished_at,
            )
        return outcome
    finally:
        if store is not None:
            store.close()


def _print_scan(outcome: Mapping[str, Any]) -> None:
    table = Table(title="规则核验结果")
    table.add_column("标的", style="bold")
    table.add_column("市场")
    table.add_column("价格", justify="right")
    table.add_column("状态")
    table.add_column("证据/质量")
    for result in outcome.get("results", []):
        evidence_count = len(result.get("relevant_evidence", []))
        issue_count = len(result.get("quality_issues", []))
        table.add_row(
            str(result.get("ticker", "—")),
            str(result.get("market", "—")),
            str(result.get("price", "—")),
            str(result.get("decision", "—")),
            f"{evidence_count} 条证据 / {issue_count} 个质量提示",
        )
    console.print(table)
    if not outcome.get("results"):
        message = (
            "默认配置全部禁用。"
            if outcome.get("selected", 0) == 0
            else "所选标的均未能完成核验，请查看错误。"
        )
        console.print(f"[yellow]没有可核验的标的。{message}[/yellow]")
    for result in outcome.get("results", []):
        evidence = result.get("relevant_evidence", [])
        if not evidence:
            continue
        detail = Table(
            title=(
                f"{result.get('ticker')} 规则证据 · 数据时间 {result.get('as_of', '—')}"
            )
        )
        detail.add_column("规则 ID")
        detail.add_column("实际值 / 阈值")
        detail.add_column("来源")
        for item in evidence[:6]:
            comparison = " ".join(
                str(value)
                for value in (
                    item.get("actual_value"),
                    item.get("operator"),
                    item.get("threshold"),
                    item.get("unit"),
                )
                if value is not None and value != ""
            )
            detail.add_row(
                str(item.get("rule_id", "—")),
                comparison,
                str(item.get("source", "—")),
            )
        console.print(detail)
    for ticker, error in outcome.get("errors", {}).items():
        console.print(f"[red]{ticker}：{error}[/red]")
    for ticker, error in outcome.get("notification_errors", {}).items():
        console.print(f"[red]{ticker} 通知失败（仍可重试）：{error}[/red]")
    console.print(
        f"状态={outcome.get('status')}，核验={outcome.get('evaluated', 0)}，"
        f"通知={outcome.get('notified', 0)}"
    )


def _print_news(outcome: Mapping[str, Any]) -> None:
    table = Table(title="新闻人工复核队列")
    table.add_column("标题")
    table.add_column("来源")
    table.add_column("AI 状态")
    for article in outcome.get("alerts", [])[:10]:
        table.add_row(
            str(article.get("title", "—")),
            str(article.get("source", "—")),
            str(article.get("ai_status", "—")),
        )
    console.print(table)
    console.print(
        f"抓取={outcome.get('fetched', 0)}，复核项={outcome.get('review_items', 0)}，"
        f"通知={outcome.get('notified', 0)}，"
        f"通知失败={len(outcome.get('notification_errors', []))}"
    )


def _json_dump(value: Any) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False, default=str))


def _handle_failure(exc: Exception, *, json_output: bool = False) -> None:
    if isinstance(exc, ValidationError):
        code = "configuration_invalid"
    elif isinstance(exc, json.JSONDecodeError):
        code = "json_invalid"
    elif isinstance(exc, (ValueError, TypeError)):
        code = "input_invalid"
    elif isinstance(exc, OSError):
        code = "local_io_error"
    else:
        code = "internal_error"
    # Exception messages, reprs and tracebacks may contain URLs, credentials,
    # response bodies or absolute paths.  Keep the CLI boundary low-cardinality.
    if json_output:
        _json_dump({"status": "error", "error_code": code})
    else:
        console.print(f"[red]失败：{code}[/red]")
    raise typer.Exit(code=1) from exc


def _backup_state_database(store: StateStore, *, now: datetime) -> Path:
    source = Path(STATE_PATH)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = source.with_name(f"{source.name}.backup-{stamp}.sqlite3")
    destination = sqlite3.connect(backup_path)
    try:
        store.connection.backup(destination)
    finally:
        destination.close()
    return backup_path


@app.command()
def validate() -> None:
    """校验规则、新闻配置与运行时开关；不联网、不写状态。"""

    try:
        rules = load_rules_config()
        news = load_news_config()
        settings = get_settings()
        enabled = sum(item.enabled for item in rules.watchlist.values())
        enabled_sources = [
            name
            for name in ("finnhub", "newsapi", "akshare")
            if getattr(news.sources, name).enabled
        ]
        console.print("[green]配置校验通过[/green]")
        console.print(
            f"标的 {len(rules.watchlist)} 个（启用 {enabled} 个）；"
            f"新闻源：{', '.join(enabled_sources) if enabled_sources else '全部禁用'}；"
            f"AI 筛选：{'启用' if news.ai_filter.enabled else '禁用'}；"
            f"外发通知：{'启用' if settings.notifications_enabled else '禁用'}；"
            f"Heartbeat：{'启用' if settings.heartbeat_enabled else '禁用'}"
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _handle_failure(exc)


@app.command()
def doctor(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="显示更详细的诊断日志。")
    ] = False,
) -> None:
    """运行本地健康检查；默认不访问任何外部网络。"""

    _configure_logging(verbose)
    try:
        rules = load_rules_config()
        news = load_news_config()
        settings = get_settings()
        sqlite_status = _doctor_sqlite_status(Path(STATE_PATH))
        now = datetime.now(UTC)
        table = Table(title="Alpha Guard 本地健康检查")
        table.add_column("检查项")
        table.add_column("结果")
        table.add_row("Python", sys.version.split()[0])
        table.add_row("规则配置", f"通过；{len(rules.watchlist)} 个标的")
        table.add_row("新闻配置", f"通过；{len(news.macro_topics)} 个主题")
        table.add_row("SQLite", sqlite_status)
        table.add_row("yfinance", _dependency_status("yfinance"))
        table.add_row(
            "AKShare",
            _dependency_status("akshare", enabled=news.sources.akshare.enabled),
        )
        table.add_row(
            "Anthropic SDK",
            _dependency_status("anthropic", enabled=news.ai_filter.enabled),
        )
        table.add_row(
            "Telegram SDK",
            _dependency_status("telegram", enabled=settings.notifications_enabled),
        )
        table.add_row(
            "通知",
            "就绪"
            if _notification_error(settings) is None
            else _notification_error(settings),
        )
        heartbeat_configured = bool(
            settings.heartbeat_enabled and settings.heartbeat_url is not None
        )
        table.add_row(
            "Heartbeat configured",
            "true" if heartbeat_configured else "false",
        )
        table.add_row("下次美股扫描", next_market_run("US", now).isoformat())
        table.add_row("下次港股扫描", next_market_run("HK", now).isoformat())
        table.add_row("下次新闻扫描", next_news_run(now).isoformat())
        console.print(table)
        console.print("[green]本地健康检查完成；未执行联网探测。[/green]")
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _handle_failure(exc)


@app.command("dry-run")
def dry_run(
    fixture: Annotated[
        Path,
        typer.Option(
            "--fixture",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="离线快照 JSON；默认使用仓库内合成样例。",
        ),
    ] = DEFAULT_FIXTURE_PATH,
    json_output: Annotated[
        bool, typer.Option("--json", help="输出机器可读 JSON。")
    ] = False,
) -> None:
    """用合成快照演练全规则链；不联网、不通知、不写状态。"""

    try:
        outcome = asyncio.run(
            run_stock_scan(
                fixture_path=fixture,
                include_disabled=True,
                notify=False,
                record_run=False,
            )
        )
        if json_output:
            _json_dump(outcome)
        else:
            console.print(
                "[yellow]离线演练：数据为合成样例，不代表真实市场行情。[/yellow]"
            )
            _print_scan(outcome)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _handle_failure(exc)


@app.command()
def scan(
    market: Annotated[
        str | None, typer.Option("--market", help="只扫描 US 或 HK 市场。")
    ] = None,
    notify: Annotated[
        bool,
        typer.Option(
            "--notify",
            help="允许外发通知；还必须配置 NOTIFICATIONS_ENABLED=true。",
        ),
    ] = False,
    include_disabled: Annotated[
        bool,
        typer.Option(
            "--include-disabled",
            help="预览已禁用标的；仍不会通知，不能与 --notify 同用。",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="输出机器可读 JSON。")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="显示更详细的日志。")
    ] = False,
) -> None:
    """从真实提供者抓取启用标的并核验规则；默认只预览。"""

    _configure_logging(verbose)
    try:
        if notify and include_disabled:
            raise ValueError("--include-disabled 不能与 --notify 同用")
        outcome = asyncio.run(
            run_stock_scan(
                market=market,
                notify=notify,
                include_disabled=include_disabled,
            )
        )
        if json_output:
            _json_dump(outcome)
        else:
            _print_scan(outcome)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _handle_failure(exc)


@app.command()
def news(
    notify: Annotated[
        bool,
        typer.Option(
            "--notify",
            help="允许外发通知；还必须配置 NOTIFICATIONS_ENABLED=true。",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="输出机器可读 JSON。")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="显示更详细的日志。")
    ] = False,
) -> None:
    """扫描已启用的新闻源并生成复核队列；默认只预览。"""

    _configure_logging(verbose)
    try:
        outcome = asyncio.run(run_news_scan(notify=notify))
        if json_output:
            _json_dump(outcome)
        else:
            _print_news(outcome)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _handle_failure(exc)


@app.command("repair-state")
def repair_state(
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="确认先备份数据库，再隔离损坏证据并写入安全状态。",
        ),
    ] = False,
    scope: Annotated[
        str, typer.Option("--scope", help="要修复的保护 scope。")
    ] = "global",
) -> None:
    """显式修复损坏的保护账本；绝不自动执行，也不回显原始载荷。"""

    if not confirm:
        console.print("[red]拒绝修复：必须显式传入 --confirm。[/red]")
        raise typer.Exit(code=2)
    try:
        if not STATE_PATH.exists():
            raise ValueError("状态数据库不存在")
        rules = load_rules_config()
        enabled_items = [
            (ticker, instrument)
            for ticker, instrument in rules.watchlist.items()
            if instrument.enabled
        ]
        configured_markets: list[str] = sorted(
            {instrument.market for _ticker, instrument in enabled_items}
        )
        configured_instruments_by_market: Mapping[str, Sequence[str]] = {
            market: tuple(
                sorted(
                    ticker
                    for ticker, instrument in enabled_items
                    if instrument.market == market
                )
            )
            for market in configured_markets
        }
        configured_contracts_by_market: Mapping[str, str] = {
            market: protection_contract_version(rules, market)
            for market in configured_markets
        }
        if scope in {"provider-runtime", "run-log"}:
            repaired_at = datetime.now(UTC)
            with StateStore(STATE_PATH) as store:
                if scope == "provider-runtime":
                    try:
                        store.load_provider_runtime_state(
                            strict=True,
                            not_after=repaired_at,
                        )
                    except CorruptProtectionStateError:
                        pass
                    else:
                        raise ValueError(
                            "未检测到可修复的 provider runtime 损坏"
                        )
                    runtime_repair_markets = configured_markets
                else:
                    runtime_repair_markets = list(
                        store.corrupt_run_log_affected_markets(now=repaired_at)
                    )
                backup_path = _backup_state_database(store, now=repaired_at)
                digests: tuple[str, ...]
                if scope == "provider-runtime":
                    digests = (
                        store.repair_corrupt_provider_runtime_state(
                            runtime_repair_markets,
                            now=repaired_at,
                        ),
                    )
                else:
                    # Recompute the responsibility inside the repair
                    # transaction; the preview above is informational only.
                    digests = store.repair_corrupt_run_log(
                        affected_markets=None,
                        now=repaired_at,
                    )
                receipt = build_reliability_cockpit(
                    store=store,
                    enabled_instruments={
                        ticker: instrument.market
                        for ticker, instrument in enabled_items
                    },
                    market_contract_hashes=configured_contracts_by_market,
                    current_delivery_fingerprints=(
                        delivery_config_fingerprints(get_settings())
                    ),
                    generated_at=repaired_at,
                )
            console.print("[green]可靠性账本显式修复完成。[/green]")
            console.print(f"backup={backup_path}")
            for index, digest in enumerate(digests, start=1):
                console.print(f"quarantine_sha256_{index}={digest}")
            console.print(
                f"state={receipt['state']} color={receipt['overall_color']}"
            )
            return
        repair_markets: list[str]
        if scope == "global":
            enabled = enabled_items
            repair_markets = sorted(
                {instrument.market for _ticker, instrument in enabled_items}
            )
        elif scope.startswith("market:"):
            scope_market = _market(scope.partition(":")[2])
            assert scope_market is not None
            scope = f"market:{scope_market}"
            enabled = [
                (ticker, instrument)
                for ticker, instrument in enabled_items
                if instrument.market == scope_market
            ]
            repair_markets = [scope_market] if enabled else []
        else:
            raise ValueError("scope 必须是 global、market:US 或 market:HK")
        enabled_count = len(enabled)
        repaired_at = datetime.now(UTC)
        with StateStore(STATE_PATH) as store:
            scope_corrupt = False
            state_corrupt = False
            event_corrupt = False
            try:
                store.get_protection_scope(scope)
            except CorruptProtectionStateError:
                scope_corrupt = True
            try:
                store.load_protection_state(scope)
            except CorruptProtectionStateError:
                state_corrupt = True
            if not state_corrupt:
                try:
                    store.pending_current_incident_event(scope)
                except CorruptProtectionStateError:
                    event_corrupt = True
            if not scope_corrupt and not state_corrupt and not event_corrupt:
                raise ValueError("未检测到可修复的保护账本损坏")

            backup_path = _backup_state_database(store, now=repaired_at)
            if scope_corrupt:
                _scope, digest, snapshot, _event_id = (
                    store.repair_corrupt_protection_scope(
                        repair_markets,
                        enabled_instruments=enabled_count,
                        enabled_instruments_by_market=(
                            {
                                market: configured_instruments_by_market[market]
                                for market in repair_markets
                            }
                        ),
                        market_contract_hashes={
                            market: configured_contracts_by_market[market]
                            for market in repair_markets
                        },
                        scope=scope,
                        now=repaired_at,
                    )
                )
                repair_kind = "scope"
            else:
                if state_corrupt:
                    snapshot, digest, _event_id = (
                        store.repair_corrupt_protection_state(
                            enabled_instruments=enabled_count,
                            scope=scope,
                            now=repaired_at,
                        )
                    )
                    repair_kind = "state"
                else:
                    digest, _event_id = store.repair_corrupt_protection_event(
                        scope=scope,
                        now=repaired_at,
                    )
                    current_snapshot = store.load_protection_state(scope)
                    assert current_snapshot is not None
                    snapshot = current_snapshot
                    repair_kind = "event"
        console.print("[green]保护账本显式修复完成。[/green]")
        console.print(f"backup={backup_path}")
        console.print(f"{repair_kind}_sha256={digest}")
        console.print(f"state={snapshot.state.value} color={snapshot.color}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _handle_failure(exc)


def _display_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    return str(value)


def _render_reliability_cockpit(receipt: Mapping[str, Any]) -> None:
    """Render only the stable Cockpit projection; never raw ledger payloads."""

    console.print(
        f"Reliability Cockpit：{receipt['state']} / {receipt['overall_color']}；"
        f"delivery={receipt['delivery_mode']}"
    )
    reasons = receipt.get("reason_codes", [])
    if reasons:
        console.print(f"原因码：{_display_value(reasons)}")

    recent_runs = list(receipt.get("recent_runs", ()))
    schedule_table = Table(title="1. 计划保护窗口：最后一次应跑与实际完成")
    schedule_table.add_column("市场")
    schedule_table.add_column("Expected")
    schedule_table.add_column("Deadline")
    schedule_table.add_column("Deadline state")
    schedule_table.add_column("最近完成")
    for item in receipt["schedule"]["markets"]:
        market = item["market"]
        completed = next(
            (
                run["finished_at"]
                for run in recent_runs
                if run.get("market") in {market, "ALL"}
            ),
            None,
        )
        schedule_table.add_row(
            market,
            item["expected_at"],
            item["deadline_at"],
            item["deadline_state"],
            _display_value(completed),
        )
    if not receipt["schedule"]["markets"]:
        schedule_table.add_row("—", "—", "—", "未激活", "—")
    console.print(schedule_table)

    silence = receipt["silence"]
    silence_table = Table(title="2. 可信沉默：数据新鲜度与决策覆盖")
    silence_table.add_column("平面")
    silence_table.add_column("状态")
    silence_table.add_column("可用/启用")
    silence_table.add_column("覆盖率")
    silence_table.add_column("影响范围")
    for label, projection in (
        ("Fresh data", silence["fresh_data"]),
        ("Trusted decision", silence["trusted_decision"]),
    ):
        silence_table.add_row(
            label,
            silence["state"] if projection.get("known") else "UNKNOWN",
            f"{_display_value(projection.get('usable'))}/"
            f"{_display_value(projection.get('enabled'))}",
            _display_value(projection.get("ratio")),
            _display_value(projection.get("affected", [])),
        )
    console.print(silence_table)
    console.print(
        "事故/盲区："
        f"state={silence['state']}；affected={_display_value(silence['affected'])}；"
        f"reasons={_display_value(reasons)}"
    )

    provider_table = Table(title="3. 提供者能力：可靠性与熔断状态")
    provider_table.add_column("Provider")
    provider_table.add_column("Operation")
    provider_table.add_column("Market")
    provider_table.add_column("Samples")
    provider_table.add_column("Wilson")
    provider_table.add_column("Circuit")
    provider_table.add_column("Reasons")
    capabilities = receipt["providers"]["capabilities"]
    for item in capabilities:
        provider_table.add_row(
            item["provider"],
            item["operation"],
            item["market"],
            _display_value(item["sample_count"]),
            _display_value(item["wilson_lower_bound"]),
            item["circuit_state"],
            _display_value(item["reasons"]),
        )
    if not capabilities:
        provider_table.add_row("—", "—", "—", "0", "—", "—", "—")
    console.print(provider_table)

    delivery_table = Table(title="4. 交付守望：Telegram 与外部 watcher")
    delivery_table.add_column("Channel")
    delivery_table.add_column("Configured")
    delivery_table.add_column("Mode")
    delivery_table.add_column("Last attempt")
    delivery_table.add_column("Last success")
    delivery_table.add_column("Success")
    delivery_table.add_column("Error code")
    for channel, item in receipt["delivery"].items():
        delivery_table.add_row(
            channel,
            _display_value(item["configured"]),
            item["mode"],
            _display_value(item["last_attempt_at"]),
            _display_value(item["last_success_at"]),
            _display_value(item["success"]),
            _display_value(item["error_code"]),
        )
    console.print(delivery_table)

    run_table = Table(title="最近运行时间线")
    run_table.add_column("完成时间")
    run_table.add_column("任务")
    run_table.add_column("状态")
    run_table.add_column("已选/已评估/已通知")
    for run in recent_runs:
        run_table.add_row(
            run["finished_at"],
            run["job"],
            run["status"],
            f"{run['selected']}/{run['evaluated']}/{run['notified']}",
        )
    if not recent_runs:
        run_table.add_row("—", "—", "暂无运行记录", "—")
    console.print(run_table)


@app.command()
def status(
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=100, help="显示最近多少条运行记录。")
    ] = 20,
    json_output: Annotated[
        bool, typer.Option("--json", help="输出机器可读 JSON。")
    ] = False,
) -> None:
    """查看离线 Reliability Cockpit 与可信沉默凭据。"""

    try:
        rules = load_rules_config()
        enabled, contracts = _cockpit_configuration(rules)
        delivery_fingerprints = delivery_config_fingerprints(get_settings())
        generated_at = datetime.now(UTC)
        if STATE_PATH.exists():
            try:
                with StateStore(STATE_PATH) as store:
                    receipt = build_reliability_cockpit(
                        store=store,
                        enabled_instruments=enabled,
                        market_contract_hashes=contracts,
                        current_delivery_fingerprints=delivery_fingerprints,
                        generated_at=generated_at,
                        recent_run_limit=limit,
                    )
            except (sqlite3.Error, CorruptProtectionStateError, OSError):
                receipt = build_corrupt_reliability_cockpit(
                    enabled_instruments=enabled,
                    generated_at=generated_at,
                )
        else:
            receipt = build_reliability_cockpit(
                store=None,
                enabled_instruments=enabled,
                market_contract_hashes=contracts,
                current_delivery_fingerprints=delivery_fingerprints,
                generated_at=generated_at,
                recent_run_limit=limit,
            )
        receipt = dict(receipt)
        receipt["recent_runs"] = list(receipt.get("recent_runs", ()))[:limit]
        if json_output:
            _json_dump(receipt)
            return
        _render_reliability_cockpit(receipt)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _handle_failure(exc, json_output=json_output)


async def _send_daily_summary(
    market: str,
    *,
    notify: bool,
    provider_runtime: ProviderRuntime | None = None,
) -> None:
    if not notify:
        return
    rules = load_rules_config()
    enabled = _selected_instruments(rules, market, include_disabled=False)
    if not enabled:
        return
    settings = get_settings()
    _require_notifications(settings)
    try:
        outcome = await run_stock_scan(
            market=market,
            notify=False,
            provider_runtime=provider_runtime,
        )
    except Exception:
        restored_at = datetime.now(UTC)
        with StateStore(STATE_PATH) as store:
            _record_mobile_delivery_modes(
                store,
                settings,
                mode="active",
                now=restored_at,
            )
            _record_delivery_state(
                store,
                settings,
                "heartbeat",
                mode="active",
                now=restored_at,
            )
        raise
    lines = [f"{market} 每日规则核验摘要（未执行交易）"]
    lines.extend(
        f"{item['ticker']}: {item['decision']} @ {item.get('price', '—')}"
        for item in outcome["results"]
    )
    completed_at = datetime.now(UTC)
    message = "\n".join(lines)
    with StateStore(STATE_PATH) as store:
        _record_mobile_delivery_modes(
            store,
            settings,
            mode="active",
            now=completed_at,
        )
        report = await deliver_mobile(
            store,
            business_key=_mobile_business_key(
                "summary", market, completed_at.date().isoformat()
            ),
            kind="summary",
            payload=message,
            settings=settings,
            now=completed_at,
            telegram_sender=(
                (lambda: send_message(message, settings=settings))
                if _telegram_configured(settings)
                else None
            ),
        )
        _record_delivery_state(
            store,
            settings,
            "heartbeat",
            mode="active",
            now=completed_at,
        )
    error = _mobile_report_error(report)
    if error is not None:
        _raise_mobile_delivery(error)


async def _initialise_scheduler_delivery(*, notify: bool) -> None:
    """Prove ACTIVE mobile delivery at startup without fetching market data."""

    if not notify:
        return
    settings = get_settings()
    _require_notifications(settings)
    started_at = datetime.now(UTC)
    with StateStore(STATE_PATH) as store:
        _record_delivery_state(
            store,
            settings,
            "heartbeat",
            mode="active",
            now=started_at,
        )
        await _ensure_trust_receipt(
            store,
            settings,
            now=started_at,
            clock=lambda: datetime.now(UTC),
        )


async def _run_trust_watchdog(
    *,
    notify: bool,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Finalize all due promises, deliver incidents, then ping dead-man.

    The ordering is the safety contract: a startup or interval tick can never
    send a green heartbeat before overdue scan evidence has been finalized.
    """

    observed_at = clock()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("watchdog clock must return a timezone-aware datetime")
    observed_at = observed_at.astimezone(UTC)
    rules = load_rules_config()
    settings = get_settings()
    delivery_status: Literal["pending", "suppressed"] = (
        "pending" if notify else "suppressed"
    )
    result: dict[str, Any] = {
        "finalized": [],
        "incident_attempted": False,
        "heartbeat_attempted": False,
        "heartbeat_success": None,
        "error_code": None,
    }
    try:
        with StateStore(STATE_PATH) as store:
            finalized = _finalize_due_protection_windows(
                store,
                rules,
                now=observed_at,
                delivery_status=delivery_status,
            )
            result["finalized"] = list(finalized["finalized"])
            if notify:
                _record_mobile_delivery_modes(
                    store,
                    settings,
                    mode="active",
                    now=observed_at,
                )
                _record_delivery_state(
                    store,
                    settings,
                    "heartbeat",
                    mode="active",
                    now=observed_at,
                )
                attempted, _notified, error, _incident_id = (
                    await _deliver_watchdog_incident(
                        store,
                        settings=settings,
                        clock=clock,
                        record_delivery_state=False,
                    )
                )
                result["incident_attempted"] = attempted
                if error is not None:
                    result["error_code"] = error
                (
                    integrity_attempted,
                    _integrity_notified,
                    integrity_errors,
                    _event_ids,
                ) = await _deliver_pending_integrity_incidents(
                    store,
                    scope=None,
                    settings=settings,
                    clock=clock,
                    record_delivery_state=False,
                )
                result["incident_attempted"] = bool(
                    attempted or integrity_attempted
                )
                if result["error_code"] is None and integrity_errors:
                    result["error_code"] = next(iter(integrity_errors.values()))

                decision_at = clock().astimezone(UTC)
                enabled, contracts = _cockpit_configuration(rules)
                receipt = build_reliability_cockpit(
                    store=store,
                    enabled_instruments=enabled,
                    market_contract_hashes=contracts,
                    current_delivery_fingerprints=(
                        delivery_config_fingerprints(settings)
                    ),
                    generated_at=decision_at,
                    recent_run_limit=1,
                )
                unsafe_reasons = tuple(receipt.get("reason_codes", ()))
                local_integrity_safe = not any(
                    reason == "state_corrupt"
                    or "corrupt" in str(reason)
                    or str(reason).startswith("integrity_")
                    for reason in unsafe_reasons
                ) and receipt.get("watchdog", {}).get("active") is not True
                # Business delivery has precedence. A trust probe is only
                # allowed after all local ledgers have passed strict reading.
                if (
                    not result["incident_attempted"]
                    and result["error_code"] is None
                    and local_integrity_safe
                ):
                    await _ensure_trust_receipt(
                        store,
                        settings,
                        now=decision_at,
                        clock=clock,
                    )
                    decision_at = clock().astimezone(UTC)
                    receipt = build_reliability_cockpit(
                        store=store,
                        enabled_instruments=enabled,
                        market_contract_hashes=contracts,
                        current_delivery_fingerprints=(
                            delivery_config_fingerprints(settings)
                        ),
                        generated_at=decision_at,
                        recent_run_limit=1,
                    )
                if heartbeat_eligible(receipt):
                    heartbeat_result = await asyncio.to_thread(
                        ping_heartbeat,
                        settings,
                    )
                    completed_at = clock().astimezone(UTC)
                    result["heartbeat_attempted"] = True
                    result["heartbeat_success"] = heartbeat_result.success
                    _record_delivery_state(
                        store,
                        settings,
                        "heartbeat",
                        mode="active",
                        attempted_at=completed_at,
                        success=heartbeat_result.success,
                        error_code=heartbeat_result.error_code,
                        now=completed_at,
                    )
    except CorruptProtectionStateError:
        result["error_code"] = "state_corrupt"
    return result


def _shared_provider_runtime(rules: RulesConfig) -> ProviderRuntime | None:
    """Hydrate one scheduler runtime once; corrupt history stays quarantined."""

    runtime = ProviderRuntime(
        ProviderRuntimeConfig.model_validate(
            rules.reliability.provider.model_dump(mode="python")
        )
    )
    if not STATE_PATH.exists():
        return runtime
    try:
        with StateStore(STATE_PATH) as store:
            persisted = store.load_provider_runtime_state(
                strict=True,
                not_after=datetime.now(UTC),
            )
        if persisted is not None:
            runtime.import_state(persisted)
    except (CorruptProtectionStateError, TypeError, ValueError):
        # Let the normal scan path keep Signal evaluation alive while it
        # records a durable integrity incident and refuses to overwrite the
        # corrupt row. Returning None intentionally selects that safe path.
        return None
    return runtime


def _scheduler_event_listener(event: object) -> None:
    """Sanitize APScheduler failures before they reach application logs."""

    raw_code = getattr(event, "code", None)
    code = raw_code if isinstance(raw_code, int) else -1
    taxonomy = {
        2**13: "job_error",
        2**14: "job_missed",
        2**16: "job_max_instances",
    }.get(code, "job_event")
    job_id = getattr(event, "job_id", None)
    allowed_job = (
        job_id
        if isinstance(job_id, str)
        and job_id
        in {
            "market-scan:US",
            "market-scan:HK",
            "daily-summary:US",
            "daily-summary:HK",
            "news-scan",
            "trust-watchdog",
        }
        else "unknown_job"
    )
    logger.error("scheduler_event=%s job=%s", taxonomy, allowed_job)


async def _serve_scheduler(
    *,
    notify: bool,
    on_ready: Callable[[], None] | None = None,
) -> None:
    rules = load_rules_config()
    shared_runtime = _shared_provider_runtime(rules)
    # Run once synchronously before scheduling any trust receipt or heartbeat.
    await _run_trust_watchdog(notify=notify)
    scheduler = build_scheduler(
        lambda market: run_stock_scan(
            market=market,
            notify=notify,
            provider_runtime=shared_runtime,
        ),
        lambda: run_news_scan(notify=notify),
        lambda market: _send_daily_summary(
            market,
            notify=notify,
            provider_runtime=shared_runtime,
        ),
        trust_watchdog=lambda: _run_trust_watchdog(notify=notify),
        event_listener=_scheduler_event_listener,
    )
    scheduler.start()
    if on_ready is not None:
        on_ready()
    console.print(
        "[green]调度器已启动。[/green] "
        + ("通知模式已开启。" if notify else "当前为只读预览模式，不外发通知。")
    )
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


@app.command()
def run(
    notify: Annotated[
        bool,
        typer.Option(
            "--notify",
            help="允许调度任务外发通知；还必须配置环境开关与凭证。",
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="显示更详细的日志。")
    ] = False,
) -> None:
    """启动时区与交易日感知的长期调度器。"""

    _configure_logging(verbose)
    try:
        load_rules_config()
        load_news_config()
        if notify:
            _require_notifications(get_settings())
        asyncio.run(_serve_scheduler(notify=notify))
    except KeyboardInterrupt:
        console.print("调度器已停止。")
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _handle_failure(exc)


if __name__ == "__main__":
    app()
