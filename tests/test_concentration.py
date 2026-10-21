from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from research.concentration import (
    ConcentrationError,
    ConcentrationStore,
    compute,
    execute_concentration,
    parse_settings,
)
from research.portfolio import FIELDS

HEADER = ",".join(FIELDS) + "\n"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _store(tmp_path, rows="US,AAPL,10,100,USD\nHK,00700,100,,\n", account="测试账户"):
    store = ConcentrationStore(tmp_path / "state.db")
    fields = {"account": account, "snapshot_date": "2026-10-20", "source_label": "h.csv",
              "csv_text": HEADER + rows, "mapping": dict(zip(FIELDS, FIELDS)),
              "full_snapshot": True, "confirmed_instruments": ["US:AAPL", "HK:00700", "US:MSFT"]}
    preview = store.preview(fields)
    store.commit({**fields, "expected_revision": preview["revision"],
                  "preview_token": preview["preview_token"]})
    return store


def _quote(price, currency, *, age_seconds=3600, provider="test"):
    stamp = (NOW - timedelta(seconds=age_seconds)).isoformat()
    return {"price": price, "currency": currency, "as_of": stamp,
            "observed_at": stamp, "provider": provider, "quality_issues": []}


def _fx(rate, *, age_seconds=3600, provider="test"):
    stamp = (NOW - timedelta(seconds=age_seconds)).isoformat()
    return {"rate": rate, "direction": "1 HKD = <rate> USD", "as_of": stamp,
            "observed_at": stamp, "provider": provider}


POSITIONS = [{"instrument": "US:AAPL", "quantity": "10", "unit_cost": None, "cost_currency": None},
             {"instrument": "HK:00700", "quantity": "100", "unit_cost": None, "cost_currency": None}]


def test_missing_fx_keeps_position_unestimated_and_never_zero_fills():
    """Spec scenario: 港美混合持仓缺少一组汇率 → 组合估值不完整，不输出貌似完整占比。"""
    result = compute(POSITIONS, {"US:AAPL": _quote(200, "USD"), "HK:00700": _quote(300, "HKD")},
                     {}, settings={"base_currency": "USD", "top_n": 5}, now=NOW)
    assert result["denominator_scope"] == "estimated_only"
    assert result["estimated_count"] == 1 and result["position_count"] == 2
    assert {"instrument": "HK:00700", "reason": "fx_missing"} in result["missing"]
    by_key = {row["instrument"]: row for row in result["positions"]}
    assert by_key["HK:00700"].get("value_base") is None
    assert by_key["US:AAPL"]["weight"] == "100.00"
    assert Decimal(result["estimated_total"]) == Decimal(2000)


def test_full_estimation_converts_via_fx_with_direction_recorded():
    result = compute(POSITIONS, {"US:AAPL": _quote(200, "USD"), "HK:00700": _quote(300, "HKD")},
                     {"HKDUSD": _fx(0.128)}, settings={"base_currency": "USD", "top_n": 2}, now=NOW)
    assert result["missing"] == [] and result["estimated_count"] == 2
    by_key = {row["instrument"]: row for row in result["positions"]}
    assert Decimal(by_key["HK:00700"]["value_base"]) == Decimal("3840.0")
    assert Decimal(by_key["US:AAPL"]["value_base"]) == Decimal(2000)
    weights = sum(Decimal(row["weight"]) for row in result["positions"])
    assert Decimal("99.99") <= weights <= Decimal("100.01")
    assert [item["instrument"] for item in result["top"]] == ["HK:00700", "US:AAPL"]


def test_native_currencies_without_base_and_threshold_breaches():
    settings = {"base_currency": None, "single_threshold": 60, "currency_threshold": 70, "top_n": 1}
    result = compute(POSITIONS, {"US:AAPL": _quote(200, "USD"), "HK:00700": _quote(300, "HKD")},
                     {}, settings=settings, now=NOW)
    by_key = {row["instrument"]: row for row in result["positions"]}
    assert "value_base" not in by_key["US:AAPL"]
    assert by_key["US:AAPL"]["weight"] == "6.25" and by_key["HK:00700"]["weight"] == "93.75"
    singles = [b for b in result["breaches"] if b["kind"] == "single"]
    assert [(b["instrument"], b["weight"]) for b in singles] == [("HK:00700", "93.75")]
    currency = [b for b in result["breaches"] if b["kind"] == "currency"]
    assert [(b["instrument"], b["weight"]) for b in currency] == [("HKD", "93.75")]
    assert len(result["top"]) == 1 and result["currencies_note"]


def test_stale_or_timeless_quotes_stay_unestimated():
    quotes = {"US:AAPL": _quote(200, "USD", age_seconds=8 * 24 * 3600),
              "HK:00700": _quote(300, "HKD", age_seconds=3600)}
    quotes["HK:00700"]["as_of"] = None
    result = compute(POSITIONS, quotes, {}, settings={}, now=NOW)
    reasons = {item["instrument"]: item["reason"] for item in result["missing"]}
    assert reasons == {"US:AAPL": "stale", "HK:00700": "time_unknown"}
    assert result["estimated_total"] == "0"


def test_settings_validation_and_store_roundtrip(tmp_path):
    for bad in ({"base_currency": "BTC"}, {"single_threshold": 0}, {"single_threshold": 100},
                {"currency_threshold": "x"}, {"top_n": 0}, {"extra": 1}):
        with pytest.raises(ConcentrationError):
            parse_settings(bad)
    store = _store(tmp_path)
    saved = store.save_options({"base_currency": "USD", "single_threshold": 30, "top_n": 3})
    assert saved == {"base_currency": "USD", "single_threshold": 30.0,
                     "currency_threshold": None, "top_n": 3}
    assert store.settings()["base_currency"] == "USD"


def test_refresh_pricing_stores_bounded_quotes_and_fx(tmp_path):
    store = _store(tmp_path)

    def fetch(instrument):
        ticker = instrument["ticker"]
        if ticker == "AAPL":
            return {"price": 205.5, "currency": "USD", "as_of": NOW.isoformat(),
                    "observed_at": NOW.isoformat(), "provider": "stub", "quality_issues": []}
        if ticker == "00700":
            return {"price": 310.0, "currency": "HKD", "as_of": NOW.isoformat(),
                    "observed_at": NOW.isoformat(), "provider": "stub", "quality_issues": []}
        if ticker == "HKDUSD=X":
            return {"price": 0.13, "currency": "USD", "as_of": NOW.isoformat(),
                    "observed_at": NOW.isoformat(), "provider": "stub", "quality_issues": []}
        return {"price": None, "quality_issues": ["market_data_unavailable"]}

    store.save_options({"base_currency": "USD"})
    receipt = store.refresh_pricing("测试账户", now=NOW, fetch=fetch)
    assert receipt["status"] == "completed"
    assert receipt["stored_quotes"] == 2 and receipt["stored_fx"] == 1
    stored_fx = store.fx_rates()["HKDUSD"]
    assert stored_fx["rate"] == 0.13 and stored_fx["direction"] == "1 HKD = <rate> USD"
    result = store.concentration("测试账户", now=NOW)
    assert result["estimated_count"] == 2 and result["missing"] == []


def test_refresh_budget_and_failures_are_per_item(tmp_path):
    rows = "US,AAPL,10,,\nUS,MSFT,10,,\n"
    store = _store(tmp_path, rows=rows)
    store.save_options({"base_currency": None})

    def fetch(_instrument):
        return {"price": None, "quality_issues": ["market_data_unavailable"]}

    receipt = store.refresh_pricing("测试账户", now=NOW, fetch=fetch)
    assert receipt["stored_quotes"] == 0
    assert {item["reason"] for item in receipt["failed"]} == {"market_data_unavailable"}
    assert store.concentration("测试账户", now=NOW)["estimated_count"] == 0


def test_execute_concentration_command_shapes(tmp_path):
    store = _store(tmp_path)
    path = tmp_path / "state.db"
    store._connection.close()
    with pytest.raises(ValueError):
        execute_concentration(path, "concentration_get", {})
    with pytest.raises(ValueError):
        execute_concentration(path, "concentration_bad", {"account": "x"})
    result = execute_concentration(path, "concentration_get", {"account": "测试账户"})
    assert result["position_count"] == 2 and result["estimated_count"] == 0
