"""Present the configuration saved with a report, never the current settings."""
from __future__ import annotations

import html
from typing import Any


def generation_html(value: Any) -> str:
    if not isinstance(value, dict):
        return "<h3>生成配置</h3><p>生成配置未记录；无法从当前设置还原。</p>"
    parameters = value.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    horizons = value.get("horizons")
    horizons = horizons if isinstance(horizons, (list, tuple)) else []
    tools_enabled = parameters.get("tools_enabled")
    fields = [("提供者", value.get("provider")), ("模型", value.get("model")),
              ("配置档案", value.get("profile_id")), ("档案版本", value.get("profile_version")),
              ("分析器", value.get("analyzer_version")), ("提示模板", value.get("prompt_template_version")),
              ("配置版本", value.get("config_version")),
              ("周期", "、".join({"short": "短线", "swing": "波段", "long": "中长期"}.get(str(v), str(v))
                                for v in horizons) or None),
              ("输出上限（token）", parameters.get("max_output_tokens")),
              ("超时（秒）", parameters.get("timeout_seconds")),
              ("推理强度", parameters.get("reasoning_effort")),
              ("最大轮数", parameters.get("max_turns")),
              ("工具", "允许" if tools_enabled is True else "关闭" if tools_enabled is False else None)]
    rows = "".join(f"<li>{label}：{html.escape(str(item)) if item is not None else '未记录 / 未指定'}</li>"
                   for label, item in fields)
    return "<h3>生成时的配置</h3><p>随原报告保存；之后切换模型不会改写这些值。</p><ul>" + rows + "</ul>"
