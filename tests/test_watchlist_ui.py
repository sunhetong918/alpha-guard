from __future__ import annotations

from copy import deepcopy

import pytest
import yaml
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

from desktop.ui.client import GuardianClient
from desktop.ui.models import ActionReceipt
from desktop.ui.watchlist_page import RulesEditorDialog, WatchlistPage
from research.store import ResearchStore
from research.watchlist import WatchlistConflictError, execute_watchlist


@pytest.fixture(scope='module')
def qt_app():
    app = QApplication.instance() or QApplication(['watchlist-tests'])
    yield app


class LocalClient(GuardianClient):
    """Exercise the real local command boundary with temporary SQLite and YAML."""

    def __init__(self, database, rules):
        super().__init__()
        self.database = database
        self.rules = rules
        self.commands = []

    def research_command(self, command, fields=None):
        fields = deepcopy(fields or {})
        self.commands.append((command, fields))
        try:
            result = execute_watchlist(self.database, command, fields, rules_path=self.rules)
        except WatchlistConflictError:
            self.action_completed.emit(ActionReceipt(f'research-{command}', False, '配置已被其他窗口更新，请重新加载', 'test'))
        except (ValueError, OSError):
            self.request_failed.emit(f'research-{command}', 'invalid_params')
        else:
            self.action_completed.emit(ActionReceipt(f'research-{command}', True, '完成', 'test', result))

    def refresh_research(self):
        pytest.fail('watchlist editing cannot start research or poll its providers')


@pytest.fixture
def local_client(tmp_path):
    database, rules = tmp_path / 'state.db', tmp_path / 'rules.yaml'
    rules.write_text(yaml.safe_dump({'watchlist': {
        'AAPL': {'name': 'Apple', 'market': 'US', 'currency': 'USD', 'enabled': False,
            'cost_basis': 150.0, 'alert_cooldown_hours': 24.0,
            'buy_rules': [{'id': 'apple-price', 'type': 'price_below', 'value': 140.0, 'note': 'keep me'},
                          {'id': 'apple-pe', 'type': 'pe_below', 'value': 20.0}],
            'sell_rules': [{'id': 'apple-upper', 'type': 'price_above', 'value': 190.0}]},
    }}, allow_unicode=True), encoding='utf-8')
    return LocalClient(database, rules)


def _select(page, key):
    for row in range(page.table.rowCount()):
        if page.table.item(row, 0).text() == key:
            page.table.selectRow(row)
            return
    pytest.fail(f'missing security {key}')


def test_add_research_only_then_preview_confirm_monitoring_preserves_rules(qt_app, local_client):
    page = WatchlistPage(local_client)
    original = local_client.rules.read_bytes()
    assert local_client.commands == []
    page.activate()
    assert local_client.commands[-1] == ('watchlist_get', {})
    page.ticker_input.setText('msft')
    page.name_input.setText('Microsoft')
    page.save_button.click()
    assert local_client.rules.read_bytes() == original
    _select(page, 'US:MSFT')
    assert page.research_enabled.isChecked()
    page.rules_button.click()
    dialog = page._rule_dialog
    assert dialog is not None and not dialog.enabled.isChecked()
    dialog.enabled.setChecked(True)
    dialog.add_rule_button.click()
    threshold = dialog.rules_table.cellWidget(0, 2)
    assert isinstance(threshold, QLineEdit)
    threshold.setText('380.25')
    assert not dialog.save_button.isEnabled()
    dialog.preview_button.click()
    assert dialog.save_button.isEnabled()
    assert local_client.rules.read_bytes() == original
    assert '价格 ≤ 380.25 USD' in dialog.preview_text.toPlainText()
    dialog.save_button.click()
    saved = yaml.safe_load(local_client.rules.read_text())
    assert saved['watchlist']['MSFT']['enabled']
    assert saved['watchlist']['MSFT']['buy_rules'][0]['value'] == 380.25
    assert len(saved['watchlist']['AAPL']['buy_rules']) == 2
    with ResearchStore(local_client.database) as store:
        assert [item['ticker'] for item in store.load_settings()['instruments']] == ['MSFT']
        assert store.list_runs() == []
    assert {command for command, _ in local_client.commands} <= {'watchlist_get', 'watchlist_save', 'rules_preview', 'rules_save'}
    page.close()


def test_existing_multiple_rules_are_all_visible_and_keep_ids_after_edit(qt_app, local_client):
    view = execute_watchlist(local_client.database, 'watchlist_get', {'key': 'US:AAPL'}, rules_path=local_client.rules)
    dialog = RulesEditorDialog(local_client, view['item'], view['revision'])
    assert dialog.rules_table.rowCount() == 3
    threshold = dialog.rules_table.cellWidget(0, 2)
    assert isinstance(threshold, QLineEdit)
    threshold.setText('145.123456789')
    dialog.preview_button.click()
    dialog.save_button.click()
    saved = yaml.safe_load(local_client.rules.read_text())['watchlist']['AAPL']
    assert saved['buy_rules'][0]['value'] == 145.123456789
    assert saved['buy_rules'][0]['id'] == 'apple-price' and saved['buy_rules'][0]['note'] == 'keep me'
    assert saved['buy_rules'][1]['id'] == 'apple-pe'
    assert saved['sell_rules'][0]['id'] == 'apple-upper'
    dialog.close()


def test_edit_after_preview_requires_new_preview_and_conflict_does_not_save(qt_app, local_client):
    view = execute_watchlist(local_client.database, 'watchlist_get', {'key': 'US:AAPL'}, rules_path=local_client.rules)
    dialog = RulesEditorDialog(local_client, view['item'], view['revision'])
    dialog.preview_button.click()
    assert dialog.save_button.isEnabled()
    threshold = dialog.rules_table.cellWidget(0, 2)
    threshold.setText('130')
    assert not dialog.save_button.isEnabled()
    before = list(local_client.commands)
    dialog.save_button.click()
    assert local_client.commands == before
    dialog.preview_button.click()
    other = local_client.rules.read_bytes() + b'\n# edited by another window\n'
    local_client.rules.write_bytes(other)
    dialog.save_button.click()
    assert '配置已被其他窗口更新' in dialog.message.text()
    assert local_client.rules.read_bytes() == other
    assert not dialog.save_button.isEnabled()
    dialog.close()


def test_cn_watchlist_is_research_only_and_local_search_emits_navigation(qt_app, local_client):
    page = WatchlistPage(local_client)
    page.activate()
    page.market_input.setCurrentIndex(page.market_input.findData('CN'))
    page.ticker_input.setText('600519')
    page.name_input.setText('贵州茅台')
    page.research_enabled.setChecked(False)
    assert not page.rules_button.isEnabled()
    page.save_button.click()
    page.search_input.setText('茅台')
    page._refresh()
    assert page.table.rowCount() == 1
    _select(page, 'CN:600519')
    research, events = [], []
    page.view_research_requested.connect(research.append)
    page.view_events_requested.connect(events.append)
    page.research_button.click()
    page.events_button.click()
    assert research == events == ['CN:600519']
    assert not page.rules_button.isEnabled()
    assert not page.research_enabled.isChecked()
    assert '600519' not in yaml.safe_load(local_client.rules.read_text())['watchlist']
    page.close()


def test_rule_type_changes_show_units_and_never_save_hidden_overflow(qt_app, local_client):
    view = execute_watchlist(local_client.database, 'watchlist_get', {'key': 'US:AAPL'}, rules_path=local_client.rules)
    dialog = RulesEditorDialog(local_client, view['item'], view['revision'])
    kind = dialog.rules_table.cellWidget(0, 1)
    assert isinstance(kind, QComboBox)
    kind.setCurrentIndex(kind.findData('pe_below'))
    assert dialog.rules_table.cellWidget(0, 3).text() == '倍'
    kind.setCurrentIndex(kind.findData('roe_above'))
    assert dialog.rules_table.cellWidget(0, 3).text() == '%'
    dialog.close()
    oversized = deepcopy(view['item'])
    oversized['configuration']['buy_rules'] = [
        {'id': f'price-{index}', 'type': 'price_below', 'value': 140.0} for index in range(201)]
    dialog = RulesEditorDialog(local_client, oversized, view['revision'])
    assert not dialog.preview_button.isEnabled() and not dialog.save_button.isEnabled()
    assert '不会保存不完整规则' in dialog.message.text()
    before = list(local_client.commands)
    dialog._request_preview()
    assert local_client.commands == before
    dialog.close()


def test_list_paging_search_and_selected_rules_are_loaded_separately(qt_app, local_client):
    document = yaml.safe_load(local_client.rules.read_text())
    for index in range(25):
        document['watchlist'][f'STOCK{index:02}'] = {'name': f'Company {index}', 'market': 'US',
            'currency': 'USD', 'enabled': False, 'buy_rules': [
                {'id': f'price-{index}-{rule}', 'type': 'price_below', 'value': 10.0} for rule in range(3)]}
    local_client.rules.write_text(yaml.safe_dump(document), encoding='utf-8')
    page = WatchlistPage(local_client)
    page.activate()
    assert page.table.rowCount() == 20 and page.next_button.isEnabled()
    page.next_button.click()
    assert page.table.rowCount() == 6 and page.previous_button.isEnabled() and not page.next_button.isEnabled()
    _select(page, 'US:STOCK24')
    page.rules_button.click()
    assert local_client.commands[-1] == ('watchlist_get', {'key': 'US:STOCK24'})
    assert page._rule_dialog is not None and page._rule_dialog.rules_table.rowCount() == 3
    page._rule_dialog.close()
    page.previous_button.click()
    page.search_input.setText('Company 24')
    page._refresh()
    assert page.table.rowCount() == 1 and page.table.item(0, 0).text() == 'US:STOCK24'
    page.close()


def test_background_page_refresh_preserves_draft_and_its_original_revision(qt_app, local_client):
    page = WatchlistPage(local_client)
    page.activate()
    _select(page, 'US:AAPL')
    original_revision = page._revision
    page.name_input.setText('Unfinished personal name')
    page.research_enabled.setChecked(True)
    local_client.rules.write_bytes(local_client.rules.read_bytes() + b'\n# external modification\n')
    page.activate()
    assert page._revision != original_revision
    assert page.name_input.text() == 'Unfinished personal name' and page.research_enabled.isChecked()
    page.save_button.click()
    assert local_client.commands[-1][1]['expected_revision'] == original_revision
    assert '配置已被其他窗口更新' in page.message.text()
    assert page.name_input.text() == 'Unfinished personal name'
    with ResearchStore(local_client.database) as store:
        assert store.load_settings()['instruments'] == []
    page.close()


def test_legacy_rule_overflow_shows_reason_and_never_opens_partial_editor(qt_app, local_client):
    document = yaml.safe_load(local_client.rules.read_text())
    document['watchlist']['AAPL']['buy_rules'] = [
        {'id': f'legacy-{index}', 'type': 'price_below', 'value': 140.0} for index in range(101)]
    local_client.rules.write_text(yaml.safe_dump(document), encoding='utf-8')
    original = local_client.rules.read_bytes()
    page = WatchlistPage(local_client)
    page.activate()
    _select(page, 'US:AAPL')
    page.rules_button.click()
    assert page._rule_dialog is None and '超出向导上限' in page.message.text()
    assert local_client.rules.read_bytes() == original
    page.close()


def test_new_security_clears_previous_selection_before_opening_rule_editor(qt_app, local_client):
    page = WatchlistPage(local_client)
    page.activate()
    _select(page, 'US:AAPL')
    page.add_button.click()
    assert page._selected_key is None
    page.ticker_input.setText('MSFT')
    page.rules_button.click()
    assert page._rule_dialog is not None and page._rule_dialog.ticker == 'MSFT'
    page._rule_dialog.close()
    page.close()
