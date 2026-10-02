from __future__ import annotations

import json

from typer.testing import CliRunner

import main

runner = CliRunner()


def test_research_commands_offline_roundtrip_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'STATE_PATH', tmp_path / 'state.db')
    status = runner.invoke(main.app, ['research', 'status'])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)['runtime']['state'] == 'STOPPED'
    config = tmp_path / 'research.json'
    config.write_text(json.dumps({'markets': ['CN', 'HK', 'US'], 'horizons': ['short', 'swing', 'long'],
                                  'news_enabled': False, 'ai_enabled': False}))
    configured = runner.invoke(main.app, ['research', 'configure', str(config)])
    assert configured.exit_code == 0, configured.output
    scanned = runner.invoke(main.app, ['research', 'scan'])
    assert scanned.exit_code == 0, scanned.output
    state = json.loads(scanned.output)
    assert state['state'] == 'STOPPED'
    assert state['last_run']['status'] == 'disabled'


def test_research_cli_replay_has_no_fake_metrics_and_rejects_naive_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'STATE_PATH', tmp_path / 'state.db')
    sample = tmp_path / 'prices.json'
    sample.write_text('[]')
    result = runner.invoke(main.app, ['research', 'replay', str(sample), '--as-of', '2026-09-30T00:00:00+00:00'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['summary_by_market_horizon'] == []
    invalid = runner.invoke(main.app, ['research', 'replay', str(sample), '--as-of', '2026-09-30T00:00:00'])
    assert invalid.exit_code == 2


def test_research_cli_bad_settings_are_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'STATE_PATH', tmp_path / 'state.db')
    config = tmp_path / 'bad.json'
    config.write_text(json.dumps({'api_key': 'private-test-secret'}))
    result = runner.invoke(main.app, ['research', 'configure', str(config)])
    assert result.exit_code == 2
    assert 'private-test-secret' not in result.output
    assert not (tmp_path / 'state.db').exists()
