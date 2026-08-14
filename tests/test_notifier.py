from __future__ import annotations

import asyncio

import pytest

from config import Settings
from notifier.telegram_bot import (
    NotificationConfigurationError,
    render_incident_alert,
    render_news_alert,
    render_plain_message,
    render_signal_alert,
    send_signal,
    send_incident,
)


def _settings(**values):
    return Settings(_env_file=None, **values)


def test_signal_renderer_escapes_every_dynamic_field_and_shows_evidence():
    message = render_signal_alert(
        {
            "ticker": "<AAPL>",
            "name": '<script>alert("x")</script>',
            "market": "US<&>",
            "action": "BUY",
            "reasons": ["reason <b>not markup</b>"],
            "evidence": [
                {
                    "rule_id": "rule<&>",
                    "actual": "101<&>",
                    "operator": ">=",
                    "threshold": "100<&>",
                    "source": "provider<&>",
                    "as_of": "2026-08-10T12:00:00+00:00<&>",
                    "currency": "USD<&>",
                    "quality_issues": ["delayed<&>"],
                }
            ],
        }
    )

    assert "人工核验提醒" in message
    assert "未执行任何交易" in message
    assert "实际值 / 阈值" in message
    assert "来源" in message
    assert "数据时间" in message
    assert "币种" in message
    assert "质量证据" in message
    assert "&lt;script&gt;" in message
    assert "<script>" not in message
    assert "reason &lt;b&gt;not markup&lt;/b&gt;" in message
    assert "买入信号" not in message
    assert "手动操作" not in message


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///tmp/secret",
        "//example.test/no-scheme",
    ],
)
def test_news_renderer_rejects_non_http_links(url):
    message = render_news_alert({"title": "Story", "url": url})

    assert "href=" not in message
    assert "未提供有效的 HTTP(S) 链接" in message


def test_news_renderer_allows_and_escapes_https_link():
    message = render_news_alert(
        {
            "title": "Story <unsafe>",
            "url": "https://example.test/path?a=1&b=2",
            "source": "Wire & Co",
        }
    )

    assert 'href="https://example.test/path?a=1&amp;b=2"' in message
    assert "Story &lt;unsafe&gt;" in message
    assert "Wire &amp; Co" in message


def test_renderers_bound_oversized_messages():
    huge = "<&>" * 20_000

    signal_message = render_signal_alert(
        {
            "ticker": huge,
            "name": huge,
            "reasons": [huge] * 100,
            "evidence": [
                {
                    "actual": huge,
                    "threshold": huge,
                    "source": huge,
                    "as_of": huge,
                    "currency": huge,
                    "quality_issues": [huge] * 100,
                }
            ]
            * 100,
        }
    )
    news_message = render_news_alert(
        {
            "title": huge,
            "ai_analysis": huge,
            "related_tickers": [huge] * 100,
            "related_topics": [huge] * 100,
            "quality_issues": [huge] * 100,
            "url": "https://example.test/" + "a" * 5_000,
        }
    )

    assert len(signal_message) <= 4_096
    assert len(news_message) <= 4_096
    assert "未执行任何交易" in signal_message
    assert "未执行任何交易" in news_message


def test_plain_message_is_escaped_and_bounded():
    message = render_plain_message("<b>not trusted</b>" + "x" * 10_000)

    assert message.startswith("&lt;b&gt;not trusted&lt;/b&gt;")
    assert len(message) <= 4_096


def test_incident_renderer_is_bounded_escaped_and_operationally_explicit():
    secretish = "<provider&failure>"
    message = render_incident_alert(
        {
            "state": "BLIND",
            "scope": "market:<US>",
            "affected_tickers": ["AAPL<&>"] * 100,
            "incident_started_at": "2026-08-10T13:40:00Z<&>",
            "last_success_at": "2026-08-07T13:25:00Z",
            "blind_started_at": "2026-08-10T13:40:00Z",
            "reason_codes": [secretish] * 100,
            "recommended_action": "check <network>" * 1_000,
        }
    )

    assert len(message) <= 4_096
    assert "保护状态" in message
    assert "BLIND" in message
    assert "未执行任何交易" in message
    assert "&lt;provider&amp;failure&gt;" in message
    assert secretish not in message


def test_incident_delivery_uses_same_double_opt_in_boundary():
    constructed = False

    def bot_factory(**_kwargs):
        nonlocal constructed
        constructed = True

    with pytest.raises(NotificationConfigurationError):
        asyncio.run(
            send_incident(
                {"state": "BLIND"},
                settings=_settings(notifications_enabled=False),
                bot_factory=bot_factory,
            )
        )
    assert constructed is False


def test_delivery_refuses_safe_default_before_constructing_bot():
    constructed = False

    def bot_factory(**kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("bot must not be constructed")

    with pytest.raises(NotificationConfigurationError, match="NOTIFICATIONS_ENABLED"):
        asyncio.run(
            send_signal(
                {"ticker": "AAPL"},
                settings=_settings(notifications_enabled=False),
                bot_factory=bot_factory,
            )
        )

    assert constructed is False


def test_delivery_reports_each_missing_required_setting():
    with pytest.raises(NotificationConfigurationError) as exc_info:
        asyncio.run(
            send_signal(
                {"ticker": "AAPL"},
                settings=_settings(notifications_enabled=True),
                bot_factory=lambda **kwargs: None,
            )
        )

    message = str(exc_info.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "TELEGRAM_CHAT_ID" in message


class _FakeBot:
    def __init__(self, *, token):
        self.token = token
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)


def test_delivery_uses_rendered_html_with_injected_settings():
    fake_bot = _FakeBot(token="unused")

    def factory(*, token):
        fake_bot.token = token
        return fake_bot

    asyncio.run(
        send_signal(
            {"ticker": "<AAPL>", "name": "Unsafe <name>"},
            settings=_settings(
                notifications_enabled=True,
                telegram_bot_token="bot-secret",
                telegram_chat_id="chat-123",
            ),
            bot_factory=factory,
        )
    )

    assert fake_bot.token == "bot-secret"
    assert len(fake_bot.calls) == 1
    call = fake_bot.calls[0]
    assert call["chat_id"] == "chat-123"
    assert call["parse_mode"] == "HTML"
    assert "Unsafe &lt;name&gt;" in call["text"]
    assert "未执行任何交易" in call["text"]
