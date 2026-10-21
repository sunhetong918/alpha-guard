"""Strict commands shared by the local desktop transport and CLI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .service import ResearchService


def execute(service: ResearchService, params: Mapping[str, Any]) -> dict[str, Any]:
    command = params.get("command")
    if isinstance(command, str) and command.startswith("portfolio_"):
        from .portfolio import PortfolioConflictError, execute_portfolio

        try:
            result = execute_portfolio(service.path, command, {key: value for key, value in params.items() if key != "command"})
        except PortfolioConflictError:
            return {"accepted": False, "command": command, "message": "持仓或预览已变化，请重新读取并预览。",
                    "result": {"status": "conflict"}}
        return {"accepted": True, "command": command, "result": result}
    if command == "instrument_overview":
        from .overview import execute_overview

        result = execute_overview(service.path, {key: value for key, value in params.items() if key != "command"})
        return {"accepted": True, "command": command, "result": result}
    # Workflow handlers validate their complete field set before any mutation.
    if isinstance(command, str) and command in {
        "watchlist_get", "watchlist_save", "rules_preview", "rules_save",
        "review_list", "review_detail", "review_save", "replay_import", "replay_run", "replay_list",
        "event_list", "event_save", "event_import", "event_history", "event_refresh", "event_status", "event_stop",
    }:
        from .reviews import ReviewConflictError, execute_review
        from .watchlist import WatchlistConflictError, execute_watchlist
        from .events import execute_events

        fields = {key: value for key, value in params.items() if key != "command"}
        try:
            if command.startswith(("watchlist_", "rules_")):
                result = execute_watchlist(service.path, command, fields)
            elif command.startswith(("review_", "replay_")):
                result = execute_review(service.path, command, fields)
            else:
                result = execute_events(service.path, command, fields)
        except (WatchlistConflictError, ReviewConflictError):
            return {"accepted": False, "command": command,
                    "message": "记录已被其他操作更新，请刷新后重新编辑并确认。",
                    "result": {"status": "conflict"}}
        return {"accepted": True, "command": command, "result": result}
    # required fields plus optional fields per command; nothing else is accepted.
    shapes: dict[str, tuple[set[str], set[str]]] = {
        "configure": ({"settings"}, set()), "subscribe": ({"subscription"}, set()),
        "subscription": ({"id", "enabled"}, set()), "delete": ({"id"}, set()),
        "import": ({"material"}, set()), "transcribe": ({"path"}, {"source_url", "title"}),
        "start": (set(), set()), "once": (set(), set()), "stop": (set(), set()),
        "open_login": ({"platform"}, set()),
        "model_save": ({"profile"}, {"api_key"}), "model_delete": ({"id"}, set()),
        "model_activate": ({"id"}, set()), "model_test": ({"id"}, set()),
        "digest_save": ({"settings"}, set()), "digest_once": (set(), set()),
        "run_list": (set(), {"offset"}), "run_detail": ({"run_id"}, {"offset"}),
        "resume": ({"run_id"}, {"ack_uncertain"}),
    }
    if not isinstance(command, str) or command not in shapes:
        raise ValueError("unsupported research command")
    required, optional = shapes[command]
    supplied = set(params) - {"command"}
    if not required <= supplied or supplied - (required | optional):
        raise ValueError("invalid research command fields")
    if command in {"run_list", "run_detail", "resume"}:
        from .progress import ResumeError

        try:
            if command == "run_list":
                result = service.run_list(offset=params.get("offset", 0))
            elif command == "run_detail":
                result = service.run_detail(params["run_id"], offset=params.get("offset", 0))
            else:
                result = service.resume(params["run_id"], ack_uncertain=params.get("ack_uncertain", False))
        except ResumeError as exc:
            messages = {
                "run_not_found": "未找到该批次，请刷新运行列表。",
                "run_active": "研究或摘要正在运行，请先停止或等待完成。",
                "collection_not_resumable": "本批次采集尚未完成，不能继续分析，请启动新扫描。",
                "mode_not_resumable": "只支持单次扫描的分析阶段续跑，请启动新扫描。",
                "configuration_changed": "研究设置或模型配置已变化，请启动新扫描。",
                "batch_date_expired": "批次已跨日，请启动新扫描。",
                "material_changed": "原材料版本已变化，请启动新扫描。",
                "market_snapshot_expired": "本批次行情快照已过期，请启动新扫描。",
                "model_budget_exhausted": "本批次模型总额度已用完，请启动新扫描。",
                "nothing_remaining": "本批次没有需要继续的条目。",
                "uncertain_model_call": "有模型调用的结果未确认；继续可能产生额外调用，请明确确认后继续。",
            }
            return {"accepted": False, "command": command, "message": messages[exc.reason],
                    "result": {"run_id": params.get("run_id"), "reason": exc.reason,
                               "status": "ack_required" if exc.acknowledgement else "incompatible"}}
    elif command == "open_login":
        platform = params["platform"]
        if not isinstance(platform, str) or platform not in {"youtube", "bilibili", "douyin", "xiaohongshu"}:
            raise ValueError("invalid login platform")
        from .network import ContentAccessError

        try:
            result = service.open_platform_login(platform)
        except ContentAccessError as exc:
            messages = {
                "browser_disabled": "浏览器读取尚未启用，请先配置 ALPHA_GUARD_BROWSER_ENABLED。",
                "browser_login_requires_dedicated": "当前使用现有浏览器模式，请在已连接的 Chrome 中自行登录。此入口仅用于专用模式。",
                "browser_not_installed": "未找到 Chrome，请安装 Chrome 后重试。",
                "browser_unavailable": "无法打开专用 Chrome，请检查本机 Chrome 后重试。",
            }
            code = str(exc) if str(exc) in messages else "browser_unavailable"
            return {"accepted": False, "command": command,
                    "result": {"status": "failed", "detail": code, "login_verified": False},
                    "message": messages[code]}
    elif command in {"configure", "subscribe", "import"}:
        field = {"configure": "settings", "subscribe": "subscription", "import": "material"}[command]
        value = params[field]
        if not isinstance(value, dict):
            raise ValueError("invalid research payload")
        action = {"configure": service.configure, "subscribe": service.add_subscription, "import": service.import_text}[command]
        result = action(value)
        if command == "import":
            result = {key: result.get(key) for key in ("id", "title", "content_kind", "completeness", "quality_issues")}
    elif command in {"subscription", "delete"}:
        identity = params["id"]
        if not isinstance(identity, str) or not identity or len(identity) > 128:
            raise ValueError("invalid subscription id")
        service.edit_subscription(identity, enabled=params.get("enabled"), delete=command == "delete")
        result = {"id": identity}
    elif command == "transcribe":
        media = {key: value for key, value in params.items() if key != "command"}
        if not all(isinstance(value, str) and len(value) <= 2048 for value in media.values()):
            raise ValueError("invalid transcription input")
        result = service.launch(media=media)
    elif command == "model_save":
        result = service.save_model_profile({"profile": params["profile"],
                                             "api_key": params.get("api_key")})
    elif command == "model_delete":
        identity = params["id"]
        if not isinstance(identity, str) or not identity or len(identity) > 64:
            raise ValueError("invalid model profile id")
        service.delete_model_profile(identity)
        result = {"id": identity, "deleted": True}
    elif command == "model_activate":
        identity = params["id"]
        if identity is not None and (not isinstance(identity, str) or len(identity) > 64):
            raise ValueError("invalid model profile id")
        result = service.activate_model_profile(identity or None)
    elif command == "model_test":
        identity = params["id"]
        if not isinstance(identity, str) or not identity or len(identity) > 64:
            raise ValueError("invalid model profile id")
        result = service.test_model_profile(identity)
        outcome = result.get("status")
        elapsed = result.get("elapsed_seconds")
        message = ("模型连接测试成功（{:.1f} 秒）。".format(elapsed)
                   if outcome == "success" and isinstance(elapsed, (int, float)) else
                   "模型连接测试失败：{}。".format(result.get("detail") or "未知原因"))
        return {"accepted": True, "command": command, "result": result, "message": message}
    elif command == "digest_save":
        value = params["settings"]
        if not isinstance(value, dict):
            raise ValueError("invalid digest settings")
        result = service.save_digest_settings(value)
    elif command == "digest_once":
        result = service.run_digest_once()
    elif command == "stop":
        result = service.stop()
    else:
        result = service.launch(continuous=command == "start")
    return {"accepted": True, "command": command, "result": result}
