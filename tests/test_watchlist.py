from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from config import RulesConfig
from research.locking import ResearchLease
from research.store import ResearchStore
from research.watchlist import WatchlistConflictError, WatchlistStore, execute_watchlist


@pytest.fixture
def workspace(tmp_path):
    database = tmp_path / "state.db"
    rules = tmp_path / "rules.yaml"
    document = {"reliability": {"provider": {"request_timeout_seconds": 7.0}}, "watchlist": {
        "AAPL": {"name": "Apple", "market": "US", "currency": "USD", "enabled": False,
                 "cost_basis": 150.0, "alert_cooldown_hours": 24.0,
                 "buy_rules": [{"id": "apple-lower", "type": "price_below", "value": 140.0, "note": "keep note"},
                               {"id": "apple-pe", "type": "pe_below", "value": 22.0}],
                 "sell_rules": [{"id": "apple-upper", "type": "price_above", "value": 180.0}]},
        "00700": {"name": "腾讯", "market": "HK", "currency": "HKD", "enabled": True,
                  "buy_rules": [{"id": "tencent-lower", "type": "price_below", "value": 280.0}]},
    }}
    rules.write_text("# retain exact original in backup\n" + yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return database, rules, document


def execute(workspace, command, fields=None):
    database, rules, _ = workspace
    return execute_watchlist(database, command, fields or {}, rules_path=rules)


def current(workspace):
    return execute(workspace, "watchlist_get")


def save_research(workspace, ticker="MSFT", market="US", enabled=True, name="Microsoft"):
    return execute(workspace, "watchlist_save", {"market": market, "ticker": ticker, "name": name,
        "research_enabled": enabled, "expected_revision": current(workspace)["revision"]})


def preview(workspace, configuration=None):
    view = current(workspace)
    row = execute(workspace, "watchlist_get", {"key": "US:AAPL"})["item"]
    return execute(workspace, "rules_preview", {"market": "US", "ticker": "aapl",
        "configuration": configuration or row["configuration"], "expected_revision": view["revision"]})


def save_rules(workspace, value, **changes):
    fields = {"market": "US", "ticker": "AAPL", "configuration": value["configuration"],
              "expected_revision": value["revision"], "preview_token": value["preview_token"]}
    fields.update(changes)
    return execute(workspace, "rules_save", fields)


def test_disabled_new_watchlist_and_turning_off_research_keep_local_identity(workspace):
    database, rules, _ = workspace
    original = rules.read_bytes()
    view = save_research(workspace, ticker="msft", enabled=False)
    row = next(item for item in view["items"] if item["key"] == "US:MSFT")
    assert row["research_enabled"] is False and row["monitoring_enabled"] is False
    with ResearchStore(database) as store:
        assert store.load_settings()["instruments"] == []
        assert store.list_runs() == []
    save_research(workspace, ticker="msft", enabled=True)
    view = save_research(workspace, ticker="MSFT", enabled=False)
    assert any(item["key"] == "US:MSFT" and not item["research_enabled"] for item in view["items"])
    assert rules.read_bytes() == original
    assert not (rules.parent / "backups").exists()


def test_markets_are_normalized_and_research_changes_do_not_enable_monitoring(workspace):
    database, _, _ = workspace
    view = save_research(workspace, ticker="700", market="hk", name="腾讯")
    hk = [item for item in view["items"] if item["key"] == "HK:00700"]
    assert len(hk) == 1 and hk[0]["research_enabled"] and hk[0]["monitoring_enabled"]
    save_research(workspace, ticker="600519", market="cn", name="茅台")
    with ResearchStore(database) as store:
        settings = store.load_settings()
        assert "CN" in settings["markets"]
        assert {item["ticker"] for item in settings["instruments"]} == {"00700", "600519"}
    view = save_research(workspace, ticker="aapl", enabled=True, name="Apple")
    assert next(item for item in view["items"] if item["key"] == "US:AAPL")["monitoring_enabled"] is False


def test_preview_and_save_preserve_all_other_securities_and_stable_rules(workspace):
    database, rules, document = workspace
    original = rules.read_bytes()
    config = deepcopy(execute(workspace, "watchlist_get", {"key": "US:AAPL"})["item"]["configuration"])
    assert config["market"] == "US"
    config["enabled"] = True
    config["buy_rules"][0]["value"] = 145.5
    config["sell_rules"].append({"type": "price_drop_pct", "value": 12.0, "note": "new condition"})
    value = preview(workspace, config)
    new_id = value["configuration"]["sell_rules"][-1]["id"]
    assert new_id and rules.read_bytes() == original
    result = save_rules(workspace, value)
    saved = yaml.safe_load(rules.read_text())
    RulesConfig.model_validate(saved)
    assert saved["watchlist"]["00700"] == document["watchlist"]["00700"]
    assert saved["reliability"] == document["reliability"]
    assert saved["watchlist"]["AAPL"]["buy_rules"][0]["id"] == "apple-lower"
    assert saved["watchlist"]["AAPL"]["buy_rules"][0]["note"] == "keep note"
    assert saved["watchlist"]["AAPL"]["buy_rules"][1]["id"] == "apple-pe"
    assert saved["watchlist"]["AAPL"]["sell_rules"][-1]["id"] == new_id
    assert saved["watchlist"]["AAPL"]["enabled"]
    assert result["revision"] != value["revision"]
    backups = list((rules.parent / "backups").glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    with ResearchStore(database) as store:
        assert store.load_settings()["instruments"] == []
        assert store.list_runs() == []


def test_save_rejects_changed_preview_and_stale_revision_without_overwrite(workspace):
    _, rules, _ = workspace
    value = preview(workspace)
    original = rules.read_bytes()
    changed = deepcopy(value["configuration"])
    changed["enabled"] = True
    with pytest.raises(ValueError, match="confirmed preview"):
        save_rules(workspace, value, configuration=changed)
    assert rules.read_bytes() == original
    rules.write_bytes(original + b"\n# another editor\n")
    with pytest.raises(WatchlistConflictError):
        save_rules(workspace, value)
    assert rules.read_bytes().endswith(b"# another editor\n")
    assert not (rules.parent / "backups").exists()


def test_schema_rejects_invalid_units_cost_duplicate_ids_and_cn_monitoring(workspace):
    value = preview(workspace)["configuration"]
    for update in ({"currency": "HKD"}, {"cost_basis": None,
        "buy_rules": [{"id": "drawdown", "type": "price_drop_pct", "value": 12.0}]},
        {"buy_rules": [{"id": "tencent-lower", "type": "price_below", "value": 10.0}]},
        {"buy_rules": [{"id": "bad", "type": "price_below", "value": float("nan")}]},
        {"enabled": True, "buy_rules": [], "sell_rules": []}):
        with pytest.raises(ValueError):
            preview(workspace, {**value, **update})
    with pytest.raises(ValueError, match="research only"):
        execute(workspace, "rules_preview", {"market": "CN", "ticker": "600519",
            "configuration": value, "expected_revision": current(workspace)["revision"]})


@pytest.mark.parametrize("command,fields", [
    ("watchlist_get", {"extra": True}), ("watchlist_save", {}), ("rules_preview", {}),
    ("rules_save", {}), ("scan", {}),
])
def test_commands_reject_extra_and_missing_fields(workspace, command, fields):
    with pytest.raises(ValueError):
        execute(workspace, command, fields)


def test_edits_are_blocked_by_research_or_shared_rules_writer_lease(workspace):
    database, rules, _ = workspace
    value = preview(workspace)
    for target in (database, rules):
        with ResearchLease(target):
            with pytest.raises(ValueError, match="already running"):
                save_rules(workspace, value)
    with ResearchLease(database):
        view = current(workspace)  # Reading remains available during research.
        with pytest.raises(ValueError, match="already running"):
            execute(workspace, "watchlist_save", {"market": "US", "ticker": "MSFT", "name": "Microsoft",
                "research_enabled": True, "expected_revision": view["revision"]})


def test_failed_atomic_replacement_preserves_original_and_backup(workspace, monkeypatch):
    _, rules, _ = workspace
    value = preview(workspace)
    original = rules.read_bytes()
    def fail(*args):
        raise OSError("disk write failure")
    monkeypatch.setattr("research.watchlist.os.replace", fail)
    with pytest.raises(OSError):
        save_rules(workspace, value)
    assert rules.read_bytes() == original
    assert list((rules.parent / "backups").glob("*.bak"))[0].read_bytes() == original
    assert not list(rules.parent.glob(".rules.yaml.*.tmp"))


def test_watchlist_schema_migration_backs_up_existing_database(workspace):
    database, _, _ = workspace
    with ResearchStore(database):
        pass
    with WatchlistStore(database) as store:
        assert store.backup_path is not None and Path(store.backup_path).is_file()
    with WatchlistStore(database) as store:
        assert store.backup_path is None


def test_more_than_fifty_research_instruments_remain_valid_across_layers(workspace):
    from research.service import ResearchOptions

    database, _, _ = workspace
    with ResearchStore(database) as store:
        settings = store.load_settings()
        settings['instruments'] = [{'market': 'US', 'ticker': f'STOCK{index}', 'name': ''} for index in range(51)]
        store.save_settings(settings)
    save_research(workspace, ticker='STOCK51', name='Another security')
    with ResearchStore(database) as store:
        options = ResearchOptions.model_validate(store.load_settings())
    assert len(options.instruments) == 52


def _assert_transport_fits(result):
    from guardian.protocol import MAX_FRAME_BYTES, RpcSuccessResponse, encode_frame, parse_response_json, serialize_response

    response = RpcSuccessResponse.model_validate({'jsonrpc': '2.0', 'id': 'watchlist-size-test',
        'result': {'accepted': True, 'command': 'watchlist_get', 'result': result}})
    payload = serialize_response(response)
    assert len(payload) <= MAX_FRAME_BYTES
    assert parse_response_json(encode_frame(payload)[4:]).result == response.result


def test_large_watchlist_pages_metadata_searches_all_and_reads_complete_selected_rules(workspace):
    _, rules, _ = workspace
    document = {'watchlist': {f'STOCK{stock:02}': {
        'name': f'Company {stock}', 'market': 'US', 'currency': 'USD', 'enabled': True,
        'buy_rules': [{'id': f'stock-{stock}-rule-{index}', 'type': 'price_below', 'value': 10.0,
                       'note': 'n' * 300} for index in range(100)]}
        for stock in range(30)}}
    RulesConfig.model_validate(document)
    rules.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
    assert rules.stat().st_size > 1_048_576  # Valid source larger than a complete IPC frame.
    first = current(workspace)
    second = execute(workspace, 'watchlist_get', {'offset': first['next_offset']})
    assert len(first['items']) == 20 and len(second['items']) == 10
    assert first['total'] == second['total'] == 30 and second['next_offset'] is None
    assert not any('configuration' in item for item in first['items'] + second['items'])
    found = execute(workspace, 'watchlist_get', {'query': 'Company 29'})
    assert [item['key'] for item in found['items']] == ['US:STOCK29']
    detail = execute(workspace, 'watchlist_get', {'key': 'us:stock29'})
    assert detail['item']['rules_editable'] is True
    assert detail['item']['configuration']['buy_rules'] == document['watchlist']['STOCK29']['buy_rules']
    for result in (first, second, found, detail):
        _assert_transport_fits(result)


def test_large_legacy_single_stock_is_readable_but_never_exposes_partial_configuration(workspace):
    _, rules, _ = workspace
    document = {'watchlist': {'BIG': {'name': 'Large old configuration', 'market': 'US',
        'currency': 'USD', 'enabled': True, 'buy_rules': [
            {'id': f'legacy-{index}', 'type': 'price_below', 'value': 10.0, 'note': '文' * 300}
            for index in range(1200)]}}}
    RulesConfig.model_validate(document)
    rules.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
    original = rules.read_bytes()
    assert 1_048_576 < len(original) < 2_000_000
    detail = execute(workspace, 'watchlist_get', {'key': 'US:BIG'})
    assert detail['item']['rule_count'] == 1200
    assert detail['item']['rules_editable'] is False
    assert 'configuration' not in detail['item']
    assert '不会保存不完整规则' in detail['item']['rules_edit_message']
    _assert_transport_fits(current(workspace))
    _assert_transport_fits(detail)
    with pytest.raises(ValueError, match='existing instrument exceeds'):
        execute(workspace, 'rules_preview', {'market': 'US', 'ticker': 'BIG',
            'expected_revision': detail['revision'], 'configuration': {
                **document['watchlist']['BIG'], 'buy_rules': document['watchlist']['BIG']['buy_rules'][:1]}})
    assert rules.read_bytes() == original


@pytest.mark.parametrize('fields', [{'offset': True}, {'offset': -1}, {'offset': 1_000_001},
    {'query': 'x' * 201}, {'query': []}, {'key': None}, {'key': 'US:AAPL', 'offset': 0}])
def test_watchlist_paging_rejects_invalid_shapes(workspace, fields):
    with pytest.raises(ValueError):
        execute(workspace, 'watchlist_get', fields)


def test_name_and_research_switch_changes_retain_existing_exchange(workspace):
    database, _, _ = workspace
    with ResearchStore(database) as store:
        settings = store.load_settings()
        settings['instruments'] = [{'market': 'US', 'ticker': 'AAPL', 'name': 'Apple', 'exchange': 'NASDAQ'}]
        store.save_settings(settings)
    save_research(workspace, ticker='aapl', name='My Apple')
    with ResearchStore(database) as store:
        assert store.load_settings()['instruments'][0]['exchange'] == 'NASDAQ'
        assert store.load_settings()['instruments'][0]['name'] == 'My Apple'
    save_research(workspace, ticker='AAPL', name='My Apple', enabled=False)
    save_research(workspace, ticker='AAPL', name='My Apple', enabled=True)
    with ResearchStore(database) as store:
        assert store.load_settings()['instruments'][0]['exchange'] == 'NASDAQ'
