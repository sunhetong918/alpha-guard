"""Exercise digest delivery through real channel dispatch with offline transports."""
from __future__ import annotations

import threading

from config import Settings
from notifier.whatsapp import WhatsAppDeliveryResult
from research.digest import _deliver, _delivery_text, generate_digest
from research.models import DigestSettings
from research.store import ResearchStore
from research.service import ResearchService
from state import StateStore

import pytest


def environment(*, telegram=True, whatsapp=False):
    return Settings(
        _env_file=None, notifications_enabled=telegram,
        telegram_bot_token="telegram-test-secret" if telegram else None,
        telegram_chat_id="test-chat" if telegram else None,
        whatsapp_enabled=whatsapp,
        whatsapp_access_token="whatsapp-test-secret" if whatsapp else None,
        whatsapp_phone_number_id="123456789" if whatsapp else None,
        whatsapp_default_to="8613800000000" if whatsapp else None,
        whatsapp_signal_template_name="test_signal",
        whatsapp_incident_template_name="test_incident",
        whatsapp_trust_template_name="test_trust",
    )


def record():
    return {"id": "digest-dispatch-test", "sections": [{"key": "US:AAPL", "label": "苹果",
            "entries": [{"title": "测试材料已保存，等待人工核验"}]}], "coverage_gaps": []}


def test_telegram_summary_reaches_plain_message_dispatch_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: environment())
    texts = []
    async def send(text, *, settings):
        texts.append(text)
    monkeypatch.setattr("notifier.mobile.send_message", send)
    monkeypatch.setattr("notifier.mobile.send_news_alert", lambda *a, **k: pytest.fail("digest is not a news object"))
    database = tmp_path / "state.db"
    first = _deliver(database, record(), date_label="2026-10-15", notify_channels=("telegram",))
    assert texts == [_delivery_text(record(), date_label="2026-10-15")]
    assert first["status"] == "accepted" and first["recipient_confirmed"] is False
    assert first["channels"][0] == {"channel": "telegram", "content": "summary_text",
        "status": "accepted", "attempted": True, "accepted": True, "error_code": None,
        "recipient_confirmed": False}
    second = _deliver(database, record(), date_label="2026-10-15", notify_channels=("telegram",))
    assert len(texts) == 1 and second["status"] == "already_accepted"
    assert second["channels"][0]["status"] == "already_accepted"
    with StateStore(database) as store:
        assert len(store.outbound_deliveries(business_key="digest:digest-dispatch-test")) == 1


def test_selected_whatsapp_uses_static_trust_template_and_never_summary_text(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: environment(telegram=True, whatsapp=True))
    monkeypatch.setattr("notifier.mobile.send_message", lambda *a, **k: pytest.fail("unselected Telegram"))
    templates = []
    def send_template(self, *, template_name):
        templates.append(template_name)
        return WhatsAppDeliveryResult(True, "accepted")
    monkeypatch.setattr("notifier.mobile.WhatsAppNotifier.send_template", send_template)
    result = _deliver(tmp_path / "state.db", record(), date_label="2026-10-15", notify_channels=("whatsapp",))
    assert templates == ["test_trust"]
    assert result["status"] == "accepted" and result["detail"] == "whatsapp_static_template_only"
    assert len(result["channels"]) == 1
    assert result["channels"][0]["content"] == "static_template"
    assert result["channels"][0]["recipient_confirmed"] is False


def test_selected_missing_channel_cannot_be_hidden_by_other_success(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: environment())
    async def send(*args, **kwargs):
        pass
    monkeypatch.setattr("notifier.mobile.send_message", send)
    result = _deliver(tmp_path / "state.db", record(), date_label="2026-10-15")
    assert result["status"] == "partial"
    assert [(c["channel"], c["status"]) for c in result["channels"]] == [
        ("telegram", "accepted"), ("whatsapp", "not_configured")]
    assert result["channels"][1]["attempted"] is False


def test_all_selected_missing_channels_do_not_attempt_network(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: environment(telegram=False))
    monkeypatch.setattr("notifier.mobile.send_message", lambda *a, **k: pytest.fail("missing Telegram"))
    result = _deliver(tmp_path / "state.db", record(), date_label="2026-10-15", notify_channels=("telegram",))
    assert result["status"] == "not_configured"
    assert result["channels"][0]["status"] == "not_configured"
    assert result["channels"][0]["attempted"] is False


def test_retry_preserves_accepted_channel_and_retries_failed_only(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: environment(whatsapp=True))
    calls = {"telegram": 0, "whatsapp": 0}
    async def send(*args, **kwargs):
        calls["telegram"] += 1
    def send_template(self, *, template_name):
        calls["whatsapp"] += 1
        return WhatsAppDeliveryResult(calls["whatsapp"] > 1,
                                      "accepted" if calls["whatsapp"] > 1 else "timeout")
    monkeypatch.setattr("notifier.mobile.send_message", send)
    monkeypatch.setattr("notifier.mobile.WhatsAppNotifier.send_template", send_template)
    database = tmp_path / "state.db"
    assert _deliver(database, record(), date_label="2026-10-15")["status"] == "partial"
    second = _deliver(database, record(), date_label="2026-10-15")
    assert calls == {"telegram": 1, "whatsapp": 2}
    assert second["status"] == "accepted"
    assert [c["status"] for c in second["channels"]] == ["already_accepted", "accepted"]


def test_real_dispatch_result_is_persisted_and_reopened(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    monkeypatch.setattr("config.get_settings", lambda: environment())
    async def send(*args, **kwargs):
        with ResearchStore(database) as store:
            assert store.list_digests(limit=1)[0]["delivery"]["status"] == "pending"
    monkeypatch.setattr("notifier.mobile.send_message", send)
    result = generate_digest(database, settings_override=DigestSettings(notify=True, notify_channels=("telegram",)),
                             collector=lambda *_: ([], []))
    assert result["delivery"]["status"] == "accepted"
    with ResearchStore(database) as store:
        assert store.get_digest(result["id"])["delivery"] == result["delivery"]


def test_notify_channel_contract_defaults_and_validation():
    assert DigestSettings().notify_channels == ("telegram", "whatsapp")
    assert DigestSettings().notify is False
    assert DigestSettings(notify_channels=["telegram"]).notify_channels == ("telegram",)
    assert DigestSettings(notify=False, notify_channels=[]).notify_channels == ()
    for values in ({"notify": True, "notify_channels": []},
                   {"notify_channels": ["telegram", "telegram"]},
                   {"notify_channels": ["email"]}):
        with pytest.raises(ValueError):
            DigestSettings.model_validate(values)


def test_cancellation_after_telegram_acceptance_prevents_whatsapp_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: environment(whatsapp=True))
    stop = threading.Event()
    async def send(*args, **kwargs):
        stop.set()
    monkeypatch.setattr("notifier.mobile.send_message", send)
    monkeypatch.setattr("notifier.mobile.WhatsAppNotifier.send_template",
                        lambda *a, **k: pytest.fail("cancelled WhatsApp must not send"))
    result = _deliver(tmp_path / "state.db", record(), date_label="2026-10-15", cancelled=stop.is_set)
    assert result["status"] == "cancelled"
    assert [c["status"] for c in result["channels"]] == ["accepted", "cancelled"]
    assert result["channels"][1]["attempted"] is False


def test_channel_selection_persists_through_service_and_validates_before_save(tmp_path):
    database = tmp_path / "state.db"
    service = ResearchService(database)
    selected = service.save_digest_settings({"notify": True, "notify_channels": ["telegram"]})
    assert selected["notify_channels"] == ["telegram"]
    with pytest.raises(ValueError):
        service.save_digest_settings({"notify": True, "notify_channels": []})
    with ResearchStore(database) as store:
        assert store.load_digest_settings()["notify_channels"] == ["telegram"]
    service.close()


def test_saved_digest_records_frozen_selected_channels(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: environment())
    async def send(*args, **kwargs):
        pass
    monkeypatch.setattr("notifier.mobile.send_message", send)
    database = tmp_path / "state.db"
    result = generate_digest(database, settings_override=DigestSettings(notify=True, notify_channels=("telegram",)),
                             collector=lambda *_: ([], []))
    assert result["settings_snapshot"]["notify_channels"] == ["telegram"]
    assert [c["channel"] for c in result["delivery"]["channels"]] == ["telegram"]
