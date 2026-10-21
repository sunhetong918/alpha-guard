import copy

import pytest
from PySide6.QtWidgets import QApplication

from desktop.ui.client import GuardianClient
from desktop.ui.models import ActionReceipt
from desktop.ui.portfolio_page import PortfolioPage
from research.portfolio import execute_portfolio


@pytest.fixture(scope='module')
def app():
    yield QApplication.instance() or QApplication(['portfolio-ui'])


class Client(GuardianClient):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.commands = []

    def research_command(self, command, fields=None):
        self.commands.append((command, copy.deepcopy(fields or {})))

    def reply(self):
        command, fields = self.commands[-1]
        result = execute_portfolio(self.path, command, fields)
        self.action_completed.emit(ActionReceipt('research-' + command, True, '', 'r', result))


def test_full_desktop_import_confirmation_save_reload_and_undo(app, tmp_path):
    client = Client(tmp_path / 'state.db')
    page = PortfolioPage(client)
    assert client.commands == []
    page.activate()
    client.reply()
    page.example_button.click()
    page.preview_button.click()
    assert '完整' in page.message.text() and len(client.commands) == 1
    page.full_snapshot.setChecked(True)
    page.preview_button.click()
    client.reply()
    assert '未识别证券' in page.message.text() and not page.commit_button.isEnabled()
    page.confirm_unknown.setChecked(True)
    page.preview_button.click()
    client.reply()
    assert page.commit_button.isEnabled()
    page.commit_button.click()
    assert not page.csv_text.isEnabled()
    client.reply()
    assert page.csv_text.isEnabled() and page.undo_button.isEnabled()
    assert page.table.rowCount() == 2
    page.read_button.click()
    client.reply()
    assert page.table.rowCount() == 2
    page.undo_button.click()
    client.reply()
    assert page.table.rowCount() == 0
    assert page.csv_text.toPlainText()
    page.close()


def test_stale_preview_and_failed_request_preserve_draft(app, tmp_path):
    client = Client(tmp_path / 'state.db')
    page = PortfolioPage(client)
    page.example_button.click()
    page.full_snapshot.setChecked(True)
    page.preview_button.click()
    page.csv_text.setPlainText(page.csv_text.toPlainText().replace('10,100', '20,100'))
    client.reply()
    assert not page.commit_button.isEnabled() and '改变' in page.message.text()
    page.preview_button.click()
    client.request_failed.emit('research-portfolio_preview', 'unavailable')
    assert '20,100' in page.csv_text.toPlainText() and not page.commit_button.isEnabled()
    page.close()
