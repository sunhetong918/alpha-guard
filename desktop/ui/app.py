"""Production-default bootstrap for the desktop App and explicit demo mode."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from guardian.instance import build_instance_names
from guardian.token_store import GuardianTokenStore

from .client import (
    FixtureGuardianClient,
    GuardianClient,
    LocalSocketGuardianClient,
    ManagedGuardianClient,
)
from .theme import apply_theme
from .window import MainWindow


def create_application(
    argv: Sequence[str] | None = None,
    *,
    client: GuardianClient | None = None,
    auto_refresh: bool = True,
) -> tuple[QApplication, MainWindow]:
    """Create the desktop app; an omitted client means production Guardian."""

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    existing = QApplication.instance()
    if existing is None:
        app = QApplication(list(argv if argv is not None else sys.argv))
    elif isinstance(existing, QApplication):
        app = existing
    else:
        raise RuntimeError("a non-widget QGuiApplication already exists")
    app.setApplicationName("Alpha Guard")
    app.setApplicationDisplayName("Alpha Guard · 可信沉默值班台")
    app.setOrganizationName("Alpha Guard")
    apply_theme(app)
    resolved_client = client or create_production_client()
    window = MainWindow(resolved_client, auto_refresh=auto_refresh)
    return app, window


def create_production_client() -> GuardianClient:
    """Build the trusted local IPC boundary without exposing its token to UI."""

    names = build_instance_names()
    runtime_token = GuardianTokenStore().load_or_create()
    transport = LocalSocketGuardianClient(names.guardian, runtime_token)
    return ManagedGuardianClient(transport)


def run(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv if argv is not None else sys.argv)
    parser = argparse.ArgumentParser(
        prog=raw_args[0] if raw_args else "alpha-guard-desktop",
        description="Alpha Guard 本地可信沉默值班台",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="显式使用离线 fixture；不连接或启动 Guardian",
    )
    options, qt_args = parser.parse_known_args(raw_args[1:])
    application_args = [raw_args[0], *qt_args] if raw_args else qt_args
    # Construct production QObjects only after QApplication exists inside
    # ``create_application``. Demo remains an explicit injected adapter.
    client: GuardianClient | None = (
        FixtureGuardianClient() if options.demo else None
    )
    app, window = create_application(application_args, client=client)
    window.show()
    return app.exec()
