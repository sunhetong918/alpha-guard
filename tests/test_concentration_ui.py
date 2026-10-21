from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtWidgets import QApplication

from desktop.ui.client import GuardianClient
from desktop.ui.models import ActionReceipt
from desktop.ui.portfolio_page import PortfolioPage
from research.concentration import ConcentrationStore, execute_concentration
from research.portfolio import execute_portfolio

NOW = datetime.now(UTC) - timedelta(hours=1)


@pytest.fixture(scope='module')
def app():
    yield QApplication.instance() or QApplication(['concentration-ui'])


class Client(GuardianClient):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.commands = []

    def research_command(self, command, fields=None):
        self.commands.append((command, dict(fields or {})))

    def reply(self):
        command, fields = self.commands[-1]
        executor = (execute_concentration if command.startswith('concentration_')
                    else execute_portfolio)
        result = executor(self.path, command, fields)
        self.action_completed.emit(ActionReceipt('research-' + command, True, '', 'r', result))


def _save_snapshot(path):
    store = ConcentrationStore(path)
    fields = {'account': '我的持仓', 'snapshot_date': '2026-10-20', 'source_label': 'h.csv',
              'csv_text': 'market,ticker,quantity,unit_cost,cost_currency\nUS,AAPL,10,,\nHK,00700,100,,\n',
              'mapping': dict(zip(('market', 'ticker', 'quantity', 'unit_cost', 'cost_currency'),
                                  ('market', 'ticker', 'quantity', 'unit_cost', 'cost_currency'))),
              'full_snapshot': True, 'confirmed_instruments': ['US:AAPL', 'HK:00700']}
    preview = store.preview(fields)
    store.commit({**fields, 'expected_revision': preview['revision'],
                  'preview_token': preview['preview_token']})
    return store


def test_concentration_panel_settings_get_and_missing_rendering(app, tmp_path):
    path = tmp_path / 'state.db'
    _save_snapshot(path)
    client = Client(path)
    page = PortfolioPage(client)
    page.base_currency.setCurrentIndex(1)  # USD
    page.single_threshold.setValue(30)
    page.top_n.setValue(2)
    page.save_options.click()
    client.reply()
    assert '已保存' in page.concentration_message.text()
    page.load_concentration.click()
    client.reply()
    assert page.concentration_table.rowCount() == 2
    statuses = {page.concentration_table.item(row, 7).text() for row in range(2)}
    assert statuses == {'缺价'}
    assert '已估值 0/2' in page.concentration_summary.text() and '已估值部分' in page.concentration_summary.text()
    assert any('不可估' in line for line in page.concentration_message.text().splitlines())
    page.close()


def test_concentration_panel_renders_estimated_weights_after_quotes(app, tmp_path):
    path = tmp_path / 'state.db'
    store = _save_snapshot(path)
    store.save_options({'base_currency': 'USD', 'top_n': 5})
    from research.concentration import compute  # noqa: F401  (sanity import)
    stamp = NOW.isoformat()
    for key, price, currency in (('US:AAPL', 200, 'USD'), ('HK:00700', 300, 'HKD')):
        store._store_quote(key, {'price': price, 'currency': currency, 'as_of': stamp,
                                 'observed_at': stamp, 'provider': 'stub', 'quality_issues': []})
    store._store_fx('HKDUSD', 0.128, {'as_of': stamp, 'observed_at': stamp, 'provider': 'stub'})
    store._connection.commit()
    client = Client(path)
    page = PortfolioPage(client)
    page.load_concentration.click()
    client.reply()
    assert page.concentration_table.rowCount() == 2
    texts = {page.concentration_table.item(row, 0).text(): page.concentration_table.item(row, 7).text()
             for row in range(2)}
    assert texts == {'HK:00700': '已估值', 'US:AAPL': '已估值'}
    assert page.currency_table.rowCount() == 2  # 按报价币种（USD/HKD）列出构成，附非敞口提示
    assert '缺汇率' not in page.concentration_message.text()
    page.close()
