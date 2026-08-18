"""Universe screening and score-history tests."""

from __future__ import annotations

from types import SimpleNamespace

from analysis.history import (
    load_scores,
    render_score_changes,
    save_scores,
    score_changes,
)
from analysis.screen import hk_universe, us_universe


class _FakeFrame:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return self._rows


def test_us_universe_is_bounded_and_ordered() -> None:
    assert us_universe(5) == list(("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"))
    assert us_universe(0) == []


def test_hk_universe_falls_back_without_akshare(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_akshare(name, *args, **kwargs):
        if name == "akshare":
            raise ModuleNotFoundError("akshare")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_akshare)
    assert hk_universe(3) == ["00700", "09988", "03690"]


def test_hk_universe_ranks_by_market_cap(monkeypatch) -> None:
    fake = SimpleNamespace(
        stock_hk_spot_em=lambda: _FakeFrame(
            [
                {"代码": "00700", "总市值": 3_000_000},
                {"代码": "09988", "总市值": 1_500_000},
                {"代码": "00001", "总市值": "not-a-number"},
                {"代码": "bad", "总市值": 9_999_999},
            ]
        )
    )
    import sys

    monkeypatch.setitem(sys.modules, "akshare", fake)
    assert hk_universe(2) == ["00700", "09988"]


def test_hk_universe_falls_back_on_snapshot_error(monkeypatch) -> None:
    def boom():
        raise ConnectionError("offline")

    import sys

    fake = SimpleNamespace(stock_hk_spot_em=boom)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    assert hk_universe(2) == ["00700", "09988"]


def test_score_changes_detects_threshold_moves() -> None:
    previous = {
        "scores": {
            "AAPL": {"total_score": 80},
            "00700": {"total_score": 50},
            "NEW": {"total_score": None},
        }
    }
    ranked = [
        {"ticker": "AAPL", "name": "Apple", "total_score": 72},
        {"ticker": "00700", "name": "Tencent", "total_score": 48},
        {"ticker": "NEW", "name": "New", "total_score": 90},
    ]
    changes = score_changes(previous, ranked, threshold=5)
    assert len(changes) == 1
    assert changes[0]["ticker"] == "AAPL"
    assert changes[0]["delta"] == -8.0


def test_save_and_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "scores.json"
    ranked = [{"ticker": "AAPL", "total_score": 72, "as_of": "2026-01-01"}]
    save_scores({str(item["ticker"]): item for item in ranked}, path)
    loaded = load_scores(path)
    assert loaded["scores"]["AAPL"]["total_score"] == 72


def test_load_missing_file_is_empty(tmp_path) -> None:
    assert load_scores(tmp_path / "nope.json") == {}


def test_render_score_changes_is_bounded() -> None:
    text = render_score_changes(
        [
            {
                "ticker": "AAPL",
                "name": "Apple",
                "old_score": 80,
                "new_score": 72,
                "delta": -8.0,
            }
        ]
    )
    assert "AAPL" in text
    assert "不构成投资建议" in text
