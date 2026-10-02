"""Explicit research providers; local CLI receives text and has no tools."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any

from config import Settings
from .ingest import _stop_process


class ModelProviderError(RuntimeError):
    """A bounded provider error code, never a raw client diagnostic."""


class ModelCancelled(ModelProviderError):
    pass


def claude_executable(settings: Settings) -> str | None:
    """Resolve only operator configuration or a standard installed CLI."""
    if settings.research_claude_executable:
        value = Path(settings.research_claude_executable).expanduser()
        return str(value) if value.is_absolute() and value.is_file() and os.access(value, os.X_OK) else None
    installed = shutil.which("claude")
    if installed:
        return installed
    for value in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if value.is_file() and os.access(value, os.X_OK):
            return str(value)
    return None


def model_identity(settings: Settings) -> str:
    if settings.research_model_provider == "codex_cli":
        return f"codex_cli:{settings.research_codex_model}"
    return f"{settings.research_model_provider}:{settings.anthropic_model}"


def model_capabilities(settings: Settings) -> dict[str, Any]:
    if settings.research_model_provider == "codex_cli":
        available = codex_executable(settings) is not None
        return {"model_provider": "codex_cli", "model_configured": available,
                "model_installed": available, "model_authentication": "checked_on_analysis"}
    if settings.research_model_provider == "claude_cli":
        available = claude_executable(settings) is not None
        return {"model_provider": "claude_cli", "model_configured": available,
                "model_installed": available, "model_authentication": "checked_on_analysis"}
    return {"model_provider": "anthropic", "model_configured": bool(settings.anthropic_api_key),
            "model_installed": importlib.util.find_spec("anthropic") is not None,
            "model_authentication": "api_key"}


def codex_executable(settings: Settings) -> str | None:
    if settings.research_codex_executable:
        path = Path(settings.research_codex_executable).expanduser()
        return str(path) if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None
    installed = shutil.which("codex")
    if installed:
        return installed
    for path in (Path("/Applications/Codex.app/Contents/Resources/codex"),
                 Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
                 Path("/opt/homebrew/bin/codex")):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _codex_text(prompt: str, settings: Settings, *, system: str,
                cancelled: Callable[[], bool] | None) -> str:
    executable = codex_executable(settings)
    if executable is None:
        raise ModelProviderError("model_client_unavailable")
    if cancelled and cancelled():
        raise ModelCancelled("model_cancelled")
    with tempfile.TemporaryDirectory(prefix="alpha-guard-model-") as directory, ExitStack() as cleanup:
        folder = Path(directory)
        result_file = folder / "answer.json"
        input_file = folder / "input.txt"
        input_file.write_text(system + "\nReturn only the requested JSON. Never call any tool.\n" + prompt)
        os.chmod(input_file, 0o600)
        arguments = [executable, "exec", "--ignore-user-config", "--ephemeral", "--sandbox", "read-only",
                     "--skip-git-repo-check", "--json", "-o", str(result_file),
                     "--model", settings.research_codex_model,
                     "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',
                     "-c", 'history.persistence="none"', "-c", 'model_reasoning_effort="low"',
                     "-c", 'approval_policy="never"', "--enable", "skip_host_skill_discovery"]
        for feature in ("shell_tool", "apps", "multi_agent", "view_image", "image_generation",
                        "tool_suggest", "unified_exec", "shell_snapshot", "sleep_tool", "skill_search",
                        "skill_mcp_dependency_install", "hooks", "plugins", "remote_plugin", "code_mode_host",
                        "browser_use", "browser_use_external", "browser_use_full_cdp_access", "computer_use",
                        "goals", "in_app_local_automation", "in_app_browser", "in_app_chat",
                        "memories", "multi_agent_v2", "code_mode", "code_mode_only",
                        "workspace_dependencies", "worktrees", "realtime_conversation"):
            arguments.extend(["--disable", feature])
        arguments.append("-")
        with input_file.open("rb") as source, (folder / "events.jsonl").open("w+b") as events:
            process = subprocess.Popen(arguments, stdin=source, stdout=events, stderr=subprocess.DEVNULL,
                                       cwd=directory, shell=False, start_new_session=os.name != "nt")
            cleanup.callback(_stop_process, process)
            deadline = time.monotonic() + settings.research_model_timeout_seconds
            while process.poll() is None:
                if cancelled and cancelled():
                    raise ModelCancelled("model_cancelled")
                if time.monotonic() >= deadline:
                    raise ModelProviderError("model_timeout")
                if os.fstat(events.fileno()).st_size > 1_000_000 or (result_file.exists() and result_file.stat().st_size > 400_000):
                    raise ModelProviderError("model_output_too_large")
                time.sleep(0.1)
            if cancelled and cancelled():
                raise ModelCancelled("model_cancelled")
            if process.returncode or not result_file.is_file():
                raise ModelProviderError("model_client_failed")
            if os.fstat(events.fileno()).st_size > 1_000_000 or result_file.stat().st_size > 400_000:
                raise ModelProviderError("model_output_too_large")
            events.seek(0)
            turns = 0
            for line in events:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    raise ModelProviderError("invalid_model_response") from None
                if not isinstance(event, dict):
                    raise ModelProviderError("invalid_model_response")
                turns += event.get("type") == "turn.completed"
                item = event.get("item", {})
                if isinstance(item, dict) and item.get("type") not in {None, "agent_message", "reasoning", "error"}:
                    raise ModelProviderError("unexpected_model_tool_call")
            if turns != 1:
                raise ModelProviderError("model_turn_limit_exceeded")
            result = result_file.read_text(encoding="utf-8")
            if not result or len(result) > 100_000:
                raise ModelProviderError("invalid_model_response")
            return result


def _claude_text(prompt: str, settings: Settings, *, system: str,
                 cancelled: Callable[[], bool] | None) -> str:
    executable = claude_executable(settings)
    if executable is None:
        raise ModelProviderError("model_client_unavailable")
    if cancelled and cancelled():
        raise ModelCancelled("model_cancelled")
    # --safe-mode disables user/project hooks, skills, plugins and memory.
    # No shell, no model tools, one turn and no persisted client conversation.
    arguments = [executable, "--print", "--safe-mode", "--output-format", "json",
                 "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                 "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
                 "--permission-mode", "dontAsk", "--max-turns", "1",
                 "--model", settings.anthropic_model, "--system-prompt", system]
    environment = dict(os.environ)
    # The application uses the client's own existing login, never copies keys.
    # Disable prompt history / nonessential background activity for this run.
    environment.update(CLAUDE_CODE_SKIP_PROMPT_HISTORY="1",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    environment.pop("CLAUDECODE", None)
    with tempfile.TemporaryDirectory(prefix="alpha-guard-model-") as directory, ExitStack() as cleanup:
        prompt_path = Path(directory) / "input.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        os.chmod(prompt_path, 0o600)
        with prompt_path.open("rb") as input_file, (Path(directory) / "result.json").open("w+b") as output:
            process = subprocess.Popen(arguments, stdin=input_file, stdout=output,
                                       stderr=subprocess.DEVNULL, cwd=directory, env=environment,
                                       shell=False, start_new_session=os.name != "nt")
            cleanup.callback(_stop_process, process)
            deadline = time.monotonic() + settings.research_model_timeout_seconds
            while process.poll() is None:
                if cancelled and cancelled():
                    raise ModelCancelled("model_cancelled")
                if time.monotonic() >= deadline:
                    raise ModelProviderError("model_timeout")
                if os.fstat(output.fileno()).st_size > 1_000_000:
                    raise ModelProviderError("model_output_too_large")
                time.sleep(0.1)
            if cancelled and cancelled():
                raise ModelCancelled("model_cancelled")
            if os.fstat(output.fileno()).st_size > 1_000_000:
                raise ModelProviderError("model_output_too_large")
            output.seek(0)
            try:
                result = json.load(output)
            except (ValueError, UnicodeError):
                raise ModelProviderError("invalid_model_response") from None
            if process.returncode or not isinstance(result, dict) or result.get("is_error") is True:
                raise ModelProviderError("model_client_failed")
            if result.get("num_turns", 1) != 1:
                raise ModelProviderError("model_turn_limit_exceeded")
            text = result.get("result")
            if not isinstance(text, str) or not text or len(text) > 100_000:
                raise ModelProviderError("invalid_model_response")
            return text


def extract_text(prompt: str, settings: Settings, *, system: str,
                 client: Any | None = None,
                 cancelled: Callable[[], bool] | None = None) -> str:
    if client is None and settings.research_model_provider == "codex_cli":
        return _codex_text(prompt, settings, system=system, cancelled=cancelled)
    if client is None and settings.research_model_provider == "claude_cli":
        return _claude_text(prompt, settings, system=system, cancelled=cancelled)
    if client is None:
        import anthropic
        if settings.anthropic_api_key is None:
            raise ModelProviderError("model_credentials_unavailable")
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value(),
                                     timeout=min(settings.http_timeout_seconds, 60), max_retries=0)
    response = client.messages.create(model=settings.anthropic_model, max_tokens=6000,
                                      system=system, messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(part, "text", "") for part in getattr(response, "content", []))
