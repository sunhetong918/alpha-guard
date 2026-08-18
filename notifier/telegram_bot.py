"""Safe Telegram rendering and delivery for human-review reminders."""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from config import Settings, get_settings

TELEGRAM_MESSAGE_LIMIT = 4_096
BotFactory = Callable[..., Any]


class NotificationConfigurationError(RuntimeError):
    """Raised before network access when real notifications are not configured."""


def render_signal_alert(signal: Mapping[str, Any]) -> str:
    """Purely render one rule-evidence package as bounded Telegram HTML."""

    ticker = _escaped(signal.get("ticker", "未提供"), 96)
    name = _escaped(signal.get("name", "未命名标的"), 180)
    market = _escaped(signal.get("market", "未提供"), 48)
    review_kind = _review_kind(signal.get("action", signal.get("status", "")))
    rules_version = _escaped(
        signal.get("instrument_rules_version", signal.get("rules_version", "未提供")),
        80,
    )

    lines = [
        "🔎 <b>人工核验提醒</b>",
        f"标的：<b>{name}</b>（{ticker}）",
        f"市场：{market}",
        f"复核类别：{review_kind}",
        f"规则版本：<code>{rules_version}</code>",
        "",
        "<b>规则证据</b>",
    ]

    evidence_items = _evidence_items(signal)
    for index, evidence in enumerate(evidence_items[:4], start=1):
        rule_id = _escaped(
            evidence.get("rule_id", evidence.get("id", f"evidence-{index}")), 100
        )
        actual = _escaped(
            evidence.get(
                "actual",
                evidence.get(
                    "actual_value", signal.get("actual", signal.get("price", "未提供"))
                ),
            ),
            120,
        )
        operator = _escaped(evidence.get("operator", ""), 24, empty="")
        threshold = _escaped(
            evidence.get(
                "threshold",
                evidence.get("value", signal.get("threshold", "未提供")),
            ),
            120,
        )
        unit = _escaped(evidence.get("unit", ""), 32, empty="")
        source = _escaped(evidence.get("source", signal.get("source", "未提供")), 180)
        as_of = _escaped(
            evidence.get(
                "as_of",
                signal.get("as_of", signal.get("retrieved_at", "未提供")),
            ),
            150,
        )
        currency = _escaped(
            evidence.get("currency", signal.get("currency", "未提供")), 32
        )
        quality = _render_quality(
            evidence.get(
                "quality_issues",
                evidence.get("quality", signal.get("quality_issues", [])),
            )
        )
        comparison = " ".join(
            part for part in (actual, operator, threshold, unit) if part
        )
        lines.extend(
            [
                f"{index}. 规则 ID：<code>{rule_id}</code>",
                f"   实际值 / 阈值：{comparison}",
                f"   来源：{source}",
                f"   数据时间：{as_of}",
                f"   币种：{currency}",
                f"   质量证据：{quality}",
            ]
        )

    reasons = _string_items(signal.get("reasons", []), max_items=5)
    if reasons:
        lines.extend(["", "<b>规则说明</b>"])
        lines.extend(f"• {_escaped(reason, 220)}" for reason in reasons)

    lines.extend(
        [
            "",
            "⚠️ 这是人工核验提醒；Alpha Guard <b>未执行任何交易</b>，也不会代替你作出投资决定。",
        ]
    )
    return _bounded_message("\n".join(lines), ticker=ticker, name=name)


def render_news_alert(article: Mapping[str, Any]) -> str:
    """Purely render one bounded and escaped news-review message."""

    title = _escaped(article.get("title", "无标题"), 520)
    source = _escaped(article.get("source", "未提供"), 180)
    as_of = _escaped(article.get("as_of", article.get("datetime", "未提供")), 150)
    currency = _escaped(article.get("currency", "不适用"), 32)
    tickers = _escaped(
        ", ".join(_string_items(article.get("related_tickers", []), max_items=20))
        or "未匹配",
        260,
    )
    topics = _escaped(
        ", ".join(_string_items(article.get("related_topics", []), max_items=20))
        or "未匹配",
        260,
    )

    score = article.get("ai_score")
    if isinstance(score, int) and not isinstance(score, bool) and 1 <= score <= 5:
        score_text = f"{'⭐' * score} {score}/5"
    else:
        score_text = "未评分（关键词匹配复核）"
    threshold = article.get("alert_threshold")
    threshold_text = (
        f"{threshold}/5"
        if isinstance(threshold, int)
        and not isinstance(threshold, bool)
        and 1 <= threshold <= 5
        else "未提供"
    )
    direction = article.get("affected_direction")
    direction_text = direction if direction in {"利好", "利空", "中性"} else "未知"
    analysis = _escaped(article.get("ai_analysis", "未提供"), 760)
    quality = _render_quality(article.get("quality_issues", []))

    link = _safe_http_url(article.get("url", ""))
    link_line = (
        f'原文：<a href="{html.escape(link, quote=True)}">打开 HTTP(S) 链接</a>'
        if link
        else "原文：未提供有效的 HTTP(S) 链接"
    )

    text = "\n".join(
        [
            "📰 <b>新闻人工核验提醒</b>",
            "",
            f"<b>{title}</b>",
            f"AI 实际评分 / 提醒阈值：{score_text} / {threshold_text}",
            f"AI 方向标注：{direction_text}",
            f"关联标的：{tickers}",
            f"关联主题：{topics}",
            f"摘要标注：{analysis}",
            "",
            f"来源：{source}",
            f"数据时间：{as_of}",
            f"币种：{currency}",
            f"质量证据：{quality}",
            link_line,
            "",
            "⚠️ 这是人工核验提醒；Alpha Guard <b>未执行任何交易</b>。新闻与 AI 标注可能不完整，不构成投资建议。",
        ]
    )
    return _bounded_message(text, ticker="news", name=title)


def render_plain_message(text: Any) -> str:
    """Escape arbitrary caller text before sending it in HTML mode."""

    rendered = _escaped(text, TELEGRAM_MESSAGE_LIMIT - 16)
    return rendered or "（空消息）"


def render_incident_alert(incident: Mapping[str, Any]) -> str:
    """Render one bounded operational state edge, separate from market signals."""

    state = str(incident.get("state") or "UNKNOWN").strip().upper()
    marker = {
        "HEALTHY": "🟢",
        "DEGRADED": "🟠",
        "BLIND": "🔴",
        "RECOVERING": "🔵",
        "RECOVERED": "🟢",
        "UNCONFIGURED": "⚪",
        "PAUSED": "⚪",
    }.get(state, "⚫")
    state_text = _escaped(state, 40)
    scope = _escaped(incident.get("scope", "global"), 100)
    started = _escaped(
        incident.get("incident_started_at", incident.get("state_since", "未提供")),
        150,
    )
    last_success = _escaped(incident.get("last_success_at", "未提供"), 150)
    blind_started = _escaped(incident.get("blind_started_at", "未发生"), 150)
    recovered = _escaped(incident.get("recovered_at", "尚未恢复"), 150)
    affected = _escaped(
        ", ".join(_string_items(incident.get("affected_tickers", []), max_items=20))
        or "未提供",
        420,
    )
    reasons = _render_quality(incident.get("reason_codes", []))
    action = _escaped(
        incident.get(
            "recommended_action",
            {
                "BLIND": "检查进程、网络、数据提供者与最近应跑窗口",
                "DEGRADED": "查看受影响标的与 capability，保留可用证据",
                "RECOVERING": "等待下一次独立 full-coverage scan 确认",
                "RECOVERED": "核验盲区与恢复时间，继续只读监控",
                "HEALTHY": "核验盲区与恢复时间，继续只读监控",
            }.get(state, "运行 alpha-guard status 查看本地证据"),
        ),
        520,
    )
    text = "\n".join(
        [
            f"{marker} <b>Alpha Guard 保护状态：{state_text}</b>",
            f"范围：{scope}",
            f"影响标的：{affected}",
            f"事故始发：{started}",
            f"最近可信成功：{last_success}",
            f"盲区起点：{blind_started}",
            f"恢复时间：{recovered}",
            f"原因代码：{reasons}",
            f"建议动作：{action}",
            "",
            "⚠️ 这是系统运行事件；Alpha Guard <b>未执行任何交易</b>。",
        ]
    )
    return _bounded_operational_message(text, state=state_text, scope=scope)


# Discoverable aliases for callers that use shorter renderer names.
render_signal = render_signal_alert
render_news = render_news_alert
format_signal_message = render_signal_alert
format_news_message = render_news_alert


async def send_message(
    text: Any,
    settings: Settings | None = None,
    *,
    bot_factory: BotFactory | None = None,
) -> None:
    """Escape and deliver an arbitrary informational message."""

    await _send_html(
        render_plain_message(text),
        settings=settings,
        bot_factory=bot_factory,
        disable_web_page_preview=True,
    )


async def send_signal(
    signal: Mapping[str, Any],
    settings: Settings | None = None,
    *,
    bot_factory: BotFactory | None = None,
) -> None:
    """Render and deliver a rule-review reminder; never executes a transaction."""

    await _send_html(
        render_signal_alert(signal),
        settings=settings,
        bot_factory=bot_factory,
        disable_web_page_preview=True,
    )


async def send_incident(
    incident: Mapping[str, Any],
    settings: Settings | None = None,
    *,
    bot_factory: BotFactory | None = None,
) -> None:
    """Deliver an operational edge; notification opt-in still applies."""

    await _send_html(
        render_incident_alert(incident),
        settings=settings,
        bot_factory=bot_factory,
        disable_web_page_preview=True,
    )


async def send_news_alert(
    article: Mapping[str, Any],
    settings: Settings | None = None,
    *,
    bot_factory: BotFactory | None = None,
) -> None:
    """Render and deliver a news-review reminder."""

    await _send_html(
        render_news_alert(article),
        settings=settings,
        bot_factory=bot_factory,
        disable_web_page_preview=True,
    )


def render_trade_alert(record: Mapping[str, Any]) -> str:
    """Render one guarded trading outcome (submission, skip or denial)."""

    intent = record.get("intent") if isinstance(record.get("intent"), Mapping) else {}
    outcome = record.get("outcome") if isinstance(record.get("outcome"), Mapping) else {}
    ticker = _escaped(intent.get("ticker", "?"), 32, empty="?")
    side = _escaped(intent.get("side", "?"), 8, empty="?")
    quantity = _escaped(intent.get("quantity", "?"), 12, empty="?")
    limit_price = _escaped(intent.get("limit_price", "?"), 24, empty="?")
    currency = _escaped(intent.get("currency", ""), 8, empty="")
    mode = _escaped(outcome.get("mode") or record.get("mode") or "dry", 8, empty="dry")
    status = _escaped(outcome.get("status", "denied"), 24, empty="denied")
    broker_order_id = _escaped(outcome.get("broker_order_id") or "无", 64, empty="无")
    reasons = _render_quality(record.get("guard_reasons", []))
    decision = _escaped(intent.get("decision", "?"), 24, empty="?")
    snapshot_as_of = _escaped(record.get("snapshot_as_of") or "未提供", 150)

    mode_marker = "🟣 DRY" if mode == "dry" else "🔴 LIVE"
    lines = [
        f"<b>Alpha Guard 交易事件 {mode_marker}</b>",
        "",
        f"标的：<code>{ticker}</code>",
        f"方向：<b>{_escaped(side.upper(), 8)}</b> × 数量：<code>{quantity}</code>",
        f"限价：<code>{limit_price} {currency}</code>",
        f"触发决策：<code>{decision}</code>",
        f"结果：<b>{status}</b>（券商单号 <code>{broker_order_id}</code>）",
        f"快照时间：{snapshot_as_of}",
        f"风控裁决：{'通过' if record.get('guard_allowed') else '驳回'}",
    ]
    if reasons:
        lines.append(f"原因：{reasons}")
    message = "\n".join(lines)
    return _bounded_operational_message(message, state="TRADE", scope=ticker)


async def _send_html(
    text: str,
    *,
    settings: Settings | None,
    bot_factory: BotFactory | None,
    disable_web_page_preview: bool,
) -> None:
    runtime = settings or get_settings()
    token, chat_id = _telegram_credentials(runtime)

    if bot_factory is None:
        from telegram import Bot

        bot_factory = Bot
    bot = bot_factory(token=token)
    if hasattr(bot, "__aenter__") and hasattr(bot, "__aexit__"):
        async with bot as active_bot:
            await active_bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=disable_web_page_preview,
            )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=disable_web_page_preview,
        )


def _telegram_credentials(settings: Settings) -> tuple[str, str]:
    if not settings.notifications_enabled:
        raise NotificationConfigurationError(
            "Real Telegram notifications are disabled. Set "
            "NOTIFICATIONS_ENABLED=true only after validating the destination."
        )

    token = _secret_value(settings.telegram_bot_token)
    chat_id = (settings.telegram_chat_id or "").strip()
    missing: list[str] = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise NotificationConfigurationError(
            "Telegram notifications are enabled but required settings are missing: "
            + ", ".join(missing)
        )
    return token, chat_id


def _evidence_items(signal: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = signal.get("evidence")
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = [item for item in value if isinstance(item, Mapping)]
        if items:
            return items
    return [signal]


def _review_kind(value: Any) -> str:
    action = str(value).strip().upper()
    return {
        "BUY": "规则条件复核（机会观察）",
        "BUY_REVIEW": "规则条件复核（机会观察）",
        "SELL": "规则条件复核（风险观察）",
        "SELL_REVIEW": "规则条件复核（风险观察）",
        "CONFLICT": "相反规则同时命中",
        "UNKNOWN": "数据不足复核",
    }.get(action, "规则条件复核")


def _render_quality(value: Any) -> str:
    if isinstance(value, Mapping):
        items = [f"{key}: {item}" for key, item in list(value.items())[:4]]
    else:
        items = _string_items(value, max_items=4)
    if not items:
        return "未提供"
    return _escaped("；".join(items), 520)


def _string_items(value: Any, *, max_items: int) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in list(value)[:max_items] if str(item).strip()]


def _safe_http_url(value: Any) -> str | None:
    raw = str(value or "").strip().replace("\x00", "")
    if len(raw) > 800:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return raw


def _escaped(value: Any, budget: int, *, empty: str = "未提供") -> str:
    raw = " ".join(str(value if value is not None else "").replace("\x00", "").split())
    if not raw:
        raw = empty
    escaped_parts: list[str] = []
    used = 0
    truncated = False
    suffix = "…"
    content_budget = max(0, budget - len(suffix))
    for character in raw:
        escaped_character = html.escape(character, quote=True)
        if used + len(escaped_character) > content_budget:
            truncated = True
            break
        escaped_parts.append(escaped_character)
        used += len(escaped_character)
    rendered = "".join(escaped_parts)
    if truncated:
        rendered += suffix
    return rendered


def _bounded_message(text: str, *, ticker: str, name: str) -> str:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    # This branch should only be reachable if a future field is added without a
    # budget. Keep the fallback valid HTML and retain the mandatory safety text.
    fallback = "\n".join(
        [
            "🔎 <b>人工核验提醒</b>",
            f"标的：<b>{_escaped(name, 180)}</b>（{_escaped(ticker, 96)}）",
            "内容超过 Telegram 长度上限，请在本地审计记录中核验完整证据。",
            "⚠️ Alpha Guard <b>未执行任何交易</b>。",
        ]
    )
    return fallback


def _bounded_operational_message(text: str, *, state: str, scope: str) -> str:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    return "\n".join(
        [
            f"🔴 <b>Alpha Guard 保护状态：{_escaped(state, 40)}</b>",
            f"范围：{_escaped(scope, 100)}",
            "事件详情超过 Telegram 长度上限，请运行 alpha-guard status 核验。",
            "⚠️ Alpha Guard <b>未执行任何交易</b>。",
        ]
    )


def _secret_value(secret: Any) -> str:
    if secret is None:
        return ""
    getter = getattr(secret, "get_secret_value", None)
    value = getter() if callable(getter) else str(secret)
    return value.strip()


__all__ = [
    "NotificationConfigurationError",
    "format_news_message",
    "format_signal_message",
    "render_news",
    "render_news_alert",
    "render_plain_message",
    "render_incident_alert",
    "render_signal",
    "render_signal_alert",
    "render_trade_alert",
    "send_message",
    "send_incident",
    "send_news_alert",
    "send_signal",
]
