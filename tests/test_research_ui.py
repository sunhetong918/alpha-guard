from __future__ import annotations

import copy
import uuid

import pytest
from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWidgets import QApplication

from desktop.ui.client import (
    FixtureGuardianClient,
    GuardianClient,
    LocalSocketGuardianClient,
    ManagedGuardianClient,
)
from desktop.ui.models import ActionReceipt
from desktop.ui.research_page import ResearchPage, _safe_url, _source_link
from desktop.ui.window import MainWindow
from guardian.application import GuardianApplication, GuardianBackend
from guardian.transport_qt import QtGuardianServer


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(["research-ui-tests"])
    yield app


def _wait(app, condition):
    if condition():
        return True
    loop = QEventLoop()
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    poll = QTimer()
    poll.setInterval(10)
    poll.timeout.connect(lambda: loop.quit() if condition() else None)
    timeout.start(3000)
    poll.start()
    loop.exec()
    poll.stop()
    app.processEvents()
    return bool(condition())


def _snapshot(**overrides):
    value = {
        "runtime": {
            "state": "STOPPED",
            "mode": None,
            "stage": "idle",
            "next_run_at": None,
            "last_run": None,
        },
        "settings": {
            "markets": ["US", "HK"],
            "horizons": ["swing", "long"],
            "instruments": [{"market": "US", "ticker": "AAPL", "name": "Apple"}],
            "interval_minutes": 15,
            "ai_enabled": False,
            "news_enabled": True,
            "news_sources": [],
            "max_items": 20,
            "max_ai_calls": 10,
        },
        "subscriptions": [],
        "evidence": [],
        "reports": [],
        "runs": [],
    }
    value.update(overrides)
    return value


class FakeResearchClient(GuardianClient):
    def __init__(self):
        super().__init__()
        self.reads = 0
        self.commands = []
        self.details = []
        self.detail_offsets = []

    def refresh(self):
        pass

    def refresh_research(self):
        self.reads += 1

    def research_command(self, command, fields=None):
        self.commands.append((command, copy.deepcopy(fields or {})))

    def research_detail(self, kind, identity, *, offset=0, segment_offset=0):
        self.details.append((kind, identity))
        self.detail_offsets.append((offset, segment_offset))


def _loaded_page(qt_app):
    client = FakeResearchClient()
    page = ResearchPage(client)
    client.research_ready.emit(_snapshot())
    return client, page


def test_page_activation_reads_only_and_never_starts_collection(qt_app):
    client = FakeResearchClient()
    window = MainWindow(client, auto_refresh=False)
    assert client.reads == 0
    assert client.commands == []

    window.show_page("research")
    assert client.reads == 1
    assert window.scan_button.isHidden()
    client.research_ready.emit(_snapshot())
    assert "已停止" in window.research_page.runtime_label.text()
    assert window.research_page._timer.isActive() is False
    assert client.commands == []
    window.show_page("overview")
    assert not window.scan_button.isHidden()
    window.close()


def test_settings_roundtrip_supports_all_markets_and_horizons(qt_app):
    client, page = _loaded_page(qt_app)
    page.market_checks["CN"].setChecked(True)
    page.horizon_checks["short"].setChecked(True)
    page.instruments_input.setText("US:AAPL,HK:00700,CN:600519")
    page.interval_input.setValue(3)
    page.source_checks["finnhub"].setChecked(True)
    page.ai_check.setChecked(True)
    assert not page.once_button.isEnabled()
    page.save_button.click()

    command, fields = client.commands[-1]
    assert command == "configure"
    settings = fields["settings"]
    assert settings["markets"] == ["US", "HK", "CN"]
    assert settings["horizons"] == ["short", "swing", "long"]
    assert [item["ticker"] for item in settings["instruments"]] == [
        "AAPL",
        "00700",
        "600519",
    ]
    assert settings["instruments"][0]["name"] == "Apple"
    assert settings["interval_minutes"] == 3
    assert settings["ai_enabled"] is True
    assert settings["news_sources"] == ["finnhub"]
    assert len(client.commands) == 1
    page.close()


def test_video_transcription_is_opt_in_with_visible_playback_budgets(qt_app):
    client, page = _loaded_page(qt_app)
    assert not page.media_check.isChecked()
    assert client.commands == []
    page.media_check.setChecked(True)
    page.max_media_input.setValue(3)
    page.media_minutes_input.setValue(4)
    page.save_button.click()
    command, fields = client.commands[-1]
    assert command == "configure"
    assert fields["settings"]["media_enabled"] is True
    assert fields["settings"]["max_media_items"] == 3
    assert fields["settings"]["max_media_seconds"] == 240
    assert len(client.commands) == 1
    client.research_ready.emit(_snapshot(runs=[{
        "status": "partial", "stats": {"media_attempts": 2, "transcription_calls": 1, "media_cache_hits": 3}
    }]))
    assert "视频读取尝试 2" in page.run_detail.toPlainText()
    assert "本地转写 1" in page.run_detail.toPlainText()
    assert "复用已有转写 3" in page.run_detail.toPlainText()
    page.close()


def test_accessibility_check_navigation_opens_research_once(qt_app):
    client = FakeResearchClient()
    window = MainWindow(client, auto_refresh=False)
    window.sidebar.nav_buttons["research"].setChecked(True)
    assert window.stack.currentWidget() is window.research_page
    assert client.reads == 1
    assert window.scan_button.isHidden()
    window.sidebar.nav_buttons["overview"].setChecked(True)
    assert window.stack.currentWidget() is window.overview_page
    window.close()


def test_status_refresh_preserves_unsaved_settings_and_import_drafts(qt_app):
    client, page = _loaded_page(qt_app)
    page.instruments_input.setText("HK:00700")
    page.import_text.setPlainText("尚未提交的访谈内容")
    client.research_ready.emit(_snapshot())

    assert page.instruments_input.text() == "HK:00700"
    assert page.import_text.toPlainText() == "尚未提交的访谈内容"
    assert page._dirty is True
    assert client.commands == []
    page.close()


def test_invalid_instrument_is_not_submitted(qt_app):
    client, page = _loaded_page(qt_app)
    page.instruments_input.setText("not a stock")
    page.save_button.click()
    assert client.commands == []
    assert "股票格式" in page.message_label.text()
    page.close()


def test_one_scan_is_explicit_and_stop_remains_available_during_ipc(qt_app):
    client, page = _loaded_page(qt_app)
    page.activate()
    page.once_button.click()
    assert client.commands == [("once", {})]
    assert not page.once_button.isEnabled()
    assert page.stop_button.isEnabled()
    client.busy_changed.emit(True)
    page.stop_button.click()
    assert client.commands[-1] == ("stop", {})
    client.research_ready.emit(
        _snapshot(runtime={"state": "RUNNING", "stage": "collecting"})
    )
    assert page._timer.isActive()
    assert page.stop_button.isEnabled()
    page.deactivate()
    assert not page._timer.isActive()
    page.close()


def test_start_receipt_keeps_controls_locked_until_runtime_refresh(qt_app):
    client, page = _loaded_page(qt_app)
    page.start_button.click()
    client.action_completed.emit(
        ActionReceipt("research-start", True, "accepted", "test")
    )
    assert not page.start_button.isEnabled()
    assert page.stop_button.isEnabled()
    assert page._runtime_state == "RUNNING"
    page.close()


def test_subscriptions_add_pause_and_delete_through_commands(qt_app):
    client, page = _loaded_page(qt_app)
    page.platform_input.setCurrentIndex(page.platform_input.findData("xiaohongshu"))
    page.subscription_url.setText("https://www.xiaohongshu.com/user/profile/example")
    page.subscription_label.setText("访谈作者")
    page.subscription_feed.setText("https://example.com/feed.xml")
    page._subscribe()
    assert client.commands[-1][0] == "subscribe"
    saved = {**client.commands[-1][1]["subscription"], "id": "creator-one"}
    client.research_ready.emit(_snapshot(subscriptions=[saved]))
    page.subscription_table.selectRow(0)
    page.toggle_subscription_button.click()
    assert client.commands[-1] == (
        "subscription",
        {"id": "creator-one", "enabled": False},
    )
    page.delete_subscription_button.click()
    assert client.commands[-1] == ("delete", {"id": "creator-one"})
    page.close()


def test_import_keeps_subtitle_timestamps_and_source(qt_app):
    client, page = _loaded_page(qt_app)
    text = "1\n00:00:01,000 --> 00:00:03,000\n访谈的原始内容\n"
    page.import_text.setPlainText(text)
    page.import_kind.setCurrentIndex(page.import_kind.findData("srt"))
    page.import_url.setText("https://www.youtube.com/watch?v=example")
    page.import_date.setText("2026-09-30T10:00:00+08:00")
    page.import_button.click()

    command, fields = client.commands[-1]
    assert command == "import"
    assert fields["material"]["kind"] == "srt"
    assert "00:00:01,000" in fields["material"]["text"]
    assert fields["material"]["source_url"].startswith("https://www.youtube.com/")
    assert fields["material"]["published_at"] == "2026-09-30T10:00:00+08:00"
    page.close()


def test_media_selection_launches_transcription_only_after_user_selection(
    qt_app, monkeypatch, tmp_path
):
    client, page = _loaded_page(qt_app)
    path = tmp_path / "interview.mp4"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "desktop.ui.research_page.QFileDialog.getOpenFileName",
        lambda *a, **kw: (str(path), ""),
    )
    assert client.commands == []
    page.transcribe_button.click()
    assert client.commands[-1] == (
        "transcribe",
        {"path": str(path), "source_url": "", "title": "interview"},
    )
    page.close()


def test_report_details_show_conditions_quotes_and_time_without_html_execution(qt_app):
    client, page = _loaded_page(qt_app)
    report = {
        "id": "report-one",
        "instrument": {"market": "US", "ticker": "AAPL"},
        "horizon": "swing",
        "action": "watch",
        "status": "ready",
        "summary": "继续核验",
        "conditions": ["等待公告确认"],
        "invalidation": ["需求未兑现"],
        "claims": [
            {
                "kind": "opinion",
                "text": "作者预计需求增长",
                "relationship": "主营业务相关",
                "citation": {
                    "quote": '<script>alert("unsafe")</script>',
                    "source_url": "https://example.com/interview",
                    "start_seconds": 42,
                    "speaker": "主持人",
                },
            }
        ],
    }
    client.research_ready.emit(_snapshot(reports=[report]))
    page.report_table.selectRow(0)
    assert client.details[-1] == ("report", "report-one")
    client.research_detail_ready.emit(
        {"kind": "report", "id": "report-one", "item": report}
    )
    text = page.report_detail.toPlainText()
    assert "等待公告确认" in text and "需求未兑现" in text
    assert "作者观点" in text and "42 秒" in text and "主持人" in text
    assert '<script>alert("unsafe")</script>' in text
    assert "<script>" not in page.report_detail.toHtml()
    assert "https://example.com/interview" in page.report_detail.toHtml()
    before = text
    client.research_detail_ready.emit(
        {"kind": "report", "id": "stale-response", "item": {}}
    )
    assert page.report_detail.toPlainText() == before
    page.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/private",
        "javascript:alert(1)",
        "http://127.0.0.1/",
        "http://[::1]/",
        "https://user:password@example.com/",
        "https://local.internal/",
    ],
)
def test_source_links_reject_unsafe_schemes_addresses_and_credentials(url):
    assert _safe_url(url) is False


def test_source_open_is_user_triggered_and_validated(qt_app, monkeypatch):
    client, page = _loaded_page(qt_app)
    opened = []
    monkeypatch.setattr(
        "desktop.ui.research_page.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )
    page._open_source(QUrl("file:///private/file"))
    assert opened == []
    page._open_source(QUrl("https://example.com/story"))
    assert opened == ["https://example.com/story"]
    page.close()


def test_source_link_preserves_video_id_and_adds_exact_quote_time():
    link = _source_link("https://www.youtube.com/watch?v=interview", start_seconds=42)
    assert "v=interview" in link
    assert "t=42" in link


def test_source_failures_remain_visible_even_when_no_articles_exist(qt_app):
    client, page = _loaded_page(qt_app)
    snapshot = _snapshot(
        runtime={
            "state": "STOPPED",
            "stage": "idle",
            "last_run": {
                "status": "failed",
                "sources": [
                    {
                        "status": "failed",
                        "sources": [
                            {
                                "provider": "finnhub",
                                "status": "failed",
                                "article_count": 0,
                                "error_code": "missing_credentials",
                            }
                        ],
                    }
                ],
            },
        }
    )
    client.research_ready.emit(snapshot)
    text = page.run_detail.toPlainText()
    assert "Finnhub" in text
    assert "尚未配置来源凭据" in text
    assert page.evidence_table.rowCount() == 0
    page.close()


def _detail_page(*, offset=0, segment_offset=0):
    return {
        "id": "large-evidence",
        "title": "完整访谈",
        "completeness": "full",
        "content_text": "甲" * 40_000 if offset == 0 else "乙" * 10_000,
        "content_offset": offset,
        "content_next_offset": 40_000 if offset == 0 else None,
        "segments": [
            {
                "start_seconds": segment_offset + index,
                "text": f"字幕 {segment_offset + index}",
            }
            for index in range(3 if segment_offset == 0 else 2)
        ],
        "segment_offset": segment_offset,
        "segment_next_offset": 3 if segment_offset == 0 else None,
    }


def _show_evidence_page(client, page, item):
    client.research_detail_ready.emit(
        {"kind": "evidence", "id": "large-evidence", "item": item}
    )


def test_long_evidence_content_and_subtitle_pages_have_independent_navigation(qt_app):
    client, page = _loaded_page(qt_app)
    client.research_ready.emit(_snapshot(evidence=[_detail_page()]))
    page.evidence_table.selectRow(0)
    _show_evidence_page(client, page, _detail_page())
    assert page.evidence_view.currentData() == "segment"
    assert "字幕第 1–3 项" in page._page_labels["evidence"].text()

    page.evidence_view.setCurrentIndex(0)
    assert "正文第 1–40000 字" in page._page_labels["evidence"].text()
    page._next_buttons["evidence"].click()
    assert client.detail_offsets[-1] == (40_000, 0)
    assert not page._next_buttons["evidence"].isEnabled()
    _show_evidence_page(client, page, _detail_page())  # late response for first page
    assert "evidence" in page._detail_pending
    _show_evidence_page(client, page, _detail_page(offset=40_000))
    assert "正文第 40001–50000 字" in page._page_labels["evidence"].text()
    assert "已到末段" in page._page_labels["evidence"].text()

    page.evidence_view.setCurrentIndex(1)
    page._next_buttons["evidence"].click()
    assert client.detail_offsets[-1] == (40_000, 3)
    _show_evidence_page(client, page, _detail_page(offset=40_000, segment_offset=3))
    assert "字幕 3" in page.evidence_detail.toPlainText()
    page._previous_buttons["evidence"].click()
    assert client.detail_offsets[-1] == (40_000, 0)
    _show_evidence_page(client, page, _detail_page(offset=40_000))
    assert not page._previous_buttons["evidence"].isEnabled()

    page.evidence_view.setCurrentIndex(0)
    assert page._previous_buttons["evidence"].isEnabled()
    page._previous_buttons["evidence"].click()
    assert client.detail_offsets[-1] == (0, 0)
    _show_evidence_page(client, page, _detail_page())
    assert not page._previous_buttons["evidence"].isEnabled()
    assert client.commands == []
    page.close()


def test_failed_page_read_preserves_display_and_can_retry(qt_app):
    client, page = _loaded_page(qt_app)
    client.research_ready.emit(_snapshot(evidence=[_detail_page()]))
    page.evidence_table.selectRow(0)
    _show_evidence_page(client, page, _detail_page())
    original = page.evidence_detail.toPlainText()
    page._next_buttons["evidence"].click()
    client.request_failed.emit("research-detail", "request_timeout")
    assert page.evidence_detail.toPlainText() == original
    assert page._page_history["segment"] == []
    assert page._reload_buttons["evidence"].isEnabled()
    page._next_buttons["evidence"].click()
    assert client.detail_offsets[-1] == (0, 3)
    _show_evidence_page(client, page, _detail_page(segment_offset=3))
    assert page._previous_buttons["evidence"].isEnabled()
    page.close()


def test_report_claims_have_next_and_previous_pages(qt_app):
    client, page = _loaded_page(qt_app)
    base = {
        "id": "report-pages",
        "instrument": {"market": "US", "ticker": "AAPL"},
        "horizon": "long",
        "status": "ready",
        "action": "watch",
        "conditions": ["验证需求"],
        "claim_offset": 0,
        "claim_next_offset": 20,
        "claims": [{"text": "首组观点"}] * 20,
    }
    client.research_ready.emit(_snapshot(reports=[base]))
    page.report_table.selectRow(0)
    client.research_detail_ready.emit(
        {"kind": "report", "id": "report-pages", "item": base}
    )
    page._next_buttons["report"].click()
    assert client.detail_offsets[-1] == (20, 0)
    second = {
        **base,
        "claim_offset": 20,
        "claim_next_offset": None,
        "claims": [{"text": "最后一条观点"}],
    }
    client.research_detail_ready.emit(
        {"kind": "report", "id": "report-pages", "item": second}
    )
    assert "最后一条观点" in page.report_detail.toPlainText()
    assert "验证需求" in page.report_detail.toPlainText()
    assert "论据第 21–21 项" in page._page_labels["report"].text()
    assert not page._next_buttons["report"].isEnabled()
    page._previous_buttons["report"].click()
    assert client.detail_offsets[-1] == (0, 0)
    page.close()


def test_report_pages_retain_counter_evidence_when_claims_are_empty(qt_app):
    client, page = _loaded_page(qt_app)
    base = {
        "id": "report-without-quotes",
        "instrument": {"market": "US", "ticker": "AAPL"},
        "horizon": "long",
        "status": "insufficient_evidence",
        "action": "watch",
        "claim_offset": 0,
        "claim_next_offset": 10,
        "claims": [],
        "counter_evidence": [f"第一组反面证据 {index}" for index in range(10)],
    }
    client.research_ready.emit(_snapshot(reports=[base]))
    page.report_table.selectRow(0)
    client.research_detail_ready.emit(
        {"kind": "report", "id": base["id"], "item": base}
    )
    assert page._next_buttons["report"].text() == "下一组论据"
    assert "论据第 1–10 项" in page._page_labels["report"].text()
    assert "还有后续内容" in page._page_labels["report"].text()
    page._next_buttons["report"].click()
    assert client.detail_offsets[-1] == (10, 0)
    second = {
        **base,
        "claim_offset": 10,
        "claim_next_offset": None,
        "counter_evidence": ["后续反面证据仍需核验"],
        "conditions": ["第十一项行动条件"],
        "verification_steps": ["检查原始财报"],
    }
    client.research_detail_ready.emit(
        {"kind": "report", "id": base["id"], "item": second}
    )
    text = page.report_detail.toPlainText()
    assert "后续反面证据仍需核验" in text
    assert "第十一项行动条件" in text
    assert "检查原始财报" in text
    assert "第一组反面证据" not in text
    assert "论据第 11–11 项" in page._page_labels["report"].text()
    assert not page._next_buttons["report"].isEnabled()
    page._previous_buttons["report"].click()
    assert client.detail_offsets[-1] == (0, 0)
    assert client.commands == []
    page.close()


def test_missing_model_configuration_has_specific_local_setup_hint(qt_app):
    client, page = _loaded_page(qt_app)
    client.research_ready.emit(
        _snapshot(capabilities={"model_configured": False, "model_installed": True})
    )
    assert "ANTHROPIC_API_KEY" in page.ai_configuration_hint.text()
    assert "勾选" in page.ai_configuration_hint.text()
    client.research_ready.emit(
        _snapshot(capabilities={"model_configured": True, "model_installed": True})
    )
    assert "已就绪" in page.ai_configuration_hint.text()
    assert client.commands == []
    page.close()


class ResearchBackend(GuardianBackend):
    def __init__(self):
        self.calls = []

    def research_get(self, params, context):
        self.calls.append(("research.get", params))
        return _snapshot()

    def research_command(self, params, context):
        self.calls.append(("research.command", params))
        return {
            "accepted": True,
            "command": params["command"],
            "result": {"state": "STOPPED"},
        }

    def research_detail(self, params, context):
        self.calls.append(("research.detail", params))
        return {"id": params["id"], "title": "local evidence"}


class PagedResearchBackend(ResearchBackend):
    def __init__(self, *, page_count=3, bad_page=None, bad_next=None):
        super().__init__()
        self.page_count = page_count
        self.bad_page = bad_page
        self.bad_next = bad_next
        self.on_page = None

    def research_get(self, params, context):
        self.calls.append(("research.get", params))
        page = params.get("page", 0)
        if self.on_page:
            self.on_page(page)
        if page == self.bad_page:
            raise ValueError("fixture page unavailable")
        snapshot = _snapshot()
        snapshot.update(
            page=page,
            next_page=page + 1 if page + 1 < self.page_count else None,
            window_limit=100,
            subscriptions=[{"id": f"sub-{page}", "label": "前沿访谈" * 1_000}],
            evidence=[{"id": f"evidence-{page}"}],
            reports=[{"id": f"report-{page}"}],
            runs=[{"id": "run-0"}] if page == 0 else [],
        )
        snapshot["runtime"]["last_run"] = {
            "status": "success",
            "sources": [{"name": f"source-{page}", "status": "success"}],
        }
        if self.bad_next is not None:
            snapshot["next_page"] = self.bad_next
        return snapshot


def _paged_client(backend):
    application = GuardianApplication("r" * 43, backend=backend)
    name = f"alpha-guard.research-pages.{uuid.uuid4().hex[:12]}"
    server = QtGuardianServer(name, application.handle_json)
    server.start()
    return server, LocalSocketGuardianClient(name, "r" * 43)


def test_async_research_pages_merge_once_without_partial_updates_or_scans(qt_app):
    backend = PagedResearchBackend()
    server, client = _paged_client(backend)
    snapshots, errors, busy, observed_pages = [], [], [], []
    client.research_ready.connect(snapshots.append)
    client.request_failed.connect(lambda *args: errors.append(args))
    client.busy_changed.connect(busy.append)

    def observe(page):
        observed_pages.append((page, len(snapshots)))
        # A poll or navigation while loading must not start another page chain.
        client.refresh_research()

    backend.on_page = observe
    try:
        client.refresh_research()
        assert _wait(qt_app, lambda: bool(snapshots) or bool(errors))
        assert errors == []
        assert len(snapshots) == 1
        assert observed_pages == [(0, 0), (1, 0), (2, 0)]
        assert busy[-1] is False and False not in busy[:-1]
        result = snapshots[0]
        for key, prefix in (("subscriptions", "sub"), ("evidence", "evidence"), ("reports", "report")):
            assert [item["id"] for item in result[key]] == [f"{prefix}-{page}" for page in range(3)]
        assert result["runs"] == [{"id": "run-0"}]
        assert [item["name"] for item in result["runtime"]["last_run"]["sources"]] == [
            "source-0", "source-1", "source-2"
        ]
        assert result["runtime"]["state"] == "STOPPED"
        assert result["next_page"] is None
        assert backend.calls == [
            ("research.get", {}), ("research.get", {"page": 1}), ("research.get", {"page": 2})
        ]
    finally:
        client.close()
        server.stop()
        qt_app.processEvents()


def test_async_research_page_failure_keeps_existing_page_and_can_retry(qt_app):
    backend = PagedResearchBackend(bad_page=1)
    server, client = _paged_client(backend)
    snapshots, errors = [], []
    page = ResearchPage(client)
    previous = _snapshot()
    client.research_ready.emit(previous)
    client.research_ready.connect(snapshots.append)
    client.request_failed.connect(lambda *args: errors.append(args))
    try:
        client.refresh_research()
        assert _wait(qt_app, lambda: bool(errors))
        assert snapshots == []
        assert page._snapshot == previous
        assert [params for _method, params in backend.calls] == [{}, {"page": 1}]
        backend.bad_page = None
        client.refresh_research()
        assert _wait(qt_app, lambda: bool(snapshots))
        assert len(snapshots) == 1
        assert len(snapshots[0]["evidence"]) == 3
    finally:
        page.close()
        client.close()
        server.stop()
        qt_app.processEvents()


@pytest.mark.parametrize("bad_next", [0, -1, True, "1", 2])
def test_async_research_rejects_invalid_pagination_without_publishing(qt_app, bad_next):
    backend = PagedResearchBackend(bad_next=bad_next)
    server, client = _paged_client(backend)
    snapshots, errors = [], []
    client.research_ready.connect(snapshots.append)
    client.request_failed.connect(lambda *args: errors.append(args))
    try:
        client.refresh_research()
        assert _wait(qt_app, lambda: bool(errors))
        assert errors == [("research-get", "invalid_response")]
        assert snapshots == []
        assert backend.calls == [("research.get", {})]
    finally:
        client.close()
        server.stop()
        qt_app.processEvents()


def test_async_research_has_fixed_page_budget(qt_app, monkeypatch):
    backend = PagedResearchBackend(page_count=1000)
    server, client = _paged_client(backend)
    snapshots, errors = [], []
    client.research_ready.connect(snapshots.append)
    client.request_failed.connect(lambda *args: errors.append(args))
    assert client._MAX_RESEARCH_PAGES == 100
    monkeypatch.setattr(client, "_MAX_RESEARCH_PAGES", 3)
    try:
        client.refresh_research()
        assert _wait(qt_app, lambda: bool(errors))
        assert errors == [("research-get", "invalid_response")]
        assert snapshots == []
        assert [params for _method, params in backend.calls] == [{}, {"page": 1}, {"page": 2}]
    finally:
        client.close()
        server.stop()
        qt_app.processEvents()


def test_async_local_client_reads_commands_details_and_keeps_gui_responsive(qt_app):
    backend = ResearchBackend()
    application = GuardianApplication("r" * 43, backend=backend)
    name = f"alpha-guard.research.{uuid.uuid4().hex[:12]}"
    server = QtGuardianServer(name, application.handle_json)
    server.start()
    client = LocalSocketGuardianClient(name, "r" * 43)
    snapshots, details, actions, errors, ticks = [], [], [], [], []
    client.research_ready.connect(snapshots.append)
    client.research_detail_ready.connect(details.append)
    client.action_completed.connect(actions.append)
    client.request_failed.connect(lambda *args: errors.append(args))
    try:
        QTimer.singleShot(0, lambda: ticks.append(1))
        client.refresh_research()
        assert _wait(qt_app, lambda: bool(snapshots) or bool(errors))
        assert errors == [] and ticks == [1]
        assert backend.calls == [("research.get", {})]
        client.research_command("stop")
        assert _wait(qt_app, lambda: bool(actions) or bool(errors))
        assert actions[0].action == "research-stop"
        client.research_detail("evidence", "one")
        assert _wait(qt_app, lambda: bool(details) or bool(errors))
        assert details == [
            {
                "kind": "evidence",
                "id": "one",
                "item": {"id": "one", "title": "local evidence"},
            }
        ]
        assert errors == []
        assert ("research.command", {"command": "stop"}) in backend.calls
        client.research_detail("evidence", "one", offset=40_000, segment_offset=120)
        assert _wait(qt_app, lambda: len(details) == 2 or bool(errors))
        assert (
            "research.detail",
            {"kind": "evidence", "id": "one", "offset": 40_000, "segment_offset": 120},
        ) in backend.calls
        assert details[-1]["offset"] == 40_000
        assert details[-1]["segment_offset"] == 120
    finally:
        client.close()
        server.stop()
        qt_app.processEvents()


def test_managed_client_forwards_research_without_starting_collection(qt_app):
    transport = FakeResearchClient()
    client = ManagedGuardianClient(transport)
    received = []
    client.research_ready.connect(received.append)
    client.refresh_research()
    transport.research_ready.emit(_snapshot())
    client.research_command("stop")
    client.research_detail("evidence", "one")
    assert transport.reads == 1
    assert transport.commands == [("stop", {})]
    assert transport.details == [("evidence", "one")]
    client.research_detail("evidence", "one", offset=40_000, segment_offset=100)
    assert transport.detail_offsets[-1] == (40_000, 100)
    assert len(received) == 1
    client.close()


def test_fixture_explicitly_simulates_and_never_invents_research(qt_app):
    client = FixtureGuardianClient()
    snapshots, actions = [], []
    client.research_ready.connect(snapshots.append)
    client.action_completed.connect(actions.append)
    client.research_command("start")
    assert _wait(qt_app, lambda: bool(actions) and bool(snapshots))
    assert "演示" in actions[0].message
    assert snapshots[0]["runtime"]["state"] == "STOPPED"
    assert snapshots[0]["reports"] == []


def test_login_control_requires_enabled_dedicated_browser(qt_app):
    client, page = _loaded_page(qt_app)
    assert not page.open_login_button.isEnabled()
    page.open_login_button.click()
    assert client.commands == []
    client.research_ready.emit(_snapshot(capabilities={'browser_enabled': True, 'browser_mode': 'existing'}))
    assert not page.open_login_button.isEnabled()
    assert '现有浏览器模式' in page.login_hint.text()
    client.research_ready.emit(_snapshot(capabilities={'browser_enabled': True, 'browser_mode': 'dedicated'}))
    assert page.open_login_button.isEnabled()
    assert '不会启动扫描' in page.login_hint.text()
    page.close()


def test_login_control_defaults_to_selected_platform_and_preserves_stopped_state(qt_app):
    client, page = _loaded_page(qt_app)
    client.research_ready.emit(_snapshot(capabilities={'browser_enabled': True, 'browser_mode': 'dedicated'}))
    page.platform_input.setCurrentIndex(page.platform_input.findData('douyin'))
    assert page.login_platform_input.currentData() == 'douyin'
    page.open_login_button.click()
    assert client.commands == [('open_login', {'platform': 'douyin'})]
    assert not page.open_login_button.isEnabled()
    assert page._runtime_state == 'STOPPED' and not page._timer.isActive()
    client.action_completed.emit(ActionReceipt('research-open_login', True, 'accepted', 'login-1'))
    assert '尚未验证登录成功' in page.message_label.text()
    assert page.open_login_button.isEnabled()
    assert page._runtime_state == 'STOPPED' and not page._timer.isActive()
    page.close()


def test_selecting_existing_subscription_sets_login_platform_without_altering_draft(qt_app):
    client, page = _loaded_page(qt_app)
    page.subscription_url.setText('尚未保存的主页')
    client.research_ready.emit(_snapshot(capabilities={'browser_enabled': True, 'browser_mode': 'dedicated'},
        subscriptions=[{'id': 'creator', 'platform': 'bilibili', 'label': '作者',
                        'url': 'https://space.bilibili.com/123', 'enabled': True}]))
    page.subscription_table.selectRow(0)
    assert page.login_platform_input.currentData() == 'bilibili'
    assert page.subscription_url.text() == '尚未保存的主页'
    page.open_login_button.click()
    assert client.commands == [('open_login', {'platform': 'bilibili'})]
    client.action_completed.emit(ActionReceipt('research-open_login', False, '未找到 Chrome，请安装后重试。', 'login-2'))
    assert page.message_label.text() == '未找到 Chrome，请安装后重试。'
    assert page._runtime_state == 'STOPPED'
    page.close()


def _profile_row():
    return {'id': 'a' * 32, 'name': '中转模型', 'protocol': 'openai_compatible',
            'base_url': 'https://relay.example.com/v1', 'model': 'gpt-test',
            'timeout_seconds': 30, 'allow_private_network': False, 'enabled': True,
            'key_configured': True, 'key_updated_at': '2026-10-02T00:00:00+00:00',
            'updated_at': '2026-10-02T00:00:00+00:00'}


def test_models_tab_lists_profiles_and_saves_without_exposing_keys(qt_app):
    client, page = _loaded_page(qt_app)
    client.research_ready.emit(_snapshot(
        model_profiles=[_profile_row()],
        capabilities={'browser_enabled': False, 'model_provider': 'profile:中转模型',
                      'model_configured': True, 'model_profile_active': _profile_row()}))
    assert page.model_table.rowCount() == 1
    assert '中转模型' in page.model_table.item(0, 0).text()
    assert '当前激活' in page.model_table.item(0, 5).text()
    page.model_table.selectRow(0)
    assert page.model_name.text() == '中转模型'
    assert page.model_key.text() == ''  # a saved key is never echoed back
    page.model_key.setText('sk-new-abcdef123456')
    page.model_save_button.click()
    command, fields = client.commands[-1]
    assert command == 'model_save'
    assert fields['api_key'] == 'sk-new-abcdef123456'
    assert fields['profile']['id'] == 'a' * 32
    client.action_completed.emit(ActionReceipt('research-model_save', True, 'Guardian 已接受请求', 'm0'))
    page.model_activate_button.click()
    assert client.commands[-1] == ('model_activate', {'id': 'a' * 32})
    client.action_completed.emit(ActionReceipt('research-model_activate', True, 'Guardian 已接受请求', 'm2'))
    page.model_deactivate_button.click()
    assert client.commands[-1] == ('model_activate', {'id': ''})
    client.action_completed.emit(ActionReceipt('research-model_save', True, 'Guardian 已接受请求', 'm1'))
    assert '钥匙串' in page.message_label.text()
    page.close()


def test_digest_tab_populates_saves_settings_and_runs_once(qt_app):
    client, page = _loaded_page(qt_app)
    digest = {
        'settings': {'enabled': True, 'time_local': '09:30',
                     'topics': [{'label': '半导体', 'keywords': ['芯片', '光刻']}],
                     'ai_summary': False, 'notify': False, 'max_items': 12},
        'plan': {'enabled': True, 'next_run_at': '2026-10-03T01:30:00+00:00'},
        'runtime': {'state': 'RUNNING', 'next_run_at': '2026-10-03T01:30:00+00:00'},
        'latest': {'id': 'd1', 'created_at': '2026-10-02T01:30:00+00:00', 'status': 'completed',
                   'detail': '', 'trigger': 'schedule', 'counts': {'matched': 2, 'evidence_window': 5},
                   'sections': [{'key': 'topic:半导体', 'kind': 'topic', 'label': '半导体',
                                 'match': '芯片', 'entries': [
                                     {'evidence_id': 'e1', 'title': '芯片涨价', 'score': 3,
                                      'source_url': 'https://e.example.com/1', 'platform': 'news',
                                      'published_at': None, 'matched': ['topic:半导体']}]}],
                   'coverage_gaps': [], 'ai_summary': {'enabled': False, 'status': 'disabled'},
                   'delivery': {'enabled': False, 'status': 'skipped'}},
        'history': [{'id': 'd0', 'created_at': '2026-10-01T01:30:00+00:00', 'status': 'completed',
                     'detail': '', 'trigger': 'schedule', 'counts': {}, 'coverage_gaps': []}],
    }
    client.research_ready.emit(_snapshot(digest=digest))
    assert page.digest_enabled.isChecked() and page.digest_time.time().toString("HH:mm") == '09:30'
    assert page.digest_topics.toPlainText().startswith('半导体: 芯片')
    assert page.digest_history.rowCount() == 1
    assert '芯片涨价' in page.digest_detail.toPlainText()
    page.digest_topics.setPlainText('创新药: 临床, FDA')
    page.digest_ai.setChecked(True)
    page._save_digest_settings()
    command, fields = client.commands[-1]
    assert command == 'digest_save'
    assert fields['settings']['topics'] == [{'label': '创新药', 'keywords': ['临床', 'FDA']}]
    assert fields['settings']['ai_summary'] is True
    assert fields['settings']['notify_channels'] == ['telegram', 'whatsapp']
    assert not page.digest_run_button.isEnabled()
    page.digest_run_button.click()
    assert client.commands[-1][0] == 'digest_save'
    client.action_completed.emit(ActionReceipt('research-digest_save', True, 'Guardian 已接受请求', 'd1'))
    assert not page._digest_dirty
    page.digest_run_button.click()
    assert client.commands[-1][0] == 'digest_once'
    page.digest_history.selectRow(0)
    assert client.details[-1] == ('digest', 'd0')
    page.set_detail({'kind': 'digest', 'id': 'd0', 'item': digest['history'][0]})
    page.close()


def test_digest_channels_preserve_unsaved_choice_and_require_save_before_run(qt_app):
    client, page = _loaded_page(qt_app)
    saved = {'settings': {'notify': True, 'notify_channels': ['telegram']}}
    client.research_ready.emit(_snapshot(digest=saved))
    assert page.digest_telegram.isChecked() and not page.digest_whatsapp.isChecked()
    page.digest_telegram.setChecked(False)
    page.digest_whatsapp.setChecked(True)
    client.research_ready.emit(_snapshot(digest=saved))
    assert not page.digest_telegram.isChecked() and page.digest_whatsapp.isChecked()
    before = list(client.commands)
    page._run_digest_once()
    assert client.commands == before
    page._save_digest_settings()
    assert client.commands[-1][1]['settings']['notify_channels'] == ['whatsapp']
    assert not page.digest_telegram.isEnabled()
    client.action_completed.emit(ActionReceipt('research-digest_save', True, 'saved', 'channels'))
    assert page.digest_whatsapp.isEnabled()
    page.digest_whatsapp.setChecked(False)
    before = list(client.commands)
    page._save_digest_settings()
    assert client.commands == before
    assert '至少选择一个' in page.message_label.text()
    page.close()


def test_digest_delivery_shows_acceptance_and_template_limits(qt_app):
    client, page = _loaded_page(qt_app)
    record = {'status': 'completed', 'delivery': {'enabled': True, 'status': 'partial', 'channels': [
        {'channel': 'telegram', 'content': 'summary_text', 'status': 'accepted'},
        {'channel': 'whatsapp', 'content': 'static_template', 'status': 'not_configured'},
    ]}}
    page.digest_detail.setHtml(page._digest_html(record))
    text = page.digest_detail.toPlainText()
    assert '服务已接受，接收待确认' in text
    assert 'WhatsApp' in text and '不含摘要正文' in text and '未配置' in text
    assert '部分通道未接受' in text
    page.close()


def _digest_record(identity, title='保存的原文'):
    return {'id': identity, 'created_at': '2026-10-03T01:30:00+00:00',
            'status': 'completed', 'trigger': 'manual', 'counts': {},
            'sections': [{'key': 'US:AAPL', 'label': 'Apple', 'entries': [
                {'evidence_id': f'evidence-{identity}', 'title': title,
                 'source_url': 'https://example.org/article'}]}]}


def test_digest_history_pagination_preserves_selected_record_across_refresh(qt_app):
    client, page = _loaded_page(qt_app)
    first = _digest_record('first')
    client.research_ready.emit(_snapshot(digest={'latest': first, 'history': []}))
    assert ('digest_history', 'history') in client.details
    assert client.detail_offsets[client.details.index(('digest_history', 'history'))] == (0, 0)
    records = [_digest_record(f'history-{index}') for index in range(20)]
    client.research_detail_ready.emit({'kind': 'digest_history', 'id': 'history', 'item': {
        'offset': 0, 'next_offset': 20, 'items': records}})
    assert page.digest_history.rowCount() == 20
    page.digest_history.selectRow(5)
    selected = records[5]
    client.research_detail_ready.emit({'kind': 'digest', 'id': selected['id'], 'item': selected})
    shown = page.digest_detail.toPlainText()
    client.research_ready.emit(_snapshot(digest={'latest': _digest_record('newest', '另一份摘要'), 'history': []}))
    assert page._selected['digest'] == selected['id']
    assert page.digest_history.currentRow() == 5
    assert page.digest_detail.toPlainText() == shown
    page.digest_history_next.click()
    assert client.detail_offsets[-1] == (20, 0)
    client.research_detail_ready.emit({'kind': 'digest_history', 'id': 'history', 'item': {
        'offset': 0, 'next_offset': 20, 'items': records}})  # late page
    assert page._digest_history_requested == 20
    client.research_detail_ready.emit({'kind': 'digest_history', 'id': 'history', 'item': {
        'offset': 20, 'next_offset': None, 'items': [_digest_record('oldest')]}})
    assert page.digest_history.rowCount() == 1
    assert page.digest_history_previous.isEnabled() and not page.digest_history_next.isEnabled()
    assert page.digest_detail.toPlainText() == shown
    page.digest_history_previous.click()
    assert client.detail_offsets[-1] == (0, 0)
    page.close()


def test_digest_links_open_local_evidence_outside_recent_list_and_original_url(qt_app, monkeypatch):
    client, page = _loaded_page(qt_app)
    opened = []
    monkeypatch.setattr('desktop.ui.research_page.QDesktopServices.openUrl', lambda url: opened.append(url.toString()))
    record = _digest_record('old')
    page._selected['digest'] = record['id']
    page.set_detail({'kind': 'digest', 'id': record['id'], 'item': record})
    assert '查看本地证据' in page.digest_detail.toPlainText()
    page.digest_detail.anchorClicked.emit(QUrl('alpha-guard-evidence:evidence-old'))
    assert client.details[-1] == ('evidence', 'evidence-old')
    assert page.tabs.currentIndex() == 1 and opened == []
    client.research_detail_ready.emit({'kind': 'evidence', 'id': 'evidence-old', 'item': {
        'id': 'evidence-old', 'content_text': '原始材料仍可读', 'title': '旧材料', 'segments': []}})
    assert '原始材料仍可读' in page.evidence_detail.toPlainText()
    before = list(client.details)
    page.digest_detail.anchorClicked.emit(QUrl('alpha-guard-evidence:unrelated'))
    assert client.details == before
    page.digest_detail.anchorClicked.emit(QUrl('https://example.org/article'))
    assert opened == ['https://example.org/article']
    page.close()


def test_digest_history_read_failure_keeps_visible_page_and_can_retry(qt_app):
    client, page = _loaded_page(qt_app)
    page._request_digest_history(0)
    client.research_detail_ready.emit({'kind': 'digest_history', 'id': 'history', 'item': {
        'offset': 0, 'next_offset': 20, 'items': [_digest_record('saved')]}})
    page.digest_history_next.click()
    client.request_failed.emit('research-detail', 'request_timeout')
    assert page.digest_history.rowCount() == 1
    assert '保留上次结果' in page.digest_history_label.text()
    assert page.digest_history_next.isEnabled()
    page.digest_history_next.click()
    assert client.detail_offsets[-1] == (20, 0)
    page.close()


def test_digest_detail_pager_ignores_late_page_and_keeps_ai_citation_links(qt_app):
    client, page = _loaded_page(qt_app)
    record = _digest_record('paged')
    record.update(digest_offset=0, digest_next_offset=20, digest_total=25)
    record['ai_summary'] = {'enabled': True, 'status': 'generated',
        'sections': {'US:AAPL': '完整标题'},
        'citations': {'US:AAPL': [{'evidence_id': 'original-ai-evidence', 'quote': '完整标题'}]}}
    page._request_digest_detail('paged', 0)
    client.research_detail_ready.emit({'kind': 'digest', 'id': 'paged', 'item': record})
    assert page.digest_detail_next.isEnabled()
    assert '核对本地原文' in page.digest_detail.toPlainText()
    page.digest_detail_next.click()
    assert client.detail_offsets[-1] == (20, 0)
    client.research_detail_ready.emit({'kind': 'digest', 'id': 'paged', 'item': record})
    assert page._digest_detail_requested == ('paged', 20)
    second = {**record, 'digest_offset': 20, 'digest_next_offset': None}
    client.research_detail_ready.emit({'kind': 'digest', 'id': 'paged', 'item': second})
    assert page.digest_detail_previous.isEnabled() and not page.digest_detail_next.isEnabled()
    client.research_detail_ready.emit({'kind': 'digest', 'id': 'paged', 'item': record})
    assert page._digest_display['digest_offset'] == 20  # late first-page reply after completion
    page.digest_detail.anchorClicked.emit(QUrl('alpha-guard-evidence:original-ai-evidence'))
    assert client.details[-1] == ('evidence', 'original-ai-evidence')
    page.close()


def test_model_current_label_tracks_api_and_default_switch(qt_app):
    page = ResearchPage(GuardianClient())
    page._snapshot = {"capabilities": {"model_provider": "codex_cli"}, "model_profiles": []}
    page._populate_models()
    assert "本机 Codex" in page.model_current.text()
    page._snapshot["capabilities"] = {"model_provider": "profile:ZCode", "model_profile_active": {"name": "ZCode", "model": "configured-model"}}
    page._populate_models()
    assert "ZCode" in page.model_current.text() and "本机 Codex" not in page.model_current.text()
    page._snapshot["capabilities"] = {"model_provider": "codex_cli"}
    page._populate_models()
    assert "本机 Codex" in page.model_current.text()
    page.close()
