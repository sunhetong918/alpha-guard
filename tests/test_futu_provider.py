"""Futu quote provider tests with a fake OpenD SDK."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from data import futu_provider
from reliability import CacheState, ProviderRuntime, ProviderRuntimeConfig


class _FakeFrame:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return self._rows


class _FakeQuoteContext:
    rows: list[dict] = []
    ret: int = 0
    expected_codes: list[str] = ["HK.00700"]

    def __init__(self, host: str, port: int, *, is_async_connect: bool) -> None:
        assert is_async_connect is True

    def set_sync_query_connect_timeout(self, timeout: float) -> None:
        assert timeout == 2.0

    def get_market_snapshot(self, codes: list[str]) -> tuple[int, _FakeFrame]:
        assert codes == self.expected_codes
        return self.ret, _FakeFrame(self.rows)

    def close(self) -> None:
        pass


def _install_fake(
    monkeypatch,
    rows: list[dict],
    *,
    expected_codes: list[str] | None = None,
) -> None:
    fake = SimpleNamespace(OpenQuoteContext=_FakeQuoteContext)
    monkeypatch.setattr(futu_provider, "futu_api", fake)
    _FakeQuoteContext.rows = rows
    _FakeQuoteContext.expected_codes = expected_codes or ["HK.00700"]
    monkeypatch.setenv("FUTU_ENABLED", "true")


def test_quote_unavailable_when_sdk_missing(monkeypatch) -> None:
    monkeypatch.setattr(futu_provider, "futu_api", None)
    assert futu_provider.quote_available() is False


def test_quote_available_requires_env_switch(monkeypatch) -> None:
    fake = SimpleNamespace(OpenQuoteContext=_FakeQuoteContext)
    monkeypatch.setattr(futu_provider, "futu_api", fake)
    monkeypatch.delenv("FUTU_ENABLED", raising=False)
    assert futu_provider.quote_available() is False


def test_disabled_futu_never_imports_the_heavy_optional_sdk(monkeypatch) -> None:
    monkeypatch.setenv("FUTU_ENABLED", "false")

    def unexpected_import():
        raise AssertionError("disabled Futu must not import the optional SDK")

    monkeypatch.setattr(futu_provider, "_load_futu_api", unexpected_import)

    assert futu_provider.quote_available() is False


def test_broken_optional_sdk_fails_closed_without_blocking_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FUTU_ENABLED", "true")
    monkeypatch.setattr(futu_provider, "futu_api", None)
    monkeypatch.setattr(futu_provider, "_futu_import_failed", False)

    def broken_import(name: str):
        raise OSError("https://secret.invalid/broken-native-module")

    monkeypatch.setattr(futu_provider, "import_module", broken_import)

    assert futu_provider.quote_available() is False


def test_fetch_hk_quote_normalizes_row(monkeypatch) -> None:
    _install_fake(
        monkeypatch,
        [
            {
                "code": "HK.00700",
                "last_price": 310.2,
                "name": "腾讯控股",
                "update_time": "2026-01-05 09:30:00",
            }
        ],
    )
    quote, attempts = futu_provider.fetch_hk_quote(
        "00700", host="127.0.0.1", port=11111
    )
    assert quote is not None
    assert quote.price == 310.2
    assert quote.name == "腾讯控股"
    assert quote.source_as_of is not None
    assert quote.source_as_of.endswith("+00:00")
    assert "T01:30:00" in quote.source_as_of  # HK 09:30 == UTC 01:30


def test_fetch_us_quote_uses_official_update_time_and_eastern_timezone(
    monkeypatch,
) -> None:
    _install_fake(
        monkeypatch,
        [
            {
                "code": "US.AAPL",
                "last_price": 231.5,
                "name": "Apple",
                "update_time": "2026-08-20 09:30:00.301",
            }
        ],
        expected_codes=["US.AAPL"],
    )

    quote, attempts = futu_provider.fetch_quote(
        "AAPL",
        market="US",
        host="127.0.0.1",
        port=11111,
    )

    assert attempts == ()
    assert quote is not None
    assert quote.price == 231.5
    assert quote.name == "Apple"
    assert quote.source_as_of == "2026-08-20T13:30:00.301000+00:00"


def test_fetch_hk_quote_rejects_bad_price(monkeypatch) -> None:
    _install_fake(monkeypatch, [{"last_price": -1, "name": "bad"}])
    quote, _ = futu_provider.fetch_hk_quote("00700", host="127.0.0.1", port=11111)
    assert quote is None


def test_fetch_hk_quote_swallows_provider_error(monkeypatch) -> None:
    class _Boom:
        def __init__(self, host: str, port: int, *, is_async_connect: bool) -> None:
            assert is_async_connect is True

        def set_sync_query_connect_timeout(self, timeout: float) -> None:
            assert timeout == 2.0

        def get_market_snapshot(self, codes):  # noqa: ANN001
            raise ConnectionError("opend down")

        def close(self) -> None:
            pass

    fake = SimpleNamespace(OpenQuoteContext=_Boom)
    monkeypatch.setattr(futu_provider, "futu_api", fake)
    quote, _ = futu_provider.fetch_hk_quote("00700", host="127.0.0.1", port=11111)
    assert quote is None


def test_opend_failure_preserves_sanitized_provider_attempt(monkeypatch) -> None:
    class _Down:
        def __init__(self, host: str, port: int, *, is_async_connect: bool) -> None:
            raise ConnectionError("https://secret.invalid/private-token")

    monkeypatch.setattr(
        futu_provider,
        "futu_api",
        SimpleNamespace(OpenQuoteContext=_Down),
    )
    runtime = ProviderRuntime(ProviderRuntimeConfig(max_attempts=1))

    quote, attempts = futu_provider.fetch_quote(
        "AAPL",
        market="US",
        host="127.0.0.1",
        port=11111,
        runtime=runtime,
    )

    assert quote is None
    assert len(attempts) == 1
    assert attempts[0].provider == "futu"
    assert attempts[0].operation == "us_snapshot"
    assert attempts[0].failure_class == "connection"
    assert "secret" not in repr(attempts)


def test_stale_futu_cache_is_explicitly_not_usable_for_signals(monkeypatch) -> None:
    now = [datetime(2026, 8, 20, 13, 30, tzinfo=UTC)]
    fail = [False]

    class _Context:
        def __init__(self, host: str, port: int, *, is_async_connect: bool) -> None:
            assert is_async_connect is True

        def set_sync_query_connect_timeout(self, timeout: float) -> None:
            assert timeout == 2.0

        def get_market_snapshot(self, codes):  # noqa: ANN001
            if fail[0]:
                raise ConnectionError("OpenD unavailable")
            return (
                0,
                _FakeFrame(
                    [
                        {
                            "code": "US.AAPL",
                            "last_price": 231.5,
                            "name": "Apple",
                            "update_time": "2026-08-20 09:30:00",
                        }
                    ]
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        futu_provider,
        "futu_api",
        SimpleNamespace(OpenQuoteContext=_Context),
    )
    runtime = ProviderRuntime(
        ProviderRuntimeConfig(
            max_attempts=1,
            fresh_cache_seconds=1,
            stale_if_error_seconds=60,
        ),
        clock=lambda: now[0],
    )
    fresh, _ = futu_provider.fetch_quote(
        "AAPL", market="US", host="127.0.0.1", port=11111, runtime=runtime
    )
    now[0] += timedelta(seconds=2)
    fail[0] = True

    stale, attempts = futu_provider.fetch_quote(
        "AAPL", market="US", host="127.0.0.1", port=11111, runtime=runtime
    )

    assert fresh is not None and fresh.cache_state == CacheState.MISS
    assert stale is not None
    assert stale.cache_state == CacheState.STALE_IF_ERROR
    assert stale.usable_for_signal is False
    assert attempts[-1].outcome == "stale_fallback"


def test_batch_snapshot_uses_one_quote_context_for_multiple_tickers(
    monkeypatch,
) -> None:
    calls = {"opened": 0, "closed": 0}

    class _BatchContext:
        def __init__(self, host: str, port: int, *, is_async_connect: bool) -> None:
            assert is_async_connect is True
            calls["opened"] += 1

        def set_sync_query_connect_timeout(self, timeout: float) -> None:
            assert timeout == 2.0

        def get_market_snapshot(self, codes):  # noqa: ANN001
            assert codes == ["HK.00005", "HK.00700"]
            return (
                0,
                _FakeFrame(
                    [
                        {
                            "code": "HK.00005",
                            "last_price": 70.0,
                            "name": "HSBC",
                            "update_time": "2026-08-20 09:30:00",
                        },
                        {
                            "code": "HK.00700",
                            "last_price": 600.0,
                            "name": "Tencent",
                            "update_time": "2026-08-20 09:30:00",
                        },
                    ]
                ),
            )

        def close(self) -> None:
            calls["closed"] += 1

    monkeypatch.setattr(
        futu_provider,
        "futu_api",
        SimpleNamespace(OpenQuoteContext=_BatchContext),
    )

    quotes, attempts = futu_provider.fetch_quotes(
        ["00700", "00005"],
        market="HK",
        host="127.0.0.1",
        port=11111,
    )

    assert attempts == ()
    assert list(quotes) == ["00005", "00700"]
    assert quotes["00005"].price == 70.0
    assert quotes["00700"].price == 600.0
    assert calls == {"opened": 1, "closed": 1}


def test_hk_alias_is_sent_to_opend_as_canonical_five_digit_code(
    monkeypatch,
) -> None:
    _install_fake(
        monkeypatch,
        [
            {
                "code": "HK.00700",
                "last_price": 600.0,
                "name": "Tencent",
                "update_time": "2026-08-20 09:30:00",
            }
        ],
        expected_codes=["HK.00700"],
    )

    quote, _attempts = futu_provider.fetch_quote(
        "0700.HK",
        market="HK",
        host="127.0.0.1",
        port=11111,
    )

    assert quote is not None
    assert quote.price == 600.0


def test_provider_rejects_more_than_official_400_code_batch() -> None:
    with pytest.raises(ValueError, match="400"):
        futu_provider.fetch_quotes(
            [f"TICKER{index}" for index in range(401)],
            market="US",
            host="127.0.0.1",
            port=11111,
        )


def test_malformed_snapshot_falls_back_with_invalid_response_evidence(
    monkeypatch,
) -> None:
    class _MalformedFrame:
        def to_dict(self, orient: str):  # noqa: ANN201
            raise TypeError("https://secret.invalid/private-frame")

    class _MalformedContext:
        def __init__(self, host: str, port: int, *, is_async_connect: bool) -> None:
            assert is_async_connect is True

        def set_sync_query_connect_timeout(self, timeout: float) -> None:
            assert timeout == 2.0

        def get_market_snapshot(self, codes):  # noqa: ANN001
            return 0, _MalformedFrame()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        futu_provider,
        "futu_api",
        SimpleNamespace(OpenQuoteContext=_MalformedContext),
    )
    runtime = ProviderRuntime(ProviderRuntimeConfig(max_attempts=1))

    quotes, attempts = futu_provider.fetch_quotes(
        ["AAPL"],
        market="US",
        host="127.0.0.1",
        port=11111,
        runtime=runtime,
    )

    assert quotes == {}
    assert len(attempts) == 1
    assert attempts[0].failure_class == "invalid_response"
    assert "secret" not in repr(attempts)


def test_opend_connection_is_bounded_before_snapshot_query(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _AsyncContext:
        status = "WAIT_RECONNECT"

        def __init__(self, host: str, port: int, *, is_async_connect: bool) -> None:
            calls["async"] = is_async_connect

        def set_sync_query_connect_timeout(self, timeout: float) -> None:
            calls["timeout"] = timeout

        def get_market_snapshot(self, codes):  # noqa: ANN001
            return -1, "Connect timeout"

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(
        futu_provider,
        "futu_api",
        SimpleNamespace(OpenQuoteContext=_AsyncContext),
    )

    runtime = ProviderRuntime(ProviderRuntimeConfig(max_attempts=1))
    quote, attempts = futu_provider.fetch_quote(
        "AAPL",
        market="US",
        host="127.0.0.1",
        port=59999,
        runtime=runtime,
    )

    assert quote is None
    assert len(attempts) == 1
    assert attempts[0].failure_class == "connection"
    assert calls == {"async": True, "timeout": 2.0, "closed": True}


def test_empty_or_untimestamped_batch_is_not_cached_as_provider_success(
    monkeypatch,
) -> None:
    valid = [False]
    calls = {"snapshots": 0}

    class _RecoveringContext:
        def __init__(self, host: str, port: int, *, is_async_connect: bool) -> None:
            pass

        def set_sync_query_connect_timeout(self, timeout: float) -> None:
            pass

        def get_market_snapshot(self, codes):  # noqa: ANN001
            calls["snapshots"] += 1
            return (
                0,
                _FakeFrame(
                    [
                        {
                            "code": "US.AAPL",
                            "last_price": 231.5 if valid[0] else "N/A",
                            "name": "Apple",
                            "update_time": (
                                "2026-08-20 09:30:00" if valid[0] else None
                            ),
                        }
                    ]
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        futu_provider,
        "futu_api",
        SimpleNamespace(OpenQuoteContext=_RecoveringContext),
    )
    runtime = ProviderRuntime(
        ProviderRuntimeConfig(max_attempts=1, fresh_cache_seconds=300)
    )

    empty, first_attempts = futu_provider.fetch_quotes(
        ["AAPL"],
        market="US",
        host="127.0.0.1",
        port=11111,
        runtime=runtime,
    )
    valid[0] = True
    recovered, second_attempts = futu_provider.fetch_quotes(
        ["AAPL"],
        market="US",
        host="127.0.0.1",
        port=11111,
        runtime=runtime,
    )

    assert empty == {}
    assert first_attempts[-1].failure_class == "invalid_response"
    assert recovered["AAPL"].price == 231.5
    assert second_attempts[-1].outcome == "success"
    assert calls["snapshots"] == 2
