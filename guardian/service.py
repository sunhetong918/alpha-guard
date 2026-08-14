"""Background Guardian process entry point."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
from pathlib import Path

from .application import GuardianApplication
from .backend import AlphaGuardBackend
from .autostart.macos import LaunchAgentSpec, disable_launch_agent, enable_launch_agent
from .autostart.windows import (
    build_run_command,
    disable_hkcu_run,
    enable_hkcu_run,
    run_value_name,
)
from .instance import build_instance_names
from .token_store import GuardianTokenStore
from .runtime import GuardianSupervisor
from .transport_qt import (
    GuardianInstanceAlreadyRunning,
    QtGuardianServer,
    QtTransportError,
)

logger = logging.getLogger(__name__)


def main() -> int:
    """Run one foreground Guardian managed by the operating system."""

    arguments = list(sys.argv[1:])
    if arguments in (["--help"], ["-h"]):
        print("usage: alpha-guard-guardian [--help] [--version]")
        print("Run the per-user Alpha Guard background Guardian.")
        return 0
    if arguments == ["--version"]:
        try:
            from importlib.metadata import version

            print(version("alpha-guard"))
        except Exception:  # noqa: BLE001 - version fallback is non-sensitive
            print("0.3.0-dev")
        return 0
    if arguments:
        print("alpha-guard-guardian: unsupported arguments", file=sys.stderr)
        return 2

    try:
        from PySide6.QtCore import QCoreApplication, QTimer
    except (ImportError, ModuleNotFoundError):
        print(
            "Alpha Guard desktop support is not installed; install the desktop extra.",
            file=sys.stderr,
        )
        return 2

    qt = QCoreApplication.instance() or QCoreApplication(sys.argv)
    stopping = False
    restart_requested = False

    def request_stop() -> None:
        nonlocal stopping
        stopping = True
        QTimer.singleShot(0, qt.quit)

    def request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        QTimer.singleShot(0, qt.quit)

    runtime: GuardianSupervisor | None = None
    server: QtGuardianServer | None = None
    try:
        token = GuardianTokenStore().load_or_create()
        runtime = GuardianSupervisor(notify=_notifications_configured())
        backend = AlphaGuardBackend(
            request_stop=request_stop,
            request_restart=request_restart,
            runtime=runtime,
            autostart_apply=_apply_launch_at_login,
        )
        application = GuardianApplication(token, backend=backend)
        names = build_instance_names()
        server = QtGuardianServer(names.guardian, application.handle_json)
        # The local endpoint is the single-instance arbiter.  It must be
        # acquired before the scheduler can touch SQLite or external channels.
        server.start()
        runtime.start()
    except GuardianInstanceAlreadyRunning:
        return 0
    except (OSError, QtTransportError):
        if server is not None:
            server.stop()
        if runtime is not None:
            runtime.stop()
        logger.error("Guardian startup failed")
        return 1
    except Exception:  # noqa: BLE001 - startup boundary must not leak secrets
        if server is not None:
            server.stop()
        if runtime is not None:
            runtime.stop()
        logger.error("Guardian runtime startup failed")
        return 1

    try:
        exit_code = int(qt.exec())
    finally:
        assert server is not None
        assert runtime is not None
        server.stop()
        runtime.stop()
    if restart_requested:
        return 75
    if stopping:
        return 0
    return exit_code


def _notifications_configured() -> bool:
    from config import get_settings

    settings = get_settings()
    return bool(settings.notifications_enabled or settings.whatsapp_enabled)


def _guardian_executable() -> Path:
    executable = shutil.which("alpha-guard-guardian")
    if executable is None:
        executable = os.path.abspath(sys.argv[0])
    return Path(executable).resolve()


def _apply_launch_at_login(enabled: bool) -> None:
    system = platform.system()
    executable = _guardian_executable()
    if system == "Darwin":
        spec = LaunchAgentSpec(executable=executable, arguments=())
        if enabled:
            enable_launch_agent(spec)
        else:
            disable_launch_agent(spec.label)
        return
    if system == "Windows":
        value_name = run_value_name()
        if enabled:
            enable_hkcu_run(value_name, build_run_command(str(executable), ()))
        else:
            disable_hkcu_run(value_name)
        return
    raise OSError("launch-at-login is unsupported on this platform")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
