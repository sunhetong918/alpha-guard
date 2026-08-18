"""Futu quote provider tests with a fake OpenD SDK."""

from __future__ import annotations

from types import SimpleNamespace

from data import futu_provider


class _FakeFrame:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return self._rows


class _FakeQuoteContext:
    rows: list[dict] = []
    ret: int = 0

    def __init__(self, host: str, port: int) -> None:
        pass

    def get_market_snapshot(self, codes: list[str]) -> tuple[int, _FakeFrame]:
        assert codes == ["HK.00700"]
        return self.ret, _FakeFrame(self.rows)

    def close(self) -> None:
        pass


def _install_fake(monkeypatch, rows: list[dict]) -> None:
    fake = SimpleNamespace(OpenQuoteContext=_FakeQuoteContext)
    monkeypatch.setattr(futu_provider, "futu_api", fake)
    _FakeQuoteContext.rows = rows
    monkeypatch.setenv("FUTU_ENABLED", "true")


def test_quote_unavailable_when_sdk_missing(monkeypatch) -> None:
    monkeypatch.setattr(futu_provider, "futu_api", None)
    assert futu_provider.quote_available() is False


def test_quote_available_requires_env_switch(monkeypatch) -> None:
    fake = SimpleNamespace(OpenQuoteContext=_FakeQuoteContext)
    monkeypatch.setattr(futu_provider, "futu_api", fake)
    monkeypatch.delenv("FUTU_ENABLED", raising=False)
    assert futu_provider.quote_available() is False


def test_fetch_hk_quote_normalizes_row(monkeypatch) -> None:
    _install_fake(
        monkeypatch,
        [
            {
                "code": "HK.00700",
                "last_price": 310.2,
                "stock_name": "腾讯控股",
                "update_timestamp": "2026-01-05 09:30:00",
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


def test_fetch_hk_quote_rejects_bad_price(monkeypatch) -> None:
    _install_fake(monkeypatch, [{"last_price": -1, "stock_name": "bad"}])
    quote, _ = futu_provider.fetch_hk_quote("00700", host="127.0.0.1", port=11111)
    assert quote is None


def test_fetch_hk_quote_swallows_provider_error(monkeypatch) -> None:
    class _Boom:
        def __init__(self, host: str, port: int) -> None:
            pass

        def get_market_snapshot(self, codes):  # noqa: ANN001
            raise ConnectionError("opend down")

        def close(self) -> None:
            pass

    fake = SimpleNamespace(OpenQuoteContext=_Boom)
    monkeypatch.setattr(futu_provider, "futu_api", fake)
    quote, _ = futu_provider.fetch_hk_quote("00700", host="127.0.0.1", port=11111)
    assert quote is None
