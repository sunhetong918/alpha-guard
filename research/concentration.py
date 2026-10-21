"""Read-only explainable concentration over stored pricing observations.

市值 = 数量 × 同币种有效报价；统一基准币种时按记录了来源、时间、方向与
新鲜度的汇率换算。缺价、缺汇率或过期只列为不可估/过期，绝不用 0 填充，
也不输出貌似完整的组合百分比。取价与取汇率是显式动作，带调用预算；
读取与计算完全离线。首版只有单股与币种维度，不含行业分类。
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Self

from .models import ResearchInstrument, aware
from .portfolio import PortfolioStore

CURRENCIES = frozenset({"USD", "HKD", "CNY", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "SGD"})
QUOTE_MAX_AGE = timedelta(days=7)
FX_MAX_AGE = timedelta(days=7)
# Explicit pricing budget: at most this many quote/FX calls per refresh, and a
# wall-clock deadline; the read path never touches the network.
MAX_QUOTES = 60
MAX_FX = 9
TOTAL_SECONDS = 90.0
PER_REQUEST_SECONDS = 10.0
MAX_TOP_N = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_concentration_settings (
 id INTEGER PRIMARY KEY CHECK (id = 1), payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_concentration_quotes (
 instrument_key TEXT PRIMARY KEY, price REAL NOT NULL, currency TEXT NOT NULL,
 as_of TEXT, observed_at TEXT NOT NULL, provider TEXT, quality_issues TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_concentration_fx (
 pair TEXT PRIMARY KEY, rate REAL NOT NULL, direction TEXT NOT NULL,
 as_of TEXT, observed_at TEXT NOT NULL, provider TEXT
);
"""
DEFAULT_SETTINGS: dict[str, Any] = {
    "base_currency": None, "single_threshold": None,
    "currency_threshold": None, "top_n": 5,
}


class ConcentrationError(ValueError):
    pass


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return aware(parsed) if parsed.tzinfo else None


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _percent(value: Decimal, denominator: Decimal) -> str:
    if denominator <= 0:
        return "0.00"
    return format((value / denominator * Decimal(100)).quantize(Decimal("0.01")), "f")


def _fresh(record: Mapping[str, Any], *, budget: timedelta, now: datetime) -> str | None:
    stamp = _instant(record.get("as_of"))
    if stamp is None:
        return "time_unknown"
    if stamp > now + timedelta(minutes=1):
        return "time_in_future"
    if now - stamp > budget:
        return "stale"
    return None


def parse_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConcentrationError("concentration_settings_invalid")
    unknown = set(payload) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ConcentrationError("concentration_settings_invalid")
    base = payload.get("base_currency", DEFAULT_SETTINGS["base_currency"])
    if base is not None and (not isinstance(base, str) or base not in CURRENCIES):
        raise ConcentrationError("concentration_settings_invalid")
    result: dict[str, Any] = {"base_currency": base}
    for name in ("single_threshold", "currency_threshold"):
        value = payload.get(name, DEFAULT_SETTINGS[name])
        if value is None:
            result[name] = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 100:
            raise ConcentrationError("concentration_settings_invalid")
        else:
            result[name] = float(value)
    top_n = payload.get("top_n", DEFAULT_SETTINGS["top_n"])
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= MAX_TOP_N:
        raise ConcentrationError("concentration_settings_invalid")
    result["top_n"] = top_n
    return result


def compute(positions: list[dict[str, Any]], quotes: Mapping[str, Mapping[str, Any]],
            fx: Mapping[str, Mapping[str, Any]], *, settings: Mapping[str, Any],
            now: datetime) -> dict[str, Any]:
    """Explainable weights over estimated positions only; no zero-fill ever."""
    now = aware(now)
    if now.tzinfo is None:
        raise ValueError("aware_clock_required")
    options = parse_settings(dict(settings))
    base = options["base_currency"]
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for position in sorted(positions, key=lambda item: str(item.get("instrument"))):
        key = str(position.get("instrument"))
        quantity = _decimal(position.get("quantity"))
        quote = quotes.get(key)
        reason = None
        if quantity is None:
            reason = "quantity_invalid"
        elif not isinstance(quote, Mapping) or quote.get("price") is None:
            reason = "quote_missing"
        else:
            reason = _fresh(quote, budget=QUOTE_MAX_AGE, now=now)
        if reason is None and isinstance(quote, Mapping):
            currency = str(quote.get("currency") or "")
            if currency not in CURRENCIES:
                reason = "quote_currency_invalid"
        row: dict[str, Any] = {"instrument": key, "quantity": str(position.get("quantity")),
                               "status": "estimated" if reason is None else reason}
        if reason is None and quantity is not None and isinstance(quote, Mapping):
            price = _decimal(quote["price"])
            if price is None or price <= 0:
                row["status"] = "quote_invalid"
                reason = row["status"]
            else:
                currency = str(quote["currency"])
                native = quantity * price
                row.update(price=format(price.normalize(), "f"), currency=currency,
                           value_native=format(native.normalize(), "f"))
                if base is not None and currency != base:
                    pair = f"{currency}{base}"
                    rate_record = fx.get(pair)
                    rate = _decimal(rate_record.get("rate")) if isinstance(rate_record, Mapping) else None
                    fx_state = (None if isinstance(rate_record, Mapping) and rate and rate > 0 else "fx_missing")
                    if fx_state is None:
                        assert isinstance(rate_record, Mapping)
                        fx_state = _fresh(rate_record, budget=FX_MAX_AGE, now=now)
                    if fx_state is None and rate is not None:
                        row.update(value_base=format((native * rate).normalize(), "f"),
                                   fx_pair=pair, fx_rate=format(rate.normalize(), "f"),
                                   fx_as_of=rate_record.get("as_of") if isinstance(rate_record, Mapping) else None)
                    else:
                        assert fx_state is not None
                        row["status"] = fx_state
                        reason = fx_state
                elif base is not None:
                    row["value_base"] = row["value_native"]
        if reason is not None:
            missing.append({"instrument": key, "reason": reason})
        rows.append(row)
    field = "value_base" if base is not None else "value_native"
    estimated = [row for row in rows if row["status"] == "estimated"]
    denominator = sum((Decimal(row[field]) for row in estimated), Decimal(0))
    for row in estimated:
        row["weight"] = _percent(Decimal(row[field]), denominator)
    ranked = sorted(estimated, key=lambda row: Decimal(row[field]), reverse=True)
    currencies: dict[str, Decimal] = {}
    for row in estimated:
        currencies[row["currency"]] = currencies.get(row["currency"], Decimal(0)) + Decimal(row[field])
    currency_rows = [{"currency": code, "value": format(value.normalize(), "f"),
                      "weight": _percent(value, denominator)}
                     for code, value in sorted(currencies.items(), key=lambda item: (-item[1], item[0]))]
    breaches: list[dict[str, str]] = []
    scope = "estimated_only"
    for row in ranked:
        limit = options["single_threshold"]
        if limit is not None and Decimal(row["weight"]) > Decimal(str(limit)):
            breaches.append({"kind": "single", "instrument": row["instrument"],
                             "weight": row["weight"], "threshold": str(limit)})
    limit = options["currency_threshold"]
    if limit is not None:
        breaches.extend({"kind": "currency", "instrument": row["currency"],
                         "weight": row["weight"], "threshold": str(limit)}
                        for row in currency_rows if Decimal(row["weight"]) > Decimal(str(limit)))
    return {"denominator_scope": scope, "base_currency": base,
            "denominator_field": field,
            "estimated_total": format(denominator.normalize(), "f"),
            "estimated_count": len(estimated), "position_count": len(rows),
            "positions": rows, "top": [{"instrument": row["instrument"], "weight": row["weight"]}
                                       for row in ranked[: options["top_n"]]],
            "currencies": currency_rows, "currencies_note": "按报价币种构成统计，不代表完整经济风险敞口。",
            "missing": missing, "breaches": breaches,
            "quote_max_age_days": QUOTE_MAX_AGE.days, "fx_max_age_days": FX_MAX_AGE.days,
            "computed_at": now.isoformat()}


class ConcentrationStore(PortfolioStore):
    """Owns observations and settings; concentration never mutates snapshots."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self.ensure_feature_schema(SCHEMA, ("research_concentration_settings",
                                           "research_concentration_quotes",
                                           "research_concentration_fx"))

    def __enter__(self) -> Self:
        return self

    def settings(self) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM research_concentration_settings WHERE id=1").fetchone()
        return parse_settings(json.loads(row[0])) if row else dict(DEFAULT_SETTINGS)

    def save_options(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        options = parse_settings(dict(payload))
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "INSERT INTO research_concentration_settings(id,payload_json) VALUES(1,?) "
                    "ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json",
                    (json.dumps(options, ensure_ascii=False),))
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return options

    def quotes(self) -> dict[str, dict[str, Any]]:
        return {row["instrument_key"]: dict(row) for row in self._connection.execute(
            "SELECT instrument_key, price, currency, as_of, observed_at, provider, quality_issues "
            "FROM research_concentration_quotes")}

    def fx_rates(self) -> dict[str, dict[str, Any]]:
        return {row["pair"]: dict(row) for row in self._connection.execute(
            "SELECT pair, rate, direction, as_of, observed_at, provider FROM research_concentration_fx")}

    def _store_quote(self, instrument_key: str, quote: Mapping[str, Any]) -> None:
        self._connection.execute(
            "INSERT INTO research_concentration_quotes VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(instrument_key) DO UPDATE SET price=excluded.price, currency=excluded.currency, "
            "as_of=excluded.as_of, observed_at=excluded.observed_at, provider=excluded.provider, "
            "quality_issues=excluded.quality_issues",
            (instrument_key, float(quote["price"]), str(quote["currency"]), quote.get("as_of"),
             quote.get("observed_at"), quote.get("provider"),
             json.dumps(list(quote.get("quality_issues") or []), ensure_ascii=False)))

    def _store_fx(self, pair: str, rate: float, record: Mapping[str, Any]) -> None:
        base_code, quote_code = pair[:3], pair[3:]
        direction = f"1 {base_code} = <rate> {quote_code}"
        self._connection.execute(
            "INSERT INTO research_concentration_fx VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(pair) DO UPDATE SET rate=excluded.rate, direction=excluded.direction, "
            "as_of=excluded.as_of, observed_at=excluded.observed_at, provider=excluded.provider",
            (pair, rate, direction, record.get("as_of"), record.get("observed_at"), record.get("provider")))

    def refresh_pricing(self, account: str, *, now: datetime | None = None,
                        cancelled: Callable[[], bool] | None = None,
                        fetch: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
        """Explicit online pricing under a hard budget; failures stay per item."""
        now = aware(now or datetime.now(UTC))
        snapshot = self.view(account)
        positions = list(snapshot.get("positions") or [])
        options = self.settings()
        base = options["base_currency"]
        if fetch is None:
            from .market import fetch_market_context

            def fetch(instrument: dict[str, Any]) -> dict[str, Any]:
                return fetch_market_context(instrument, now=now, cancelled=cancelled,
                                            timeout_seconds=PER_REQUEST_SECONDS)
        deadline = time.monotonic() + TOTAL_SECONDS
        stored_quotes, stored_fx, failed = 0, 0, []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for index, position in enumerate(positions[:MAX_QUOTES]):
                    if cancelled is not None and cancelled():
                        raise ConcentrationError("cancelled")
                    if time.monotonic() > deadline:
                        failed.append({"item": str(position.get("instrument")), "reason": "budget_exhausted"})
                        continue
                    market, _, ticker = str(position.get("instrument")).partition(":")
                    try:
                        if market not in ("US", "HK", "CN"):
                            raise ValueError("invalid market")
                        instrument = ResearchInstrument(market=market, ticker=ticker)  # type: ignore[arg-type]
                    except ValueError:
                        failed.append({"item": str(position.get("instrument")), "reason": "instrument_invalid"})
                        continue
                    try:
                        context = fetch(instrument.model_dump(mode="json"))
                    except Exception:
                        context = {"price": None, "quality_issues": ["market_data_unavailable"]}
                    price = context.get("price")
                    if isinstance(price, (int, float)) and price > 0 and context.get("currency"):
                        self._store_quote(str(position.get("instrument")),
                                          {"price": price, "currency": str(context["currency"]).upper(),
                                           "as_of": context.get("as_of") or context.get("source_as_of"),
                                           "observed_at": context.get("observed_at") or now.isoformat(),
                                           "provider": context.get("provider"),
                                           "quality_issues": context.get("quality_issues")})
                        stored_quotes += 1
                    else:
                        issues = list(context.get("quality_issues") or ["market_data_unavailable"])
                        failed.append({"item": str(position.get("instrument")), "reason": issues[0]})
                if base is not None:
                    needed: set[str] = set()
                    for row in self.quotes().values():
                        code = str(row.get("currency"))
                        if code in CURRENCIES and code != base:
                            needed.add(f"{code}{base}")
                    for pair in sorted(needed)[:MAX_FX]:
                        if cancelled is not None and cancelled():
                            raise ConcentrationError("cancelled")
                        if time.monotonic() > deadline:
                            failed.append({"item": pair, "reason": "budget_exhausted"})
                            continue
                        ticker = f"{pair[:3]}{pair[3:]}=X"
                        try:
                            context = fetch({"market": "US", "ticker": ticker})
                        except Exception:
                            context = {"price": None}
                        rate = context.get("price")
                        if isinstance(rate, (int, float)) and rate > 0:
                            self._store_fx(pair, float(rate), {
                                "as_of": context.get("as_of") or context.get("source_as_of"),
                                "observed_at": context.get("observed_at") or now.isoformat(),
                                "provider": context.get("provider")})
                            stored_fx += 1
                        else:
                            failed.append({"item": pair, "reason": "fx_unavailable"})
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        if len(positions) > MAX_QUOTES:
            failed.extend({"item": str(row.get("instrument")), "reason": "budget_exhausted"}
                          for row in positions[MAX_QUOTES:])
        return {"status": "completed", "stored_quotes": stored_quotes, "stored_fx": stored_fx,
                "failed": failed, "budget": {"max_quotes": MAX_QUOTES, "max_fx": MAX_FX,
                              "total_seconds": TOTAL_SECONDS, "per_request_seconds": PER_REQUEST_SECONDS},
                "note": "取价是显式联网动作；失败项按原因保留为不可估，读取与计算不再联网。"}

    def concentration(self, account: str, *, now: datetime | None = None) -> dict[str, Any]:
        now = aware(now or datetime.now(UTC))
        snapshot = self.view(account)
        positions = list(snapshot.get("positions") or [])
        result = compute(positions, self.quotes(), self.fx_rates(),
                         settings=self.settings(), now=now)
        result["account"] = snapshot.get("account")
        result["snapshot_id"] = snapshot.get("snapshot_id")
        result["snapshot_date"] = snapshot.get("snapshot_date")
        result["quote_count"] = len(self.quotes())
        result["fx_count"] = len(self.fx_rates())
        return result


def execute_concentration(database: Path, command: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    shapes: dict[str, tuple[set[str], set[str]]] = {"concentration_get": ({"account"}, set()),
              "concentration_settings": ({"settings"}, set()),
              "concentration_price": ({"account"}, set())}
    if command not in shapes:
        raise ValueError("concentration_command_invalid")
    required, optional = shapes[command]
    if not required <= fields.keys() or fields.keys() - required - optional:
        raise ValueError("concentration_fields_invalid")
    with ConcentrationStore(database) as store:
        if command == "concentration_get":
            return store.concentration(fields["account"])
        if command == "concentration_settings":
            options = store.save_options(fields["settings"])
            return {"status": "saved", "settings": options}
        return {"status": "saved", "receipt": store.refresh_pricing(fields["account"]),
                "concentration": store.concentration(fields["account"])}
