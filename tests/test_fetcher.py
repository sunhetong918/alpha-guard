from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from data import fetcher
from data.futu_provider import FutuQuote
from reliability import (
    CacheState,
    FieldFreshnessPolicy,
    FreshnessContext,
    ProviderRuntime,
    ProviderRuntimeConfig,
    ProviderUnavailableError,
)
from signals.engine import evaluate


class FakeHistory(dict):
    def __init__(self, highs=(120.0, 130.0), lows=(80.0, 90.0)):
        super().__init__(High=list(highs), Low=list(lows))
        self.index = [datetime(2026, 8, 8, tzinfo=UTC)]

    @property
    def empty(self):
        return not bool(self.get("High") or self.get("Low"))


class FakeTicker:
    def __init__(self, info, history=None):
        self.info = info
        self._history = history if history is not None else FakeHistory()

    def history(self, period, timeout=10):
        assert period == "1y"
        assert 0 < timeout <= 120
        return self._history


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def _complete_info(**overrides):
    info = {
        "longName": "Example Inc.",
        "currentPrice": 100.0,
        "trailingPE": 20.0,
        "priceToBook": 3.0,
        "returnOnEquity": 0.20,
        "marketCap": 1_000_000,
        "fiftyTwoWeekHigh": 130.0,
        "fiftyTwoWeekLow": 80.0,
        "dividendYield": 0.02,
        "debtToEquity": 25.0,
        "freeCashflow": 500_000,
        "revenueGrowth": 0.10,
        "earningsGrowth": 0.12,
        "currency": "USD",
        "regularMarketTime": 1_786_300_000,
    }
    info.update(overrides)
    return info


def test_hk_symbol_is_canonical_and_fundamentals_stay_on_yfinance(monkeypatch):
    calls = []
    info = _complete_info(
        longName="Tencent from Yahoo",
        currentPrice=319.0,
        trailingPE="25.5",
        priceToBook="4.5",
        returnOnEquity=0.238,
        currency="HKD",
    )

    def ticker_factory(symbol):
        calls.append(symbol)
        return FakeTicker(info)

    monkeypatch.setattr(fetcher, "yf", SimpleNamespace(Ticker=ticker_factory))
    monkeypatch.setattr(
        fetcher,
        "ak",
        SimpleNamespace(
            stock_hk_spot_em=lambda: FakeFrame(
                [
                    {
                        "代码": "00700",
                        "名称": "腾讯控股",
                        "最新价": "320.50",
                        "市盈率": 1.0,
                        "市净率": 0.1,
                        "更新时间": "2026-08-10T09:30:00+08:00",
                    }
                ]
            )
        ),
    )

    snapshot = fetcher.get_hk_stock("00700")

    assert calls == ["0700.HK"]
    assert fetcher.canonical_hk_symbol("00700") == "0700.HK"
    assert snapshot["ticker"] == "00700"
    assert snapshot["symbol"] == "0700.HK"
    assert snapshot["name"] == "腾讯控股"
    assert snapshot["price"] == 320.5
    assert snapshot["pe_ttm"] == 25.5
    assert snapshot["pb"] == 4.5
    assert snapshot["roe"] == 23.8
    assert snapshot["provider"] == "akshare+yfinance"
    assert snapshot["source"]["price"] == "akshare"
    assert snapshot["source"]["pe_ttm"] == "yfinance"
    assert snapshot["currency"] == "HKD"
    assert snapshot["retrieved_at"]
    assert snapshot["as_of"] == "2026-08-10T09:30:00+08:00"
    assert snapshot["field_metadata"]["price"]["provider"] == "akshare"
    assert snapshot["field_metadata"]["price"]["time_basis"] == "source_event"
    assert snapshot["field_metadata"]["pe_ttm"]["source_as_of"] is None
    assert snapshot["field_metadata"]["pe_ttm"]["time_basis"] == "observed_only"
    assert isinstance(snapshot["quality_issues"], list)


def test_us_snapshot_normalizes_ratio_roe_and_rejects_nonfinite_values(monkeypatch):
    info = _complete_info(
        currentPrice=float("nan"),
        regularMarketPrice="189.25",
        trailingPE=float("inf"),
        returnOnEquity=0.1875,
    )
    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda symbol: FakeTicker(info)),
    )

    snapshot = fetcher.get_us_stock("aapl")

    assert snapshot["ticker"] == "AAPL"
    assert snapshot["price"] == 189.25
    assert snapshot["pe_ttm"] is None
    assert snapshot["roe"] == 18.75
    assert snapshot["provider"] == "yfinance"
    assert snapshot["source"]["roe"] == "yfinance"
    assert snapshot["field_metadata"]["price"]["source_as_of"]
    assert snapshot["field_metadata"]["roe"]["source_as_of"] is None
    assert snapshot["field_metadata"]["roe"]["timestamp_confidence"] == "observed_only"
    assert "pe_ttm:missing_or_non_finite" in snapshot["quality_issues"]


def test_us_snapshot_prefers_futu_price_and_keeps_yfinance_fundamentals(
    monkeypatch,
) -> None:
    info = _complete_info(
        currentPrice=190.0,
        trailingPE=31.5,
        priceToBook=45.0,
        returnOnEquity=0.42,
    )
    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda _symbol: FakeTicker(info)),
    )
    monkeypatch.setattr("data.futu_provider.quote_available", lambda: True)
    monkeypatch.setattr(
        "data.futu_provider.fetch_quote",
        lambda ticker, **_kwargs: (
            FutuQuote(
                price=231.5,
                name="Apple from Futu",
                source_as_of="2026-08-20T13:30:00+00:00",
                observed_at="2026-08-20T13:30:01+00:00",
            ),
            (),
        ),
    )

    snapshot = fetcher.get_us_stock("aapl")

    assert snapshot["ticker"] == "AAPL"
    assert snapshot["price"] == 231.5
    assert snapshot["name"] == "Apple from Futu"
    assert snapshot["pe_ttm"] == 31.5
    assert snapshot["roe"] == 42.0
    assert snapshot["provider"] == "futu+yfinance"
    assert snapshot["source"]["price"] == "futu"
    assert snapshot["source"]["pe_ttm"] == "yfinance"
    assert snapshot["field_metadata"]["price"]["source_as_of"] == (
        "2026-08-20T13:30:00+00:00"
    )


def test_calc_roe_uses_percentage_points_for_both_paths():
    assert fetcher._calc_roe({"returnOnEquity": 0.25}) == 25.0
    assert fetcher._calc_roe({"returnOnEquity": "23.5%"}) == 23.5
    assert (
        fetcher._calc_roe(
            {
                "netIncomeToCommon": 25,
                "bookValue": 5,
                "sharesOutstanding": 20,
                "returnOnEquity": 0.99,
            }
        )
        == 25.0
    )
    assert fetcher._calc_roe({"returnOnEquity": float("nan")}) is None


def test_hk_quote_failure_falls_back_to_yfinance_with_quality_issue(monkeypatch):
    info = _complete_info(currentPrice="42.5", currency="HKD")
    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda symbol: FakeTicker(info)),
    )
    monkeypatch.setattr(
        fetcher,
        "ak",
        SimpleNamespace(stock_hk_spot_em=lambda: FakeFrame([])),
    )

    snapshot = fetcher.get_hk_stock("00005")

    assert snapshot["symbol"] == "0005.HK"
    assert snapshot["price"] == 42.5
    assert snapshot["source"]["price"] == "yfinance"
    assert snapshot["provider"] == "yfinance"
    assert "akshare:quote_not_found" in snapshot["quality_issues"]


def test_stale_futu_quote_falls_back_to_fresh_akshare_price(monkeypatch) -> None:
    info = _complete_info(currentPrice=99.0, currency="HKD")
    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda _symbol: FakeTicker(info)),
    )
    monkeypatch.setattr("data.futu_provider.quote_available", lambda: True)
    monkeypatch.setattr(
        "data.futu_provider.fetch_quote",
        lambda ticker, **_kwargs: (
            FutuQuote(
                price=120.0,
                name="stale",
                source_as_of="2026-08-20T01:20:00+00:00",
                observed_at="2026-08-20T01:20:01+00:00",
                cache_state=CacheState.STALE_IF_ERROR,
                usable_for_signal=False,
            ),
            (),
        ),
    )
    monkeypatch.setattr(
        fetcher,
        "ak",
        SimpleNamespace(
            stock_hk_spot_em=lambda: FakeFrame(
                [
                    {
                        "代码": "00700",
                        "名称": "腾讯控股",
                        "最新价": "121",
                        "更新时间": "2026-08-20T09:30:00+08:00",
                    }
                ]
            )
        ),
    )

    snapshot = fetcher.get_hk_stock("00700")

    assert snapshot["price"] == 121.0
    assert snapshot["source"]["price"] == "akshare"
    assert "futu:stale_or_unusable" in snapshot["quality_issues"]


def test_futu_quote_without_exchange_time_falls_back_instead_of_blocking(
    monkeypatch,
) -> None:
    info = _complete_info(currentPrice=99.0, currency="HKD")
    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda _symbol: FakeTicker(info)),
    )
    monkeypatch.setattr("data.futu_provider.quote_available", lambda: True)
    monkeypatch.setattr(
        "data.futu_provider.fetch_quote",
        lambda ticker, **_kwargs: (
            FutuQuote(
                price=120.0,
                name="timestamp missing",
                source_as_of=None,
                observed_at="2026-08-20T01:30:01+00:00",
            ),
            (),
        ),
    )
    monkeypatch.setattr(
        fetcher,
        "ak",
        SimpleNamespace(
            stock_hk_spot_em=lambda: FakeFrame(
                [
                    {
                        "代码": "00700",
                        "名称": "腾讯控股",
                        "最新价": "121",
                        "更新时间": "2026-08-20T09:30:00+08:00",
                    }
                ]
            )
        ),
    )

    snapshot = fetcher.get_hk_stock("00700")

    assert snapshot["price"] == 121.0
    assert snapshot["source"]["price"] == "akshare"
    assert "futu:timestamp_unavailable" in snapshot["quality_issues"]


def test_futu_price_remains_usable_when_yfinance_info_fails(monkeypatch) -> None:
    class _PriceOnlyTicker:
        @property
        def info(self):
            raise ConnectionError("Yahoo unavailable")

        def history(self, period, timeout=10):
            return FakeHistory()

    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda _symbol: _PriceOnlyTicker()),
    )
    monkeypatch.setattr("data.futu_provider.quote_available", lambda: True)
    monkeypatch.setattr(
        "data.futu_provider.fetch_quote",
        lambda ticker, **_kwargs: (
            FutuQuote(
                price=231.5,
                name="Apple",
                source_as_of="2026-08-20T13:59:50+00:00",
                observed_at="2026-08-20T13:59:51+00:00",
                cache_state=CacheState.MISS,
                usable_for_signal=True,
            ),
            (),
        ),
    )
    context = FreshnessContext(
        evaluated_at=datetime(2026, 8, 20, 14, 0, tzinfo=UTC),
        market_phase="open",
    )

    snapshot = fetcher.get_stock(
        "AAPL",
        "US",
        required_fields={"price"},
        freshness_policies={"price": PRICE_POLICY},
        freshness_context=context,
    )

    assert snapshot["price"] == 231.5
    assert snapshot["pe_ttm"] is None
    assert snapshot["source"]["price"] == "futu"
    assert snapshot["reliability"]["fields"]["price"]["usable_for_signal"] is True
    assert "yfinance:info_unavailable" in snapshot["quality_issues"]


def test_akshare_price_remains_usable_when_yfinance_info_fails(monkeypatch) -> None:
    now = datetime(2026, 8, 20, 1, 30, 10, tzinfo=UTC)

    class _PriceOnlyTicker:
        @property
        def info(self):
            raise ConnectionError("Yahoo unavailable")

        def history(self, period, timeout=10):
            return FakeHistory()

    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda _symbol: _PriceOnlyTicker()),
    )
    monkeypatch.setattr("data.futu_provider.quote_available", lambda: False)
    monkeypatch.setattr(
        fetcher,
        "ak",
        SimpleNamespace(
            stock_hk_spot_em=lambda: FakeFrame(
                [
                    {
                        "代码": "00700",
                        "名称": "腾讯控股",
                        "最新价": "121",
                        "更新时间": "2026-08-20T09:30:00+08:00",
                    }
                ]
            )
        ),
    )
    monkeypatch.setattr(fetcher, "_utc_now_iso", lambda: now.isoformat())

    snapshot = fetcher.get_stock(
        "00700",
        "HK",
        required_fields={"price"},
        freshness_policies={"price": PRICE_POLICY},
        freshness_context=FreshnessContext(
            evaluated_at=now,
            market_phase="open",
        ),
    )

    assert snapshot["price"] == 121.0
    assert snapshot["source"]["price"] == "akshare"
    assert snapshot["reliability"]["fields"]["price"]["usable_for_signal"] is True
    assert "yfinance:info_unavailable" in snapshot["quality_issues"]


def test_future_dated_futu_quote_retries_with_yfinance_price(monkeypatch) -> None:
    now = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    info = _complete_info(
        currentPrice=230.0,
        regularMarketTime=int((now - timedelta(seconds=10)).timestamp()),
    )
    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda _symbol: FakeTicker(info)),
    )
    monkeypatch.setattr(fetcher, "_utc_now_iso", lambda: now.isoformat())
    monkeypatch.setattr("data.futu_provider.quote_available", lambda: True)
    monkeypatch.setattr(
        "data.futu_provider.fetch_quote",
        lambda ticker, **_kwargs: (
            FutuQuote(
                price=999.0,
                name="future",
                source_as_of=(now + timedelta(minutes=10)).isoformat(),
                observed_at=now.isoformat(),
            ),
            (),
        ),
    )

    snapshot = fetcher.get_stock(
        "AAPL",
        "US",
        required_fields={"price"},
        freshness_policies={"price": PRICE_POLICY},
        freshness_context=FreshnessContext(
            evaluated_at=now,
            market_phase="open",
        ),
    )

    assert snapshot["price"] == 230.0
    assert snapshot["source"]["price"] == "yfinance"
    assert "futu:quote_failed_freshness" in snapshot["quality_issues"]


def test_stale_akshare_quote_retries_with_fresh_yfinance_price(monkeypatch) -> None:
    now = datetime(2026, 8, 20, 1, 30, 10, tzinfo=UTC)
    info = _complete_info(
        currentPrice=230.0,
        currency="HKD",
        regularMarketTime=int((now - timedelta(seconds=10)).timestamp()),
    )
    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda _symbol: FakeTicker(info)),
    )
    monkeypatch.setattr("data.futu_provider.quote_available", lambda: False)
    monkeypatch.setattr(
        fetcher,
        "_find_hk_quote",
        lambda *_args, **_kwargs: (
            {
                "代码": "00700",
                "名称": "腾讯控股",
                "最新价": "121",
                "更新时间": "2026-08-20T09:30:00+08:00",
            },
            now.isoformat(),
            CacheState.STALE_IF_ERROR,
        ),
    )
    monkeypatch.setattr(fetcher, "_utc_now_iso", lambda: now.isoformat())

    snapshot = fetcher.get_stock(
        "00700",
        "HK",
        required_fields={"price"},
        freshness_policies={"price": PRICE_POLICY},
        freshness_context=FreshnessContext(
            evaluated_at=now,
            market_phase="open",
        ),
    )

    assert snapshot["price"] == 230.0
    assert snapshot["source"]["price"] == "yfinance"
    assert "akshare:quote_failed_freshness" in snapshot["quality_issues"]


def test_future_akshare_quote_retries_with_fresh_yfinance_price(monkeypatch) -> None:
    now = datetime(2026, 8, 20, 1, 30, 10, tzinfo=UTC)
    info = _complete_info(
        currentPrice=230.0,
        currency="HKD",
        regularMarketTime=int((now - timedelta(seconds=10)).timestamp()),
    )
    monkeypatch.setattr(
        fetcher,
        "yf",
        SimpleNamespace(Ticker=lambda _symbol: FakeTicker(info)),
    )
    monkeypatch.setattr("data.futu_provider.quote_available", lambda: False)
    monkeypatch.setattr(
        fetcher,
        "ak",
        SimpleNamespace(
            stock_hk_spot_em=lambda: FakeFrame(
                [
                    {
                        "代码": "00700",
                        "名称": "腾讯控股",
                        "最新价": "121",
                        "更新时间": "2026-08-20T09:40:00+08:00",
                    }
                ]
            )
        ),
    )
    monkeypatch.setattr(fetcher, "_utc_now_iso", lambda: now.isoformat())

    snapshot = fetcher.get_stock(
        "00700",
        "HK",
        required_fields={"price"},
        freshness_policies={"price": PRICE_POLICY},
        freshness_context=FreshnessContext(
            evaluated_at=now,
            market_phase="open",
        ),
    )

    assert snapshot["price"] == 230.0
    assert snapshot["source"]["price"] == "yfinance"
    assert "akshare:quote_failed_freshness" in snapshot["quality_issues"]


@pytest.mark.parametrize("ticker", ["", "ABC", "00000", "100000"])
def test_invalid_hk_symbols_fail_explicitly(ticker):
    with pytest.raises(ValueError):
        fetcher.canonical_hk_symbol(ticker)


def test_unsupported_market_fails_instead_of_silently_using_us():
    with pytest.raises(ValueError, match="Unsupported market"):
        fetcher.get_stock("AAPL", "EU")


TEST_NOW = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
PRICE_POLICY = FieldFreshnessPolicy(
    max_source_age_seconds=60,
    max_observation_age_seconds=60,
    aging_ratio=0.8,
    session_aware=True,
)
FUNDAMENTAL_POLICY = FieldFreshnessPolicy(
    max_source_age_seconds=None,
    max_observation_age_seconds=60,
    aging_ratio=0.8,
    allow_observed_only=True,
)


def _reliability_kwargs(now=TEST_NOW):
    return {
        "required_fields": {"price", "pe_ttm"},
        "freshness_policies": {
            "price": PRICE_POLICY,
            "pe_ttm": FUNDAMENTAL_POLICY,
        },
        "freshness_context": FreshnessContext(
            evaluated_at=now,
            market_phase="open",
        ),
    }


def test_get_stock_builds_and_gates_a_serializable_reliability_report(monkeypatch):
    info = _complete_info(
        regularMarketTime=int((TEST_NOW - timedelta(seconds=10)).timestamp())
    )
    monkeypatch.setattr(
        fetcher, "yf", SimpleNamespace(Ticker=lambda _symbol: FakeTicker(info))
    )
    monkeypatch.setattr(fetcher, "_utc_now_iso", lambda: TEST_NOW.isoformat())
    runtime = ProviderRuntime(
        ProviderRuntimeConfig(request_timeout_seconds=1),
        clock=lambda: TEST_NOW,
    )

    snapshot = fetcher.get_stock(
        "AAPL", "US", provider_runtime=runtime, **_reliability_kwargs()
    )

    report = snapshot["reliability"]
    operations = {
        (attempt["provider"], attempt["operation"])
        for attempt in report["provider_attempts"]
    }
    assert report["overall"] == "DEGRADED"
    assert report["full_coverage"] is True
    assert report["usable_for_trusted_silence"] is False
    assert report["fields"]["price"]["time_basis"] == "source_event"
    assert report["fields"]["pe_ttm"]["time_basis"] == "observed_only"
    assert operations == {("yfinance", "info"), ("yfinance", "history")}


def test_hard_provider_failure_has_blind_report(monkeypatch):
    class FailingTicker:
        @property
        def info(self):
            raise TimeoutError("https://secret.example/?token=hidden")

        def history(self, period, timeout=10):
            raise AssertionError("history must not run after required info failure")

    monkeypatch.setattr(
        fetcher, "yf", SimpleNamespace(Ticker=lambda _symbol: FailingTicker())
    )
    runtime = ProviderRuntime(
        ProviderRuntimeConfig(max_attempts=1, request_timeout_seconds=1),
        clock=lambda: TEST_NOW,
    )

    with pytest.raises(ProviderUnavailableError) as exc_info:
        fetcher.get_stock(
            "AAPL", "US", provider_runtime=runtime, **_reliability_kwargs()
        )

    assert exc_info.value.report is not None
    assert exc_info.value.report.overall.value == "BLIND"
    assert exc_info.value.report.usable_for_signal is False
    assert exc_info.value.attempts[0].failure_class == "timeout"
    assert "secret" not in str(exc_info.value)


def test_missing_foundational_price_is_blind_even_when_fundamentals_exist(
    monkeypatch,
):
    info = _complete_info(
        currentPrice=None,
        regularMarketPrice=None,
        regularMarketTime=None,
    )
    monkeypatch.setattr(
        fetcher, "yf", SimpleNamespace(Ticker=lambda _symbol: FakeTicker(info))
    )
    monkeypatch.setattr(fetcher, "_utc_now_iso", lambda: TEST_NOW.isoformat())

    with pytest.raises(ProviderUnavailableError) as exc_info:
        fetcher.get_stock("AAPL", "US", **_reliability_kwargs())

    assert exc_info.value.report is not None
    assert exc_info.value.report.overall.value == "BLIND"
    assert exc_info.value.report.fields["pe_ttm"].usable_for_signal is True
    assert exc_info.value.report.fields["price"].usable_for_signal is False


def test_fresh_ak_price_survives_stale_yfinance_fundamentals(monkeypatch):
    class Clock:
        current = TEST_NOW

        def __call__(self):
            return self.current

        def advance(self, seconds):
            self.current += timedelta(seconds=seconds)

    clock = Clock()
    state = {"fail_info": False}
    info = _complete_info(
        currentPrice=99,
        trailingPE=40,
        currency="HKD",
        regularMarketTime=int((TEST_NOW - timedelta(seconds=10)).timestamp()),
    )

    class DynamicTicker(FakeTicker):
        @property
        def info(self):
            if state["fail_info"]:
                raise TimeoutError
            return self._info

        @info.setter
        def info(self, value):
            self._info = value

    monkeypatch.setattr(
        fetcher, "yf", SimpleNamespace(Ticker=lambda _symbol: DynamicTicker(info))
    )
    monkeypatch.setattr(
        fetcher,
        "ak",
        SimpleNamespace(
            stock_hk_spot_em=lambda: FakeFrame(
                [
                    {
                        "代码": "00700",
                        "名称": "腾讯控股",
                        "最新价": "120",
                        "更新时间": clock.current.isoformat(),
                    }
                ]
            )
        ),
    )
    monkeypatch.setattr(
        fetcher, "_utc_now_iso", lambda: clock.current.isoformat()
    )
    runtime = ProviderRuntime(
        ProviderRuntimeConfig(
            max_attempts=1,
            failure_threshold=5,
            fresh_cache_seconds=5,
            stale_if_error_seconds=100,
            request_timeout_seconds=1,
        ),
        clock=clock,
    )
    fetcher.get_hk_stock("00700", provider_runtime=runtime)
    clock.advance(6)
    state["fail_info"] = True

    snapshot = fetcher.get_stock(
        "00700",
        "HK",
        provider_runtime=runtime,
        **_reliability_kwargs(clock.current),
    )
    report = snapshot["reliability"]
    result = evaluate(
        "00700",
        snapshot,
        {
            "watchlist": {
                "00700": {
                    "name": "Tencent",
                    "sell_rules": [
                        {"id": "price", "type": "price_above", "value": 110},
                        {"id": "pe", "type": "pe_above", "value": 30},
                    ],
                    "buy_rules": [],
                }
            }
        },
    )

    attempts = report["provider_attempts"]
    assert report["overall"] == "DEGRADED"
    assert report["cache_state"] == "stale_if_error"
    assert report["fields"]["price"]["usable_for_signal"] is True
    assert report["fields"]["pe_ttm"]["cache_state"] == "stale_if_error"
    assert report["fields"]["pe_ttm"]["usable_for_signal"] is False
    assert snapshot["price"] == 120
    assert snapshot["pe_ttm"] is None
    assert snapshot["context_values"]["pe_ttm"] == 40
    assert result["decision"] == "SELL_REVIEW"
    assert {item["provider"] for item in attempts} == {"yfinance", "akshare"}
