"""Rank command tests via the offline fixture path."""

from __future__ import annotations


from typer.testing import CliRunner

import main
from config import PROJECT_ROOT

runner = CliRunner()


def test_rank_offline_fixture_outputs_json() -> None:
    fixture = PROJECT_ROOT / "data" / "fixtures" / "snapshots.json"
    result = runner.invoke(
        main.app, ["rank", "--fixture", str(fixture), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert "研究评分榜" in result.output or result.output.startswith("[")
    # JSON mode prints the ranked payload; verify it parses and is sorted.
    import json

    start = result.output.index("[")
    ranked = json.loads(result.output[start:])
    assert len(ranked) >= 1
    scores = [item["total_score"] for item in ranked]
    assert scores == sorted(scores, reverse=True)


def test_rank_offline_renders_disclaimer() -> None:
    fixture = PROJECT_ROOT / "data" / "fixtures" / "snapshots.json"
    result = runner.invoke(main.app, ["rank", "--fixture", str(fixture)])
    assert result.exit_code == 0, result.output
    assert "不构成投资建议" in result.output


def test_rank_online_without_tickers_or_watchlist(monkeypatch) -> None:
    from config import load_rules_config

    monkeypatch.setattr(main, "load_rules_config", load_rules_config)
    result = runner.invoke(main.app, ["rank"])
    # No enabled instruments in the shipped default config -> friendly notice.
    assert result.exit_code == 0
    assert "没有待评分标的" in result.output
