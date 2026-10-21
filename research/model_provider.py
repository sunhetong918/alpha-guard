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
from .models import GenerationParameters

_MAX_OUTPUT_TOKENS = 6000
_CLI_MAX_TURNS = 1


def generation_parameters(settings: Settings | None, profile: Any | None = None, *,
                          client_supplied: bool = False) -> GenerationParameters:
    """The same explicit limits used by requests and report configuration snapshots.

    An injected SDK client controls its own timeout. CLI token limits and server
    sampling defaults are not set here and must not be invented in a snapshot.
    """
    if profile is not None:
        return GenerationParameters(max_output_tokens=_MAX_OUTPUT_TOKENS,
                                    timeout_seconds=float(profile.timeout_seconds))
    if client_supplied:
        return GenerationParameters(max_output_tokens=_MAX_OUTPUT_TOKENS)
    provider = getattr(settings, "research_model_provider", "anthropic")
    if provider in {"codex_cli", "claude_cli"}:
        return GenerationParameters(
            timeout_seconds=float(getattr(settings, "research_model_timeout_seconds", 180)),
            reasoning_effort="low" if provider == "codex_cli" else None,
            max_turns=_CLI_MAX_TURNS,
        )
    return GenerationParameters(max_output_tokens=_MAX_OUTPUT_TOKENS,
        timeout_seconds=min(float(getattr(settings, "http_timeout_seconds", 10)), 60.0))


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


def model_identity(settings: Settings, profile: Any | None = None) -> str:
    if profile is not None:
        from .model_profiles import profile_identity

        return profile_identity(profile)
    if settings.research_model_provider == "codex_cli":
        return f"codex_cli:{settings.research_codex_model}"
    return f"{settings.research_model_provider}:{settings.anthropic_model}"


def model_capabilities(settings: Settings, profile: Any | None = None) -> dict[str, Any]:
    if profile is not None:
        return {"model_provider": f"profile:{profile.name}", "model_protocol": profile.protocol,
                "model_configured": True, "model_installed": True,
                "model_authentication": "api_key" if profile.key_configured else "key_optional",
                "model_endpoint_private": profile.allow_private_network}
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
    parameters = generation_parameters(settings)
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
                     "-c", 'history.persistence="none"', "-c", f'model_reasoning_effort="{parameters.reasoning_effort}"',
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
            deadline = time.monotonic() + float(parameters.timeout_seconds or 180)
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
            if turns != parameters.max_turns:
                raise ModelProviderError("model_turn_limit_exceeded")
            result = result_file.read_text(encoding="utf-8")
            if not result or len(result) > 100_000:
                raise ModelProviderError("invalid_model_response")
            return result


def _claude_text(prompt: str, settings: Settings, *, system: str,
                 cancelled: Callable[[], bool] | None) -> str:
    parameters = generation_parameters(settings)
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
                 "--permission-mode", "dontAsk", "--max-turns", str(parameters.max_turns),
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
            deadline = time.monotonic() + float(parameters.timeout_seconds or 180)
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
            if result.get("num_turns", 1) != parameters.max_turns:
                raise ModelProviderError("model_turn_limit_exceeded")
            text = result.get("result")
            if not isinstance(text, str) or not text or len(text) > 100_000:
                raise ModelProviderError("invalid_model_response")
            return text


def _profile_response_text(data: Any, protocol: str) -> str:
    """Parse the same bounded model text for analysis and connection probes."""
    if not isinstance(data, dict) or data.get("error") or data.get("type") == "error":
        raise ModelProviderError("invalid_model_response")
    text: Any = None
    if protocol == "anthropic_compatible":
        content = data.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    raise ModelProviderError("invalid_model_response")
                if part.get("type") == "text":
                    value = part.get("text")
                    if not isinstance(value, str):
                        raise ModelProviderError("invalid_model_response")
                    parts.append(value)
            text = "".join(parts)
    elif protocol == "openai_compatible":
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                text = message.get("content")
    if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
        raise ModelProviderError("invalid_model_response")
    return text


def _profile_text(prompt: str, profile: Any, api_key: str | None, *, system: str,
                  cancelled: Callable[[], bool] | None) -> str:
    from .model_http import model_post_json
    from .network import ContentAccessError

    parameters = generation_parameters(None, profile)
    max_tokens = parameters.max_output_tokens
    if profile.protocol == "anthropic_compatible":
        payload = {"model": profile.model, "max_tokens": max_tokens, "system": system,
                   "messages": [{"role": "user", "content": prompt}]}
        path = "/v1/messages"
    else:
        payload = {"model": profile.model, "max_tokens": max_tokens, "stream": False,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": prompt}]}
        path = "/chat/completions"
    try:
        data = model_post_json(endpoint=profile.base_url, path=path, payload=payload,
                               api_key=api_key, protocol=str(profile.protocol),
                               timeout_seconds=float(parameters.timeout_seconds or 180),
                               allow_private=bool(profile.allow_private_network),
                               cancelled=cancelled)
    except ContentAccessError as exc:
        raise ModelProviderError(str(exc)) from None
    return _profile_response_text(data, str(profile.protocol))


def test_model_connection(profile: Any, api_key: str | None,
                           *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Check a real nonempty model response, not analysis or citation quality.

    The receipt contains neither generated text nor raw provider diagnostics.
    """
    from .model_http import model_post_json
    from .network import ContentAccessError

    started = time.monotonic()
    anthropic_protocol = profile.protocol == "anthropic_compatible"
    payload: dict[str, Any] = {"model": profile.model, "max_tokens": 16,
                               "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
    if not anthropic_protocol:
        payload["stream"] = False
    try:
        data = model_post_json(
            endpoint=profile.base_url,
            path="/v1/messages" if anthropic_protocol else "/chat/completions",
            payload=payload, api_key=api_key, protocol=str(profile.protocol),
            timeout_seconds=min(float(profile.timeout_seconds), 30.0),
            allow_private=bool(profile.allow_private_network), cancelled=cancelled)
    except ContentAccessError as exc:
        return {"status": "failed", "detail": str(exc), "elapsed_seconds": time.monotonic() - started}
    try:
        _profile_response_text(data, str(profile.protocol))
    except ModelProviderError:
        return {"status": "failed", "detail": "invalid_model_response",
                "elapsed_seconds": time.monotonic() - started}
    return {"status": "success", "elapsed_seconds": time.monotonic() - started}


def extract_text(prompt: str, settings: Settings, *, system: str,
                 client: Any | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 profile: Any | None = None, api_key: str | None = None) -> str:
    parameters = generation_parameters(settings, profile, client_supplied=client is not None)
    if profile is not None:
        if cancelled and cancelled():
            raise ModelCancelled("model_cancelled")
        return _profile_text(prompt, profile, api_key, system=system, cancelled=cancelled)
    if client is None and settings.research_model_provider == "codex_cli":
        return _codex_text(prompt, settings, system=system, cancelled=cancelled)
    if client is None and settings.research_model_provider == "claude_cli":
        return _claude_text(prompt, settings, system=system, cancelled=cancelled)
    if client is None:
        import anthropic
        if settings.anthropic_api_key is None:
            raise ModelProviderError("model_credentials_unavailable")
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value(),
                                     timeout=parameters.timeout_seconds, max_retries=0)
    assert parameters.max_output_tokens is not None
    response = client.messages.create(model=settings.anthropic_model, max_tokens=parameters.max_output_tokens,
                                      system=system, messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(part, "text", "") for part in getattr(response, "content", []))
