from __future__ import annotations

from copy import deepcopy

import pytest
from PySide6.QtWidgets import QApplication

from desktop.ui.client import FixtureGuardianClient
from desktop.ui.models import ActionReceipt
from desktop.ui.window import MainWindow


@pytest.fixture(scope='module')
def qt_app():
    app = QApplication.instance() or QApplication(['research-close-tests'])
    return app


class CloseClient(FixtureGuardianClient):
    def __init__(self):
        super().__init__()
        self.reads = 0
        self.commands = []
        self.closed = False

    def refresh_research(self):
        self.reads += 1

    def research_command(self, command, fields=None):
        self.commands.append(command)

    def close(self):
        self.closed = True

    def state(self, value):
        payload = deepcopy(self._research_payload)
        payload['fixture'] = False
        payload['runtime']['state'] = value
        self.research_ready.emit(payload)

    def acknowledge_stop(self, accepted=True):
        self.action_completed.emit(ActionReceipt('research-stop', accepted, '操作已接受', 'stop-test'))


def setup_window(qt_app, state='RUNNING'):
    client = CloseClient()
    window = MainWindow(client, auto_refresh=False)
    window.show()
    if state is not None:
        client.state(state)
    qt_app.processEvents()
    return window, client


def choose(window, text):
    assert window._close_dialog is not None
    next(button for button in window._close_dialog.buttons() if button.text() == text).click()


def cleanup(window):
    if window._close_dialog is not None:
        window._close_dialog.escapeButton().click()
    window._close_allowed = True
    window.close()


def test_stopped_research_closes_without_requests(qt_app):
    window, client = setup_window(qt_app, 'STOPPED')
    window.close()
    assert client.closed and not window.isVisible()
    assert client.commands == [] and client.reads == 0


def test_unknown_research_state_is_read_before_closing(qt_app):
    window, client = setup_window(qt_app, None)
    window.close()
    window.close()
    assert window.isVisible() and client.reads == 1 and not client.closed
    client.state('STOPPED')
    qt_app.processEvents()
    assert client.closed and not window.isVisible()
    assert client.commands == []


def test_unknown_running_state_opens_default_cancel_dialog(qt_app):
    window, client = setup_window(qt_app, None)
    try:
        window.close()
        client.state('RUNNING')
        qt_app.processEvents()
        dialog = window._close_dialog
        assert dialog.defaultButton().text() == '取消'
        assert dialog.escapeButton().text() == '取消'
        assert '后台' in dialog.text()
        choose(window, '取消')
        assert window.isVisible() and not client.closed
        assert client.commands == []
    finally:
        cleanup(window)


@pytest.mark.parametrize('state', ['RUNNING', 'STOPPING'])
def test_continue_background_only_closes_window(qt_app, state):
    window, client = setup_window(qt_app, state)
    window.close()
    choose(window, '继续后台')
    assert client.closed and not window.isVisible()
    assert client.commands == [] and client.reads == 0


def test_stop_waits_for_acknowledged_stopped_state(qt_app):
    window, client = setup_window(qt_app)
    window.close()
    choose(window, '停止研究后关闭')
    window.close()
    assert client.commands == ['stop'] and window.isVisible() and not client.closed
    client.state('STOPPED')  # An earlier snapshot is not a stop acknowledgement.
    qt_app.processEvents()
    assert window.isVisible()
    client.acknowledge_stop()
    assert client.reads == 1
    client.state('STOPPING')
    assert window.isVisible() and not client.closed
    window._close_poll.timeout.emit()
    client.state('STOPPED')
    qt_app.processEvents()
    assert client.closed and not window.isVisible()


@pytest.mark.parametrize('failure', ['request', 'rejected', 'timeout'])
def test_failed_stop_retains_window_and_explains_uncertainty(qt_app, failure):
    window, client = setup_window(qt_app)
    try:
        window.close()
        choose(window, '停止研究后关闭')
        if failure == 'request':
            client.request_failed.emit('research-stop', 'request_timeout')
        elif failure == 'rejected':
            client.acknowledge_stop(False)
        else:
            window._close_timer.timeout.emit()
        assert window.isVisible() and not client.closed
        assert window._close_wait is None and not window._close_poll.isActive()
        assert '窗口保持打开' in window.statusBar().currentMessage()
        assert window.research_page.isEnabled()
        client.state('STOPPED')  # A later unsolicited snapshot cannot resume a failed close.
        qt_app.processEvents()
        assert window.isVisible()
    finally:
        cleanup(window)


def test_pending_start_also_requires_close_decision(qt_app):
    window, client = setup_window(qt_app, 'STOPPED')
    try:
        window.research_page._send('start')
        window.close()
        assert window._close_dialog is not None
        choose(window, '取消')
        assert window.isVisible() and client.commands == ['start']
    finally:
        cleanup(window)


def test_stop_waits_for_pending_launch_before_sending_stop(qt_app):
    window, client = setup_window(qt_app, 'STOPPED')
    try:
        window.research_page._send('start')
        window.close()
        choose(window, '停止研究后关闭')
        assert client.commands == ['start'] and window._close_wait == 'launch'
        client.state('STOPPED')
        assert window.isVisible()
        client.action_completed.emit(ActionReceipt('research-start', True, '操作已接受', 'start-test'))
        assert client.commands == ['start', 'stop']
        client.acknowledge_stop()
        client.state('STOPPED')
        qt_app.processEvents()
        assert client.closed
    finally:
        cleanup(window)


def test_old_reads_drain_before_stop_confirmation_snapshot(qt_app):
    window, client = setup_window(qt_app)
    window.close()
    choose(window, '停止研究后关闭')
    client.busy_changed.emit(True)
    client.acknowledge_stop()
    assert client.reads == 0
    client.state('STOPPED')  # May belong to a request sent before stop.
    qt_app.processEvents()
    assert window.isVisible() and not client.closed
    client.busy_changed.emit(False)
    assert client.reads == 1
    client.state('STOPPED')
    qt_app.processEvents()
    assert client.closed


def test_unknown_state_read_failure_never_silently_closes(qt_app):
    window, client = setup_window(qt_app, None)
    try:
        window.close()
        client.request_failed.emit('research-get', 'guardian_unavailable')
        assert window.isVisible() and not client.closed and client.commands == []
        assert '窗口保持打开' in window.statusBar().currentMessage()
    finally:
        cleanup(window)


@pytest.mark.parametrize('failure', ['read_failed', 'timeout'])
def test_disconnected_window_can_close_with_explicit_unknown_state_warning(qt_app, failure):
    window, client = setup_window(qt_app, None)
    window.close()
    if failure == 'read_failed':
        client.request_failed.emit('research-get', 'guardian_unavailable')
    else:
        window._close_timer.timeout.emit()
    window.close()
    dialog = window._close_dialog
    assert dialog.defaultButton().text() == '保持打开'
    assert '不会停止可能存在的后台研究' in dialog.text()
    choose(window, '保持打开')
    assert window.isVisible() and not client.closed
    window.close()
    choose(window, '仍然关闭')
    assert not window.isVisible() and client.closed
    assert client.reads == 1 and client.commands == []
