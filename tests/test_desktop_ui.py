from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from desktop.ui.app import create_application
from desktop.ui.client import FixtureGuardianClient, LocalSocketGuardianClient
from desktop.ui.client import GuardianClient, ManagedGuardianClient
from desktop.ui.models import (
    ChannelKind,
    DashboardSnapshot,
    PayloadError,
    StatusColor,
    public_config_update,
)
from desktop.ui.theme import APP_QSS
from desktop.ui.widgets import StatusBadge
from guardian.application import GuardianApplication, GuardianBackend
from guardian.transport_qt import QtGuardianServer


FIXTURE = Path("desktop/ui/fixtures/guardian.json")
TOKEN = "a" * 43


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(["alpha-guard-ui-tests"])
    assert isinstance(app, QApplication)
    return app


def _wait_until(
    app: QApplication,
    condition,
    *,
    timeout_ms: int = 3_000,
) -> bool:
    if condition():
        return True
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    poll = QTimer()
    poll.setInterval(10)

    def check() -> None:
        if condition():
            loop.quit()

    poll.timeout.connect(check)
    timer.start(timeout_ms)
    poll.start()
    loop.exec()
    poll.stop()
    app.processEvents()
    return bool(condition())


def _fixture_payload() -> dict:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _fixture_snapshot() -> DashboardSnapshot:
    payload = _fixture_payload()
    return DashboardSnapshot.from_payloads(
        health=payload["health"],
        cockpit=payload["cockpit"],
        config=payload["config"],
        incidents=payload["incidents"],
        providers=payload["providers"],
    )


def test_fixture_maps_to_typed_redacted_dashboard() -> None:
    snapshot = _fixture_snapshot()

    assert snapshot.health.source == "fixture"
    assert snapshot.cockpit.receipt_id == "CPT-20260812-112600"
    assert snapshot.cockpit.overall_color is StatusColor.AMBER
    assert {asset.symbol for asset in snapshot.assets} == {
        "AAPL",
        "MSFT",
        "NVDA",
        "00700",
        "09988",
    }
    assert {channel.kind for channel in snapshot.channels} == set(ChannelKind)
    assert snapshot.channels[0].recipient_hint.endswith("4821")
    rendered = FIXTURE.read_text(encoding="utf-8").lower()
    assert "bot_token" not in rendered
    assert "access_token" not in rendered
    assert "chat_id" not in rendered
    assert "sqlite" not in rendered


def test_invalid_or_naive_timestamps_fail_closed() -> None:
    payload = _fixture_payload()
    payload["cockpit"]["generated_at"] = "2026-08-12T03:26:00"

    with pytest.raises(PayloadError, match="timestamp_offset"):
        DashboardSnapshot.from_payloads(
            health=payload["health"],
            cockpit=payload["cockpit"],
            config=payload["config"],
            incidents=payload["incidents"],
            providers=payload["providers"],
        )


def test_public_config_update_cannot_carry_channel_secrets() -> None:
    snapshot = _fixture_snapshot()
    update = public_config_update(snapshot.preferences, snapshot.config_revision)

    assert set(update) == {"revision", "preferences"}
    rendered = json.dumps(update, sort_keys=True).lower()
    for forbidden in ("token", "secret", "chat_id", "phone_number", "sqlite"):
        assert forbidden not in rendered


def test_five_status_badges_have_text_not_color_alone(qt_app: QApplication) -> None:
    badges = [StatusBadge(color) for color in StatusColor]

    assert len(badges) == 5
    for badge, color in zip(badges, StatusColor, strict=True):
        assert badge.property("statusColor") == color.value
        assert color.value not in badge.text()
        assert "状态：" in badge.accessibleName()
        assert len(badge.text()) > 5
    assert all(f'QLabel[statusColor="{color.value}"]' in APP_QSS for color in StatusColor)
    qt_app.processEvents()


def test_fixture_window_loads_overview_and_navigates_all_pages(
    qt_app: QApplication,
) -> None:
    client = FixtureGuardianClient(FIXTURE)
    app, window = create_application(
        ["alpha-guard-ui-tests"], client=client, auto_refresh=True
    )
    assert app is qt_app
    window.show()

    assert _wait_until(qt_app, lambda: window.snapshot is not None)
    assert window.stack.count() == 5
    assert "局部退化" in window.overall_badge.text()
    assert window.overview_page.receipt_id.text().endswith("CPT-20260812-112600")
    for index, key in enumerate(
        ("overview", "assets", "incidents", "providers", "settings")
    ):
        window.sidebar.nav_buttons[key].click()
        assert window.stack.currentIndex() == index
    assert window.assets_page.table.rowCount() == 5
    assert window.providers_page.table.rowCount() == 4
    assert set(window.settings_page.channel_cards) == set(ChannelKind)
    window.close()
    qt_app.processEvents()


def test_overview_and_navigation_reflow_for_compact_desktop(
    qt_app: QApplication,
) -> None:
    client = FixtureGuardianClient(FIXTURE)
    _app, window = create_application(
        ["alpha-guard-ui-tests"], client=client, auto_refresh=False
    )
    window.resize(1000, 700)
    window.show()
    qt_app.processEvents()

    assert window.sidebar.width() == 78
    assert window.sidebar.brand.text() == "AG"
    assert window.overview_page._compact is True
    window.resize(1360, 820)
    qt_app.processEvents()
    assert window.sidebar.width() == 218
    assert window.overview_page._compact is False
    window.close()


class _FakeGuardianClient(GuardianClient):
    def __init__(self) -> None:
        super().__init__()
        self.refresh_count = 0
        self.closed = False

    def refresh(self) -> None:
        self.refresh_count += 1

    def request_scan(self) -> None:
        pass

    def test_channel(self, channel: ChannelKind) -> None:
        del channel

    def update_preferences(self, preferences, *, revision: int) -> None:
        del preferences, revision

    def close(self) -> None:
        self.closed = True


def test_managed_client_starts_guardian_once_and_reconnects_bounded(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _FakeGuardianClient()
    launches: list[tuple[str, list[str]]] = []

    class FakeProcess:
        @staticmethod
        def startDetached(program: str, arguments: list[str]):  # noqa: N802
            launches.append((program, arguments))
            return True, 4312

    monkeypatch.setattr("desktop.ui.client.QProcess", FakeProcess)
    monkeypatch.setattr(ManagedGuardianClient, "_RECONNECT_DELAYS_MS", (1, 1))
    managed = ManagedGuardianClient(transport)
    failures: list[tuple[str, str]] = []
    managed.request_failed.connect(lambda purpose, code: failures.append((purpose, code)))

    managed.refresh()
    transport.request_failed.emit("refresh", "guardian_unavailable")
    assert _wait_until(qt_app, lambda: transport.refresh_count == 2)
    transport.request_failed.emit("refresh", "guardian_unavailable")
    assert _wait_until(qt_app, lambda: transport.refresh_count == 3)
    transport.request_failed.emit("refresh", "guardian_unavailable")

    assert launches == [("alpha-guard-guardian", [])]
    assert failures == [("refresh", "guardian_unavailable")]
    managed.close()
    assert transport.closed is True


def test_create_application_defaults_to_production_client(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from desktop.ui import app as app_module

    production = _FakeGuardianClient()
    calls: list[bool] = []

    def factory() -> GuardianClient:
        calls.append(True)
        return production

    monkeypatch.setattr(app_module, "create_production_client", factory)
    app, window = app_module.create_application(
        ["alpha-guard-desktop"], auto_refresh=False
    )
    assert app is qt_app
    assert window.client is production
    assert calls == [True]
    window.close()


def test_run_uses_fixture_only_with_explicit_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.ui import app as app_module

    captured: list[GuardianClient] = []

    class FakeApp:
        def exec(self) -> int:
            return 17

    class FakeWindow:
        def show(self) -> None:
            pass

    def create(argv, *, client, auto_refresh=True):
        del argv, auto_refresh
        captured.append(client)
        return FakeApp(), FakeWindow()

    monkeypatch.setattr(app_module, "create_application", create)
    monkeypatch.setattr(
        app_module,
        "create_production_client",
        lambda: pytest.fail("demo must not touch production token/IPC"),
    )

    assert app_module.run(["alpha-guard-desktop", "--demo"]) == 17
    assert len(captured) == 1
    assert isinstance(captured[0], FixtureGuardianClient)


class _FixtureBackend(GuardianBackend):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def _result(self, method: str, value: object) -> object:
        self.calls.append(method)
        return value

    def health_get(self, params, context):
        del context
        assert params == {}
        return self._result("health.get", self.payload["health"])

    def cockpit_get(self, params, context):
        del context
        assert params == {}
        cockpit = dict(self.payload["cockpit"])
        cockpit["incidents"] = self.payload["incidents"]
        cockpit["providers"] = self.payload["providers"]
        return self._result("cockpit.get", cockpit)

    def config_get(self, params, context):
        del context
        assert params == {}
        return self._result("config.get", self.payload["config"])

    def incidents_list(self, params, context):
        del context
        assert params == {"limit": 50}
        return self._result("incidents.list", self.payload["incidents"])

    def providers_list(self, params, context):
        del context
        assert params == {}
        return self._result("providers.list", self.payload["providers"])

    def runs_list(self, params, context):
        del context
        assert params == {"limit": 50}
        return self._result("runs.list", {"items": []})


def test_local_socket_client_uses_canonical_json_rpc_without_blocking_ui(
    qt_app: QApplication,
) -> None:
    backend = _FixtureBackend(_fixture_payload())
    application = GuardianApplication(TOKEN, backend=backend)
    server_name = f"alpha-guard.ui.{uuid.uuid4().hex[:12]}"
    server = QtGuardianServer(server_name, application.handle_json)
    server.start()
    client = LocalSocketGuardianClient(server_name, TOKEN, request_timeout_ms=2_000)
    snapshots: list[DashboardSnapshot] = []
    errors: list[tuple[str, str]] = []
    ui_ticks: list[bool] = []
    client.snapshot_ready.connect(snapshots.append)
    client.request_failed.connect(lambda purpose, code: errors.append((purpose, code)))
    QTimer.singleShot(0, lambda: ui_ticks.append(True))

    try:
        client.refresh()
        assert _wait_until(qt_app, lambda: bool(snapshots) or bool(errors), timeout_ms=4_000)
        assert errors == []
        assert ui_ticks == [True]
        assert snapshots[0].cockpit.receipt_id == "CPT-20260812-112600"
        assert set(backend.calls) == {
            "health.get",
            "cockpit.get",
            "config.get",
            "runs.list",
            "incidents.list",
            "providers.list",
        }
        assert client._transport.pending_request_ids == ()
    finally:
        client.close()
        server.stop()
        qt_app.processEvents()


def test_ui_modules_do_not_import_state_store_or_notification_secrets() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(Path("desktop/ui").glob("*.py"))
    )
    assert "from state" not in source
    assert "import state" not in source
    assert "StateStore" not in source
    assert "TELEGRAM_BOT_TOKEN" not in source
    assert "WHATSAPP_ACCESS_TOKEN" not in source
    assert "sqlite3" not in source
