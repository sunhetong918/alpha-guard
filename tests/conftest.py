"""Shared test hygiene: deterministic Qt teardown and lifecycle-leak detection.

PySide6 在解释器关闭阶段的销毁顺序不可控：如果测试创建的顶层窗口活到进程退出，
QApplication 与残留窗口之间的析构顺序是随机的，会触发
"shared QObject was deleted directly" 警告，并在 Linux 上升级为收尾段错误。
每个测试结束后显式关闭并 deleteLater 所有顶层窗口，把每个窗口的生命周期
限定在它的测试之内；会话结束时再兜底刷新一次。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

_LIFECYCLE_MARKER = "deleted directly"
_QT_LIFECYCLE_MESSAGES: list[str] = []


def _flush_toplevel_widgets() -> None:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for _ in range(3):
        for widget in list(app.topLevelWidgets()):
            try:
                widget.close()
                widget.deleteLater()
            except RuntimeError:
                # C++ 对象已被销毁（例如 WA_DeleteOnClose 已生效），跳过。
                continue
        app.processEvents()


def _record_qt_message(msg_type, context, message) -> None:
    if _LIFECYCLE_MARKER in message:
        _QT_LIFECYCLE_MESSAGES.append(str(message))
    else:
        sys.stderr.write(f"{message}\n")


@pytest.fixture(scope="session", autouse=True)
def _qt_session_hygiene() -> Iterator[None]:
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except ImportError:
        yield
        return
    previous = qInstallMessageHandler(_record_qt_message)
    yield
    _flush_toplevel_widgets()
    qInstallMessageHandler(previous)


@pytest.fixture(autouse=True)
def _qt_test_hygiene() -> Iterator[None]:
    yield
    _flush_toplevel_widgets()
    leaked = list(_QT_LIFECYCLE_MESSAGES)
    if leaked:
        _QT_LIFECYCLE_MESSAGES.clear()
        pytest.fail(
            "Qt 在本测试期间直接删除了共享 QObject（生命周期泄漏）："
            + "；".join(leaked)
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _flush_toplevel_widgets()
    if _QT_LIFECYCLE_MESSAGES:
        try:
            session.testsfailed += 1
        except AttributeError:
            pass


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    if _QT_LIFECYCLE_MESSAGES:
        terminalreporter.write_sep(
            "=", "Qt 生命周期泄漏（共享 QObject 被直接删除）", red=True
        )
        for message in _QT_LIFECYCLE_MESSAGES:
            terminalreporter.write_line(message)
        _QT_LIFECYCLE_MESSAGES.clear()
