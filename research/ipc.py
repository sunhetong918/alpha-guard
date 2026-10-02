"""Strict commands shared by the local desktop transport and CLI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .service import ResearchService


def execute(service: ResearchService, params: Mapping[str, Any]) -> dict[str, Any]:
    command = params.get("command")
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
    }
    if not isinstance(command, str) or command not in shapes:
        raise ValueError("unsupported research command")
    required, optional = shapes[command]
    supplied = set(params) - {"command"}
    if not required <= supplied or supplied - (required | optional):
        raise ValueError("invalid research command fields")
    if command == "open_login":
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
