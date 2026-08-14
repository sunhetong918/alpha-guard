"""Chinese display vocabulary for redacted Guardian DTOs."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Decision, IncidentState, StatusColor


STATUS_META: dict[StatusColor, tuple[str, str]] = {
    StatusColor.GRAY: ("灰", "未配置 / 已暂停"),
    StatusColor.GREEN: ("绿", "健康"),
    StatusColor.AMBER: ("琥珀", "局部退化"),
    StatusColor.RED: ("红", "失明事故"),
    StatusColor.BLUE: ("蓝", "恢复中"),
}

STATE_LABELS = {
    "UNCONFIGURED": "未配置",
    "PAUSED": "已暂停",
    "HEALTHY": "健康",
    "DEGRADED": "局部退化",
    "BLIND": "不可见 / 失明",
    "RECOVERING": "恢复校准中",
    "RUNNING": "运行中",
    "STOPPED": "已停止",
}

COLOR_STATE_LABELS = {
    StatusColor.GRAY: "未配置 / 已暂停",
    StatusColor.GREEN: "健康",
    StatusColor.AMBER: "局部退化",
    StatusColor.RED: "失明事故",
    StatusColor.BLUE: "恢复中",
}

DEADLINE_LABELS = {
    "outside_activation": "责任范围外",
    "within_grace": "宽限期内",
    "completed": "按时完成",
    "missing": "已漏跑",
    "pending": "证据待定",
    "bad": "未达标",
}

DEADLINE_COLORS = {
    "outside_activation": StatusColor.GRAY,
    "within_grace": StatusColor.BLUE,
    "completed": StatusColor.GREEN,
    "missing": StatusColor.RED,
    "pending": StatusColor.AMBER,
    "bad": StatusColor.RED,
}

DECISION_LABELS = {
    Decision.NONE: "无人工核验事项",
    Decision.BUY_REVIEW: "买入方向 · 待人工复核",
    Decision.SELL_REVIEW: "卖出方向 · 待人工复核",
    Decision.UNKNOWN: "证据不足 · 未知",
    Decision.CONFLICT: "规则冲突 · 待人工复核",
}

DECISION_COLORS = {
    Decision.NONE: StatusColor.GRAY,
    Decision.BUY_REVIEW: StatusColor.BLUE,
    Decision.SELL_REVIEW: StatusColor.AMBER,
    Decision.UNKNOWN: StatusColor.AMBER,
    Decision.CONFLICT: StatusColor.RED,
}

GRADE_LABELS = {
    "healthy": "健康",
    "degraded": "退化",
    "unreliable": "不可靠",
    "insufficient_data": "样本不足",
}

CIRCUIT_LABELS = {
    "closed": "闭合 / 正常",
    "open": "熔断开启",
    "half_open": "恢复探测",
}

INCIDENT_LABELS = {
    IncidentState.OPEN: "处理中",
    IncidentState.RECOVERING: "恢复中",
    IncidentState.RESOLVED: "已关闭",
}

REASON_LABELS = {
    "provider_degraded": "上游能力退化",
    "provider_unreliable": "上游能力不可靠",
    "insufficient_samples": "真实调用样本不足",
    "configuration_baseline_missing": "配置基线待重建",
    "deadline_missed": "预期扫描超过截止时间",
    "state_corrupt": "本地安全账本异常",
    "delivery_unavailable": "提醒通道不可用",
    "delivery_unconfigured": "提醒通道未配置",
    "watcher_unavailable": "外部 Watcher 不可用",
}


def status_label(color: StatusColor, detail: str | None = None) -> str:
    color_name, default_detail = STATUS_META[color]
    return f"{color_name} · {detail or default_detail}"


def state_label(state: str) -> str:
    return STATE_LABELS.get(state, state.replace("_", " ").title())


def reason_label(code: str) -> str:
    return REASON_LABELS.get(code, code.replace("_", " "))


def format_percent(value: float | None, *, empty: str = "—") -> str:
    if value is None:
        return empty
    return f"{value * 100:.1f}%"


def format_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def format_time(
    value: datetime | None,
    timezone: str = "Asia/Shanghai",
    *,
    include_date: bool = True,
) -> str:
    if value is None:
        return "尚无证据"
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    local = value.astimezone(zone)
    return local.strftime("%m-%d %H:%M:%S" if include_date else "%H:%M:%S")


def elapsed_seconds(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "—"
    seconds = max(0, int((end - start).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"
